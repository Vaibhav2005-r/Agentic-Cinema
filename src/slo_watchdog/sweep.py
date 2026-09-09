"""Stage 7: bundle one full sweep.

Stages 1-2 always run. Stages 3-6 run only when an agent is configured; without
one the sweep still produces a ranked candidate list, which is what makes the
detection layer developable and demoable without spending a token.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .agent import AgentSettings, build_agents, candidate_briefing
from .burn_rate import Candidate
from .detect import DetectionSettings, detect
from .discovery import Inventory, discover
from .impact import describe_impact
from .mcp_client import GrafanaConfig, build_toolset
from .models import Evidence, Finding, SweepReport
from .state import StateStore, fingerprint

log = logging.getLogger(__name__)

APP_NAME = "slo-watchdog"


async def run_detection(caller, settings: DetectionSettings | None = None):
    """Stages 1-2. Deterministic, no LLM, no writes."""
    inventory: Inventory = await discover(caller)
    candidates = await detect(caller, inventory, settings)
    return inventory, candidates


def _parse_agent_json(text: str) -> dict[str, Any] | None:
    """The model returns JSON; tolerate a stray code fence around it."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _apply_agent_result(finding: Finding, result: dict[str, Any]) -> Finding:
    """Merge the agent's judgement onto the detector's numbers.

    Only narrative fields are taken from the model; the arithmetic is never
    overwritten, so a hallucinated burn rate cannot reach the report.
    """
    if not result.get("confirmed", True):
        finding.dismissed = True
        finding.dismissal_reason = result.get("dismissal_reason") or "unspecified"

    finding.hypothesis = result.get("hypothesis", "")
    confidence = result.get("confidence")
    if confidence in ("high", "medium", "low"):
        finding.confidence = confidence

    for item in result.get("evidence", []) or []:
        if not isinstance(item, dict):
            continue
        finding.evidence.append(
            Evidence(
                kind=item.get("kind", "metric"),
                summary=item.get("summary", ""),
                source=item.get("source", "unknown"),
            )
        )
    return finding


def _apply_responder_result(finding: Finding, result: dict[str, Any]) -> Finding:
    """Carry stage 6's actual outputs back onto the finding.

    Only values the responder reports from real tool results land here. A
    finding without working deeplinks is not finished, so this is the step that
    makes `incidents_created` and `grafana_links` mean anything.
    """
    incident_id = (result.get("incident_id") or "").strip()
    if incident_id:
        finding.incident_id = incident_id

    if result.get("annotation_created"):
        finding.annotation_created = True

    for link in result.get("grafana_links") or []:
        if isinstance(link, str) and link.strip() and link not in finding.grafana_links:
            finding.grafana_links.append(link.strip())

    for error in result.get("errors") or []:
        if isinstance(error, str) and error.strip():
            finding.evidence.append(
                Evidence(kind="metric", summary=f"write failed: {error}", source="responder")
            )
    return finding


def _as_payload(value: Any) -> dict[str, Any] | None:
    """Session state holds either a parsed object or the raw model text."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return _parse_agent_json(value)
    return None


async def _session_state(runner, *, app_name: str, user_id: str, session_id: str) -> dict[str, Any]:
    try:
        session = await runner.session_service.get_session(
            app_name=app_name, user_id=user_id, session_id=session_id
        )
    except Exception:  # noqa: BLE001 - fall back to the transcript
        log.debug("could not read session state", exc_info=True)
        return {}
    return dict(getattr(session, "state", None) or {})


async def investigate(
    runner,
    candidate: Candidate,
    dashboard_uid: str | None,
    *,
    impact: str | None = None,
    user_id: str,
    session_id: str,
    app_name: str = APP_NAME,
) -> Finding:
    """Stages 3-6 for one candidate."""
    from google.genai import types

    finding = Finding.from_candidate(candidate)
    message = types.Content(
        role="user",
        parts=[types.Part(text=candidate_briefing(candidate, dashboard_uid, impact))],
    )

    transcript: list[str] = []
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=message
    ):
        content = getattr(event, "content", None)
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "text", None):
                transcript.append(part.text)

    # Each sub-agent writes its own result to session state under its
    # `output_key`. Reading both is the only way to recover the responder's
    # work -- the transcript alone loses it behind the investigator's reply.
    state = await _session_state(
        runner, app_name=app_name, user_id=user_id, session_id=session_id
    )

    applied = False
    investigation = _as_payload(state.get("finding"))
    if investigation:
        _apply_agent_result(finding, investigation)
        applied = True

    response = _as_payload(state.get("response"))
    if response:
        _apply_responder_result(finding, response)

    if applied:
        return finding

    # No structured state (an older ADK, or a run that never reached the
    # sub-agent): fall back to the last JSON object in the transcript.
    for chunk in reversed(transcript):
        parsed = _parse_agent_json(chunk)
        if parsed:
            return _apply_agent_result(finding, parsed)

    finding.hypothesis = (transcript[-1] if transcript else "").strip()
    return finding


async def run_sweep(
    caller,
    *,
    config: GrafanaConfig | None = None,
    agent_settings: AgentSettings | None = None,
    detection_settings: DetectionSettings | None = None,
    use_agent: bool = False,
    max_candidates: int = 5,
    state: StateStore | None = None,
    telemetry=None,
) -> SweepReport:
    """One scheduled run, end to end."""
    report = SweepReport(
        started_at=datetime.now(timezone.utc),
        dry_run=(agent_settings or AgentSettings()).dry_run,
    )

    inventory, candidates = await run_detection(caller, detection_settings)
    report.services_discovered = len(inventory.services)
    report.candidates_detected = len(candidates)
    report.errors.extend(inventory.errors)

    if not use_agent:
        report.findings = [Finding.from_candidate(c) for c in candidates]
        report.finished_at = datetime.now(timezone.utc)
        return report

    if state is not None:
        # Drop anything already reported inside the cooldown before spending a
        # single token investigating it again.
        _, suppressed = state.partition([Finding.from_candidate(c) for c in candidates])
        # Match on fingerprint, not service name: one service can carry more
        # than one SLI, and suppressing by name would hide the other.
        known = {fingerprint(f) for f in suppressed}
        if known:
            log.info("suppressed %d already-reported finding(s): %s",
                     len(known), ", ".join(sorted(known)))
        candidates = [
            c for c in candidates
            if fingerprint(Finding.from_candidate(c)) not in known
        ]

    if config is None:
        raise ValueError("running the agent requires a GrafanaConfig")

    from google.adk.runners import InMemoryRunner

    dashboards = {s.name: s.dashboard_uid for s in inventory.services}
    profiles = {s.name: s.profile for s in inventory.services}
    toolset = build_toolset(config)
    try:
        root, _, _ = build_agents(toolset, agent_settings, telemetry=telemetry)
        runner = InMemoryRunner(agent=root, app_name=APP_NAME)
        for candidate in candidates[:max_candidates]:
            session = await runner.session_service.create_session(
                app_name=APP_NAME, user_id="watchdog"
            )
            try:
                finding = await investigate(
                    runner,
                    candidate,
                    dashboards.get(candidate.service),
                    impact=(
                        describe_impact(candidate, profiles[candidate.service]).sentence()
                        if candidate.service in profiles
                        else None
                    ),
                    user_id="watchdog",
                    session_id=session.id,
                    app_name=APP_NAME,
                )
            except Exception as exc:  # noqa: BLE001 - one bad candidate must not kill the sweep
                log.exception("investigation failed for %s", candidate.service)
                report.errors.append(f"{candidate.service}: {exc}")
                continue
            if finding.dismissed:
                report.dismissed.append(finding)
            else:
                if state is not None:
                    state.record(finding)
                report.findings.append(finding)
    finally:
        # Recreated per sweep rather than held open; see mcp_client.open_session.
        await toolset.close()

    if state is not None:
        state.save()

    report.finished_at = datetime.now(timezone.utc)
    return report


def write_report(report: SweepReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.to_json())
    return path

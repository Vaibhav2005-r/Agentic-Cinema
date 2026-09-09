"""Stage 7: bundle one full sweep.

Stages 1-2 always run. Stages 3-6 run only when an agent is configured; without
one the sweep still produces a ranked candidate list, which is what makes the
detection layer developable and demoable without spending a token.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
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


def _as_text(value: Any) -> str:
    """Whatever the model returned, render it as a string.

    Structured output is a request, not a guarantee: a live run put a dict in a
    field the schema declares as a string, and slicing it for the report raised
    `KeyError: slice(None, 300, None)`.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        # Prefer an obvious text field before falling back to JSON.
        for key in ("text", "summary", "description", "value", "content"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner
        return json.dumps(value, default=str)
    if isinstance(value, (list, tuple)):
        return " ".join(_as_text(v) for v in value if v is not None)
    return str(value)


def _apply_agent_result(finding: Finding, result: dict[str, Any]) -> Finding:
    """Merge the agent's judgement onto the detector's numbers.

    Only narrative fields are taken from the model; the arithmetic is never
    overwritten, so a hallucinated burn rate cannot reach the report.
    """
    if not result.get("confirmed", True):
        finding.dismissed = True
        finding.dismissal_reason = _as_text(result.get("dismissal_reason")) or "unspecified"

    # A schema constrains what we *ask* for, not what arrives. A live run
    # returned a dict where the schema says string, and the report crashed
    # formatting it. Coerce every narrative field rather than trusting it.
    finding.hypothesis = _as_text(result.get("hypothesis"))
    confidence = result.get("confidence")
    if confidence in ("high", "medium", "low"):
        finding.confidence = confidence

    for item in result.get("evidence", []) or []:
        if not isinstance(item, dict):
            continue
        finding.evidence.append(
            Evidence(
                kind=_as_text(item.get("kind")) or "metric",
                summary=_as_text(item.get("summary")),
                source=_as_text(item.get("source")) or "unknown",
            )
        )
    return finding


def _apply_responder_result(finding: Finding, result: dict[str, Any]) -> Finding:
    """Carry stage 6's actual outputs back onto the finding.

    Only values the responder reports from real tool results land here. A
    finding without working deeplinks is not finished, so this is the step that
    makes `incidents_created` and `grafana_links` mean anything.
    """
    incident_id = _as_text(result.get("incident_id")).strip()
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


#: Free-tier Gemini allows 5 requests per minute per model. One agentic
#: investigation makes a request per tool-use turn, so a sweep hits that wall
#: almost immediately. The API tells us how long to wait; honour it.
_RETRY_DELAY_RE = re.compile(r"[\"']retryDelay[\"']:\s*[\"'](\d+(?:\.\d+)?)s")
DEFAULT_MAX_RETRIES = 3
FALLBACK_RETRY_SECONDS = 45.0


def is_rate_limited(exc: Exception) -> bool:
    text = str(exc)
    return "429" in text or "RESOURCE_EXHAUSTED" in text


def is_transient(exc: Exception) -> bool:
    """503s are the model being busy, not the request being wrong."""
    text = str(exc)
    return is_rate_limited(exc) or "503" in text or "UNAVAILABLE" in text


def retry_after_seconds(exc: Exception, attempt: int) -> float:
    """Prefer the delay the API asked for over a guess."""
    match = _RETRY_DELAY_RE.search(str(exc))
    if match:
        return float(match.group(1)) + 1.0
    return FALLBACK_RETRY_SECONDS * (attempt + 1)


async def investigate_with_retry(
    runner,
    candidate: Candidate,
    dashboard_uid: str | None,
    *,
    impact: str | None = None,
    user_id: str,
    session_id_factory,
    app_name: str = APP_NAME,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Finding:
    """Investigate, backing off when the model is rate limited or busy.

    A fresh session per attempt: a partially-consumed one would replay the
    turns that already burned quota.
    """
    last: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await investigate(
                runner,
                candidate,
                dashboard_uid,
                impact=impact,
                user_id=user_id,
                session_id=await session_id_factory(),
                app_name=app_name,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised below when fatal
            last = exc
            if attempt >= max_retries or not is_transient(exc):
                raise
            delay = retry_after_seconds(exc, attempt)
            log.warning(
                "%s: %s; retrying in %.0fs (attempt %d/%d)",
                candidate.service,
                "rate limited" if is_rate_limited(exc) else "model unavailable",
                delay,
                attempt + 1,
                max_retries,
            )
            await asyncio.sleep(delay)
    raise last  # pragma: no cover - loop always returns or raises


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
    max_retries: int = DEFAULT_MAX_RETRIES,
    pace_seconds: float = 0.0,
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
        async def new_session() -> str:
            session = await runner.session_service.create_session(
                app_name=APP_NAME, user_id="watchdog"
            )
            return session.id

        for index, candidate in enumerate(candidates[:max_candidates]):
            if index and pace_seconds:
                # Free-tier quota is per minute, so spacing candidates costs
                # nothing but avoids walking straight back into the limit.
                await asyncio.sleep(pace_seconds)
            try:
                finding = await investigate_with_retry(
                    runner,
                    candidate,
                    dashboards.get(candidate.service),
                    impact=(
                        describe_impact(candidate, profiles[candidate.service]).sentence()
                        if candidate.service in profiles
                        else None
                    ),
                    user_id="watchdog",
                    session_id_factory=new_session,
                    app_name=APP_NAME,
                    max_retries=max_retries,
                )
            except Exception as exc:  # noqa: BLE001 - one bad candidate must not kill the sweep
                log.warning("investigation failed for %s: %s", candidate.service, exc)
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

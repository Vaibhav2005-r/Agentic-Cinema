"""Stages 3-6: the agent.

One root agent with two sub-agents. Resisting a six-agent swarm is deliberate:
it demos worse and debugs harder.

  investigator -- stages 3-5. Triage, correlate, hypothesise.
  responder    -- stage 6. File the incident, mark the dashboard.

The split exists so the responder's writes can be gated behind --dry-run and
traced individually. The root agent is a thin orchestrator.

Note what the agent is *not* given: any arithmetic to do. Burn rates, budgets
and exhaustion dates arrive pre-computed from `burn_rate.py`. The agent reasons
about meaning, not numbers.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from .burn_rate import Candidate
from .mcp_client import RESPONDER_TOOLS, is_write_tool

log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-2.5-pro"
FAST_MODEL = "gemini-2.5-flash"


INVESTIGATOR_INSTRUCTION = """\
You are the investigator in an SLO watchdog sweep. You are handed a candidate
that deterministic Python has already detected and quantified.

The numbers are settled. Burn rate, error budget remaining, windows and
projected exhaustion were computed from raw PromQL and are not yours to
recompute, restate differently, or second-guess. Your job is judgement.

Work in three steps.

1. TRIAGE. Decide whether this is a real, ongoing degradation or an artefact.
   Dismiss it when the evidence says: a deploy or migration window that has
   ended, a load test, an expected batch job, a metric that stopped reporting,
   or a burn already trending back to healthy. Use `query_prometheus` to look
   at the shape of the series and `get_dashboard_panel_queries` to see how the
   humans who own this service measure it. Dismissing a candidate is a correct
   and valuable outcome -- say so plainly and stop.

2. CORRELATE. For a real finding, gather evidence over the burn window only:
     - `query_loki_patterns` for the dominant log structures
     - `query_loki_logs` for representative error lines
     - `find_error_pattern_logs` for elevated error patterns, when available
   Prefer three specific, quotable pieces of evidence over ten vague ones.
   Quote actual log lines. Never invent a service name, error string, trace ID
   or numeric value that did not appear in a tool result.

3. HYPOTHESISE. Write a root-cause narrative a tired on-call engineer can read
   in twenty seconds: what is failing, since when, what the blast radius is,
   and what you would check next. Tie every claim to evidence you actually
   retrieved. If the evidence does not support a cause, say the cause is
   unclear and state what would settle it -- a confident wrong answer is worse
   than an honest gap.

Set confidence honestly: `high` only when log or trace evidence directly names
a mechanism; `medium` when the correlation is strong but circumstantial; `low`
when you are mostly inferring from the metric shape.

Return your result as JSON matching the schema you were given. No prose outside
the JSON.
"""


RESPONDER_INSTRUCTION = """\
You are the responder. You act on findings the investigator has confirmed.

For each confirmed finding, in this order:

1. `generate_deeplink` for the service's dashboard and for the Explore query
   covering the burn window. Every claim must be verifiable in one click; a
   finding without working links is not finished.
2. `create_annotation` on the service's dashboard, spanning the burn window,
   tagged `slo-watchdog`. Put the burn rate and budget remaining in the text.
   The annotation is the point: the next human to open that dashboard sees the
   agent's marker sitting on the anomaly.
3. `create_incident` in Grafana IRM, with the hypothesis as the summary and the
   deeplinks in the body. Title it as
   `[SLO watchdog] <service>: <burn>x burn, <budget>% budget left`.

Rules. Never invent an ID, URL or timestamp -- use only values returned by
tools. Never file a second incident for a finding that already carries an
incident ID. If a write fails, report the failure; do not retry it silently or
pretend it succeeded. You may call only these tools:
""" + ", ".join(sorted(RESPONDER_TOOLS)) + "."


ROOT_INSTRUCTION = """\
You orchestrate one SLO watchdog sweep.

Deterministic Python has already discovered the services and detected the
candidates. You do not detect anything and you do not do arithmetic.

For each candidate you are given, delegate to `investigator`. If it returns a
confirmed finding, delegate that finding to `responder`. If it dismisses the
candidate, record the dismissal reason and move on -- staying quiet about a
false positive is a successful outcome, not a failure.

Handle candidates one at a time so each investigation is separately traceable.
When every candidate is resolved, summarise: how many were confirmed, how many
dismissed and why, and what the most urgent finding is.
"""


FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "confirmed": {
            "type": "boolean",
            "description": "False when this is an artefact that should be dismissed.",
        },
        "dismissal_reason": {
            "type": "string",
            "description": "Required when confirmed is false; empty otherwise.",
        },
        "hypothesis": {
            "type": "string",
            "description": "Root-cause narrative, grounded in retrieved evidence.",
        },
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["log_pattern", "trace", "panel_image", "metric", "deeplink"],
                    },
                    "summary": {"type": "string"},
                    "source": {
                        "type": "string",
                        "description": "The MCP tool this came from.",
                    },
                },
                "required": ["kind", "summary", "source"],
            },
        },
        "next_check": {
            "type": "string",
            "description": "The single most useful thing a human should look at next.",
        },
    },
    "required": ["service", "confirmed", "hypothesis", "confidence", "evidence"],
}


#: The responder's output. Without a schema here its work was unrecoverable:
#: the incident was filed, but the ID and the deeplinks never made it back into
#: the report, so `incidents_created` always read zero.
RESPONDER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "service": {"type": "string"},
        "incident_id": {
            "type": "string",
            "description": "ID returned by create_incident. Empty if none was filed.",
        },
        "annotation_created": {"type": "boolean"},
        "grafana_links": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Deeplinks returned by generate_deeplink, verbatim.",
        },
        "actions_taken": {"type": "array", "items": {"type": "string"}},
        "errors": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Any write that failed. Report it; never retry silently.",
        },
    },
    "required": ["service", "actions_taken"],
}


@dataclass
class AgentSettings:
    model: str = DEFAULT_MODEL
    investigator_model: str | None = None
    dry_run: bool = True


def block_writes_callback(tool, args=None, tool_context=None, **kwargs):
    """Hard gate for --dry-run.

    Prompt instructions are not a security boundary; a dry run must be
    physically unable to write. Returning a dict short-circuits the call, so
    the model sees a normal tool response and carries on.

    ADK invokes this as `callback(tool=..., args=..., tool_context=...)`, all
    by keyword -- the parameter names here are part of that contract.
    """
    name = getattr(tool, "name", "")
    if is_write_tool(name):
        log.info("dry-run: suppressed write tool %s", name)
        return {
            "status": "skipped",
            "reason": "dry-run is enabled; no write was performed",
            "tool": name,
        }
    return None


def make_tool_callback(dry_run: bool, telemetry=None):
    """Count every agent tool call, and gate writes when dry-running.

    Counting has to happen here rather than in `mcp_client`: the agent reaches
    MCP through ADK's toolset, not through our own transport, so stages 3-6
    were invisible to `mcp_tool_calls` and the metric always reported zero.
    """
    if not dry_run and telemetry is None:
        return None

    def callback(tool, args=None, tool_context=None, **kwargs):
        if telemetry is not None:
            telemetry.count_tool_call(getattr(tool, "name", ""))
        if dry_run:
            return block_writes_callback(tool=tool, args=args, tool_context=tool_context)
        return None

    return callback


def candidate_briefing(
    candidate: Candidate,
    dashboard_uid: str | None = None,
    impact: str | None = None,
) -> str:
    """The structured hand-off from deterministic detection to the agent.

    Deliberately not a time series. Feeding raw series into context costs
    roughly an order of magnitude more tokens and invites the model to redo
    arithmetic that is already correct.
    """
    payload = {
        "service": candidate.service,
        "sli": candidate.sli,
        "slo_target": candidate.slo_target,
        "slo_is_provisional": candidate.provisional,
        "tier": candidate.tier.name,
        "tier_meaning": candidate.tier.rationale,
        "burn_rate": round(candidate.burn_rate, 3),
        "short_window_burn_rate": round(candidate.short_burn_rate, 3),
        "windows": {"long": candidate.windows[0], "short": candidate.windows[1]},
        # Withheld rather than guessed: the agent must not narrate a budget
        # figure the detector does not trust.
        "budget_remaining_pct": (
            None
            if candidate.budget_is_estimate
            else round(candidate.budget_remaining_pct, 2)
        ),
        "budget_unavailable_reason": (
            f"only {candidate.budget_coverage:.0%} of the SLO window has data; "
            "do not state a budget or exhaustion date"
            if candidate.budget_is_estimate
            else None
        ),
        "projected_exhaustion": (
            candidate.projected_exhaustion.isoformat()
            if candidate.projected_exhaustion and not candidate.budget_is_estimate
            else None
        ),
        "statistical_confidence": candidate.confidence,
        "requests_in_long_window": candidate.request_count,
        "dashboard_uid": dashboard_uid,
        "pages_today": candidate.severity == "page",
        # What this costs a viewer or a production, computed deterministically.
        "audience_impact": impact,
    }
    return (
        "Investigate this candidate. These numbers are already verified; do not "
        "recompute them.\n\n" + json.dumps(payload, indent=2)
    )


def build_agents(toolset, settings: AgentSettings | None = None, telemetry=None):
    """Construct the root agent and its two sub-agents.

    `toolset` is recreated per sweep by the caller rather than held open, which
    is where ADK/MCP session churn otherwise bites on long-running schedules.
    """
    from google.adk.agents import LlmAgent

    settings = settings or AgentSettings()
    write_gate = make_tool_callback(settings.dry_run, telemetry)

    investigator = LlmAgent(
        name="investigator",
        model=settings.investigator_model or settings.model,
        description=(
            "Triages a burn-rate candidate, correlates logs and traces, and "
            "writes a root-cause hypothesis. Read-only."
        ),
        instruction=INVESTIGATOR_INSTRUCTION,
        tools=[toolset],
        output_schema=FINDING_SCHEMA,
        output_key="finding",
        before_tool_callback=write_gate,
    )

    responder = LlmAgent(
        name="responder",
        model=settings.model,
        description=(
            "Files the Grafana IRM incident and annotates the dashboard for a "
            "confirmed finding. The only agent permitted to write."
        ),
        instruction=RESPONDER_INSTRUCTION,
        tools=[toolset],
        output_schema=RESPONDER_SCHEMA,
        output_key="response",
        before_tool_callback=write_gate,
    )

    root = LlmAgent(
        name="slo_watchdog",
        model=settings.model,
        description="Orchestrates one SLO watchdog sweep.",
        instruction=ROOT_INSTRUCTION,
        sub_agents=[investigator, responder],
    )
    return root, investigator, responder

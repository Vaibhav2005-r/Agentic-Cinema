"""The agent's guardrails.

These are the tests that matter for trust: the dry run must be physically
unable to write, and the model must never be handed arithmetic to redo.
"""

from __future__ import annotations

import json

import pytest

from slo_watchdog.agent import (
    FINDING_SCHEMA,
    RESPONDER_INSTRUCTION,
    block_writes_callback,
    candidate_briefing,
)
from slo_watchdog.burn_rate import REQUIRED_WINDOWS, RatioSample, evaluate
from slo_watchdog.mcp_client import RESPONDER_TOOLS, WRITE_TOOLS
from slo_watchdog.models import Finding
from slo_watchdog.sweep import _apply_agent_result, _parse_agent_json


class FakeTool:
    def __init__(self, name: str):
        self.name = name


def a_candidate(**kwargs):
    samples = {
        w: RatioSample(w, 0.0023, request_count=500_000) for w in REQUIRED_WINDOWS
    }
    # A healthier 30d ratio, so budget_remaining is a real number rather than
    # the floor -- otherwise "unchanged" would be trivially true.
    samples["30d"] = RatioSample("30d", 0.0006, request_count=5_000_000)
    return evaluate("paymentservice", samples, 0.999, **kwargs)


# --- the dry-run gate ------------------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOLS))
def test_dry_run_blocks_every_write_tool(tool_name):
    result = block_writes_callback(FakeTool(tool_name), {})
    assert result is not None and result["status"] == "skipped"


@pytest.mark.parametrize("tool_name", ["query_prometheus", "query_loki_logs", "generate_deeplink"])
def test_dry_run_lets_reads_through(tool_name):
    assert block_writes_callback(FakeTool(tool_name), {}) is None


def test_the_responder_may_only_use_tools_it_was_told_about():
    for tool in RESPONDER_TOOLS:
        assert tool in RESPONDER_INSTRUCTION


def test_find_error_pattern_logs_is_treated_as_a_write():
    """It reads logs but creates a Sift investigation, so it is a write."""
    assert "find_error_pattern_logs" in WRITE_TOOLS


# --- the hand-off ----------------------------------------------------------


def test_the_briefing_carries_computed_numbers_not_a_time_series():
    briefing = candidate_briefing(a_candidate(), dashboard_uid="dash-1")
    payload = json.loads(briefing.split("\n\n", 1)[1])
    assert payload["burn_rate"] == pytest.approx(2.3)
    assert payload["windows"] == {"long": "3d", "short": "6h"}
    assert payload["dashboard_uid"] == "dash-1"
    assert "do not recompute" in briefing
    # No raw samples: feeding series into context costs ~10x and invites the
    # model to redo arithmetic that is already correct.
    assert "samples" not in payload


def test_the_briefing_flags_a_provisional_slo():
    payload = json.loads(
        candidate_briefing(a_candidate(provisional=True)).split("\n\n", 1)[1]
    )
    assert payload["slo_is_provisional"] is True


def test_the_finding_schema_requires_a_dismissal_path():
    assert "confirmed" in FINDING_SCHEMA["required"]
    assert "dismissal_reason" in FINDING_SCHEMA["properties"]
    assert FINDING_SCHEMA["properties"]["confidence"]["enum"] == ["high", "medium", "low"]


# --- merging the agent's answer -------------------------------------------


def base_finding() -> Finding:
    return Finding.from_candidate(a_candidate())


def test_the_agent_cannot_overwrite_the_arithmetic():
    """A hallucinated burn rate must never reach the report."""
    finding = base_finding()
    before = (finding.burn_rate, finding.budget_remaining_pct, finding.windows,
              finding.slo_target, finding.tier)
    _apply_agent_result(
        finding,
        {"confirmed": True, "hypothesis": "h", "confidence": "high",
         "burn_rate": 99.0, "budget_remaining_pct": 0.0, "slo_target": 0.5,
         "windows": ["1h", "5m"], "tier": "page-fast"},
    )
    assert (finding.burn_rate, finding.budget_remaining_pct, finding.windows,
            finding.slo_target, finding.tier) == before
    assert finding.burn_rate == pytest.approx(2.3)
    assert finding.budget_remaining_pct == pytest.approx(40.0)
    # Only the narrative moved.
    assert finding.hypothesis == "h"


def test_a_dismissal_is_recorded_with_its_reason():
    finding = base_finding()
    _apply_agent_result(
        finding,
        {"confirmed": False, "dismissal_reason": "load test window", "hypothesis": "",
         "confidence": "low", "evidence": []},
    )
    assert finding.dismissed and finding.dismissal_reason == "load test window"


def test_evidence_is_carried_across():
    finding = base_finding()
    _apply_agent_result(
        finding,
        {"confirmed": True, "hypothesis": "h", "confidence": "medium",
         "evidence": [{"kind": "log_pattern", "summary": "upstream timeout",
                       "source": "query_loki_patterns"}]},
    )
    assert len(finding.evidence) == 1
    assert finding.evidence[0].source == "query_loki_patterns"


def test_an_invalid_confidence_from_the_model_is_ignored():
    finding = base_finding()
    finding.confidence = "high"
    _apply_agent_result(finding, {"confirmed": True, "hypothesis": "h", "confidence": "certain"})
    assert finding.confidence == "high"


@pytest.mark.parametrize(
    "text", ['{"a": 1}', '```json\n{"a": 1}\n```', '```\n{"a": 1}\n```']
)
def test_agent_json_survives_a_code_fence(text):
    assert _parse_agent_json(text) == {"a": 1}


def test_unparseable_agent_output_is_not_a_crash():
    assert _parse_agent_json("I could not determine a cause.") is None


# --- MCP argument contract -------------------------------------------------
#
# These shapes are transcribed from mcp-grafana's Go structs. They are the
# thing the graded requirement actually rests on: a wrong key here means every
# call fails at runtime, and no amount of offline testing would notice.


def test_instant_query_sends_the_required_end_time():
    """`endTime` is required on query_prometheus even for an instant query.

    Omitting it fails every call the detection engine makes.
    """
    from slo_watchdog import tools

    args = tools.instant_query("prom-1", "up")
    assert args["endTime"] == "now"
    assert args["queryType"] == "instant"
    assert args["datasourceUid"] == "prom-1"
    assert args["expr"] == "up"


def test_range_query_sends_start_and_step():
    from slo_watchdog import tools

    args = tools.range_query("prom-1", "up", start="now-1h")
    assert {"startTime", "endTime", "stepSeconds"} <= set(args)
    assert args["queryType"] == "range"


def test_label_matches_are_selector_objects_not_strings():
    """`matches` is []Selector; a bare metric name is silently not a selector."""
    from slo_watchdog import tools

    args = tools.label_values("prom-1", "service_name", "http_server_request_duration_seconds_count")
    assert args["matches"] == [
        {"filters": [{"name": "__name__",
                      "value": "http_server_request_duration_seconds_count",
                      "type": "="}]}
    ]


def test_label_values_omits_matches_when_no_metric_given():
    from slo_watchdog import tools

    assert "matches" not in tools.label_values("prom-1", "service_name")


def test_metric_names_always_sends_a_limit():
    """The server default is 10, which silently truncates discovery."""
    from slo_watchdog import tools

    assert tools.metric_names("prom-1")["limit"] == 5000


def test_loki_uses_logql_not_expr():
    from slo_watchdog import tools

    args = tools.loki_query("loki-1", '{app="x"}', start="now-3d")
    assert "logql" in args and "expr" not in args
    assert {"startRfc3339", "endRfc3339"} <= set(args)


def test_annotation_spans_a_region_not_a_point():
    """time + timeEnd is what puts the marker across the anomaly window."""
    from slo_watchdog import tools

    args = tools.annotation("dash-1", "burning", start_ms=1000, end_ms=2000)
    assert args["time"] == 1000 and args["timeEnd"] == 2000
    assert args["tags"] == ["slo-watchdog"]


def test_incident_sends_every_required_field():
    from slo_watchdog import tools

    args = tools.incident("[SLO watchdog] paymentservice")
    assert {"title", "severity", "status", "roomPrefix", "isDrill"} <= set(args)


def test_deeplink_names_its_resource_type():
    from slo_watchdog import tools

    assert tools.deeplink_dashboard("dash-1")["resourceType"] == "dashboard"

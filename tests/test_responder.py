"""Stage 6's outputs must survive back into the report.

Before this was wired, the responder filed the incident and the ID was
discarded: `incidents_created` always read zero and no deeplink ever reached
the report. These tests pin that path shut.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from slo_watchdog.agent import (
    RESPONDER_SCHEMA,
    block_writes_callback,
    make_tool_callback,
)
from slo_watchdog.burn_rate import REQUIRED_WINDOWS, RatioSample, evaluate
from slo_watchdog.mcp_client import WRITE_TOOLS
from slo_watchdog.models import Finding, SweepReport
from slo_watchdog.observability import Telemetry
from slo_watchdog.sweep import (
    _apply_responder_result,
    _as_payload,
    _session_state,
    investigate,
)


class FakeTool:
    def __init__(self, name: str):
        self.name = name


def a_finding() -> Finding:
    samples = {w: RatioSample(w, 0.0023, request_count=500_000) for w in REQUIRED_WINDOWS}
    return Finding.from_candidate(evaluate("paymentservice", samples, 0.999))


# --- the ADK callback contract --------------------------------------------


@pytest.mark.parametrize("tool_name", sorted(WRITE_TOOLS))
def test_the_gate_works_when_called_the_way_adk_calls_it(tool_name):
    """ADK invokes `callback(tool=..., args=..., tool_context=...)` by keyword.

    The parameter names are part of the contract; calling positionally in a
    test would not catch a rename that breaks every live run.
    """
    result = block_writes_callback(tool=FakeTool(tool_name), args={}, tool_context=None)
    assert result is not None and result["status"] == "skipped"


def test_the_gate_passes_reads_through_by_keyword():
    assert block_writes_callback(
        tool=FakeTool("query_prometheus"), args={}, tool_context=None
    ) is None


# --- tool-call counting ----------------------------------------------------


def test_the_callback_counts_agent_tool_calls():
    """Agent calls reach MCP through ADK, not our transport, so they must be
    counted here or `mcp_tool_calls` reports zero for stages 3-6."""
    telemetry = Telemetry(enabled=False)
    callback = make_tool_callback(dry_run=False, telemetry=telemetry)
    for name in ("query_prometheus", "query_loki_logs", "create_incident"):
        callback(tool=FakeTool(name), args={}, tool_context=None)
    assert telemetry.metrics.mcp_tool_calls == 3


def test_counting_and_gating_compose():
    telemetry = Telemetry(enabled=False)
    callback = make_tool_callback(dry_run=True, telemetry=telemetry)
    blocked = callback(tool=FakeTool("create_incident"), args={}, tool_context=None)
    allowed = callback(tool=FakeTool("query_prometheus"), args={}, tool_context=None)
    assert blocked["status"] == "skipped"
    assert allowed is None
    assert telemetry.metrics.mcp_tool_calls == 2  # both counted, one blocked


def test_a_live_run_with_no_telemetry_installs_no_callback():
    """No gate and nothing to count means ADK should call nothing at all."""
    assert make_tool_callback(dry_run=False, telemetry=None) is None


def test_a_live_run_with_telemetry_still_permits_writes():
    telemetry = Telemetry(enabled=False)
    callback = make_tool_callback(dry_run=False, telemetry=telemetry)
    assert callback(tool=FakeTool("create_incident"), args={}, tool_context=None) is None
    assert telemetry.metrics.mcp_tool_calls == 1


# --- merging the responder's result ---------------------------------------


def test_the_incident_id_reaches_the_finding():
    finding = a_finding()
    _apply_responder_result(finding, {"incident_id": "inc-42", "actions_taken": []})
    assert finding.incident_id == "inc-42"


def test_deeplinks_are_collected_without_duplicates():
    finding = a_finding()
    _apply_responder_result(
        finding,
        {"grafana_links": ["https://g/d/abc", "https://g/d/abc", " https://g/explore "],
         "actions_taken": []},
    )
    assert finding.grafana_links == ["https://g/d/abc", "https://g/explore"]


def test_an_empty_incident_id_is_not_recorded():
    finding = a_finding()
    _apply_responder_result(finding, {"incident_id": "   ", "actions_taken": []})
    assert finding.incident_id is None


def test_the_annotation_flag_is_carried_across():
    finding = a_finding()
    _apply_responder_result(finding, {"annotation_created": True, "actions_taken": []})
    assert finding.annotation_created


def test_a_failed_write_is_surfaced_not_swallowed():
    finding = a_finding()
    _apply_responder_result(
        finding, {"errors": ["create_annotation: 403 forbidden"], "actions_taken": []}
    )
    assert len(finding.evidence) == 1
    assert "403 forbidden" in finding.evidence[0].summary
    assert finding.evidence[0].source == "responder"


def test_the_responder_schema_asks_for_what_the_report_needs():
    props = RESPONDER_SCHEMA["properties"]
    for field in ("incident_id", "grafana_links", "annotation_created", "errors"):
        assert field in props


# --- session-state plumbing -----------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [({"a": 1}, {"a": 1}), ('{"a": 1}', {"a": 1}), ("not json", None), (None, None), (7, None)],
)
def test_as_payload_handles_every_state_shape(value, expected):
    assert _as_payload(value) == expected


class FakeSession:
    def __init__(self, state):
        self.id = "sess-1"
        self.state = state


class FakeSessionService:
    def __init__(self, state):
        self._state = state

    async def get_session(self, *, app_name, user_id, session_id):
        return FakeSession(self._state)


class ExplodingSessionService:
    async def get_session(self, *, app_name, user_id, session_id):
        raise RuntimeError("session backend down")


class FakeRunner:
    """Mimics ADK: streams events, then exposes sub-agent output in state."""

    def __init__(self, state, transcript=()):
        self.session_service = FakeSessionService(state)
        self._transcript = transcript

    async def run_async(self, *, user_id, session_id, new_message=None, **kwargs):
        for text in self._transcript:
            yield type("Event", (), {
                "content": type("C", (), {"parts": [type("P", (), {"text": text})()]})()
            })()


def a_candidate():
    samples = {w: RatioSample(w, 0.0023, request_count=500_000) for w in REQUIRED_WINDOWS}
    return evaluate("paymentservice", samples, 0.999)


async def test_investigate_recovers_both_sub_agent_results():
    runner = FakeRunner(
        {
            "finding": {"service": "paymentservice", "confirmed": True,
                        "hypothesis": "upstream timeouts", "confidence": "high",
                        "evidence": [{"kind": "log_pattern", "summary": "timeout",
                                      "source": "query_loki_patterns"}]},
            "response": {"service": "paymentservice", "incident_id": "inc-7",
                         "annotation_created": True,
                         "grafana_links": ["https://g/d/pay"], "actions_taken": ["filed"]},
        }
    )
    finding = await investigate(
        runner, a_candidate(), "dash-1", user_id="u", session_id="s"
    )
    assert finding.hypothesis == "upstream timeouts"
    assert finding.confidence == "high"
    assert finding.incident_id == "inc-7"
    assert finding.annotation_created
    assert finding.grafana_links == ["https://g/d/pay"]
    assert len(finding.evidence) == 1
    # The arithmetic still comes from the detector.
    assert finding.burn_rate == pytest.approx(2.3)


async def test_investigate_records_a_dismissal_from_state():
    runner = FakeRunner(
        {"finding": {"service": "cartservice", "confirmed": False,
                     "dismissal_reason": "deploy window", "hypothesis": "",
                     "confidence": "low", "evidence": []}}
    )
    finding = await investigate(runner, a_candidate(), None, user_id="u", session_id="s")
    assert finding.dismissed and finding.dismissal_reason == "deploy window"
    assert finding.incident_id is None


async def test_investigate_falls_back_to_the_transcript_without_state():
    runner = FakeRunner({}, transcript=['```json\n{"confirmed": true, '
                                        '"hypothesis": "from transcript", '
                                        '"confidence": "medium"}\n```'])
    finding = await investigate(runner, a_candidate(), None, user_id="u", session_id="s")
    assert finding.hypothesis == "from transcript"
    assert finding.confidence == "medium"


async def test_investigate_survives_an_unusable_session_backend():
    runner = FakeRunner({}, transcript=["could not determine a cause"])
    runner.session_service = ExplodingSessionService()
    finding = await investigate(runner, a_candidate(), None, user_id="u", session_id="s")
    assert finding.hypothesis == "could not determine a cause"


async def test_session_state_read_failure_is_not_fatal():
    runner = FakeRunner({})
    runner.session_service = ExplodingSessionService()
    assert await _session_state(runner, app_name="a", user_id="u", session_id="s") == {}


# --- report accounting -----------------------------------------------------


def test_the_report_counts_incidents_and_annotations():
    filed = a_finding()
    filed.incident_id, filed.annotation_created = "inc-1", True
    marked_only = a_finding()
    marked_only.annotation_created = True

    report = SweepReport(started_at=datetime.now(timezone.utc),
                         findings=[filed, marked_only, a_finding()])
    assert report.incidents_created == 1
    assert report.annotations_created == 2
    assert report.to_dict()["annotations_created"] == 2

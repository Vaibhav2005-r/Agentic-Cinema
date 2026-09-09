"""Response shapes taken from a real Grafana Cloud stack.

Every case here is a bug a live run found and offline tests had missed: the
fixtures were written from my assumptions about mcp-grafana's responses, so
they agreed with the code rather than with the server.
"""

from __future__ import annotations

import pytest

from slo_watchdog.discovery import _as_list, find_datasources

#: Verbatim from https://happymousse3570.grafana.net, trimmed. Note the
#: envelope key, and that three of these are Loki.
REAL_DATASOURCES = {
    "datasources": [
        {"id": 1, "uid": "grafanacloud-alert-state-history",
         "name": "grafanacloud-x-alert-state-history", "type": "loki", "isDefault": False},
        {"id": 2, "uid": "grafanacloud-cardinality-management",
         "name": "grafanacloud-x-cardinality-management",
         "type": "grafanacloud-cardinality-datasource", "isDefault": False},
        {"id": 7, "uid": "grafanacloud-logs", "name": "grafanacloud-x-logs",
         "type": "loki", "isDefault": False},
        {"id": 8, "uid": "grafanacloud-prom", "name": "grafanacloud-x-prom",
         "type": "prometheus", "isDefault": True},
        {"id": 10, "uid": "grafanacloud-traces", "name": "grafanacloud-x-traces",
         "type": "tempo", "isDefault": False},
    ]
}


class Caller:
    def __init__(self, payload):
        self.payload = payload

    async def call(self, name, arguments):
        return self.payload


# --- response envelopes ----------------------------------------------------


def test_the_datasources_envelope_is_unwrapped():
    """list_datasources returns {"datasources": [...]}, not a bare list.

    Missing this made every sweep report "no Prometheus datasource found".
    """
    assert len(_as_list(REAL_DATASOURCES)) == 5


@pytest.mark.parametrize(
    "payload",
    [
        [1, 2],
        {"result": [1, 2]},
        {"data": [1, 2]},
        {"dashboards": [1, 2]},
        {"metrics": [1, 2]},
    ],
)
def test_known_envelopes_are_unwrapped(payload):
    assert _as_list(payload) == [1, 2]


def test_an_unknown_single_list_envelope_is_still_unwrapped():
    """Rather than fail on the next key mcp-grafana invents."""
    assert _as_list({"somethingNew": [1, 2]}) == [1, 2]


def test_an_ambiguous_dict_is_left_alone():
    """Two lists means we cannot know which one was meant."""
    payload = {"a": [1], "b": [2]}
    assert _as_list(payload) == [payload]


def test_empty_and_none_are_empty():
    assert _as_list(None) == []
    assert _as_list([]) == []


# --- datasource selection --------------------------------------------------


async def test_the_prometheus_datasource_is_found():
    found = await find_datasources(Caller(REAL_DATASOURCES))
    assert found.prometheus_uid == "grafanacloud-prom"


async def test_alert_state_history_is_not_mistaken_for_application_logs():
    """It sorts first by id and contains no service logs at all.

    Picking it would leave stage 4 correlating against an empty datasource
    while appearing to work.
    """
    found = await find_datasources(Caller(REAL_DATASOURCES))
    assert found.loki_uid == "grafanacloud-logs"


async def test_the_default_prometheus_wins_when_there_are_several():
    payload = {"datasources": [
        {"uid": "prom-a", "name": "a", "type": "prometheus", "isDefault": False},
        {"uid": "prom-b", "name": "b", "type": "prometheus", "isDefault": True},
    ]}
    assert (await find_datasources(Caller(payload))).prometheus_uid == "prom-b"


async def test_a_stack_with_no_prometheus_reports_none():
    payload = {"datasources": [{"uid": "l", "name": "l", "type": "loki"}]}
    found = await find_datasources(Caller(payload))
    assert found.prometheus_uid is None
    assert found.loki_uid == "l"


async def test_datasource_names_are_recorded_for_reporting():
    found = await find_datasources(Caller(REAL_DATASOURCES))
    assert found.names["grafanacloud-prom"] == "grafanacloud-x-prom"

"""Integration tests against the real mcp-grafana binary.

These need `mcp-grafana` on PATH but NOT a working Grafana: the server
registers its tools statically and advertises them before any upstream call
succeeds. That is enough to verify the two things offline tests cannot --
that every tool the sweep names actually exists, and that every argument we
send is one the server accepts.

    brew install mcp-grafana

Skipped automatically when the binary is absent.
"""

from __future__ import annotations

import shutil

import pytest

from slo_watchdog import tools
from slo_watchdog.mcp_client import ENABLED_TOOL_CATEGORIES, GrafanaConfig, open_session

pytestmark = pytest.mark.skipif(
    shutil.which("mcp-grafana") is None, reason="mcp-grafana not installed"
)


@pytest.fixture(scope="module")
def config():
    # Credentials are deliberately fake: tool registration does not need them.
    return GrafanaConfig(url="http://localhost:3000", service_account_token="dummy")


@pytest.fixture(scope="module")
async def advertised(config):
    async with open_session(config) as caller:
        listing = await caller.session.list_tools()
        return {t.name: (t.inputSchema or {}) for t in listing.tools}


async def test_the_server_starts_and_advertises_tools(advertised):
    assert len(advertised) > 20


async def test_every_required_tool_exists(advertised):
    missing = [t for t in tools.REQUIRED_TOOLS if t not in advertised]
    assert missing == [], f"missing from --enabled-tools={ENABLED_TOOL_CATEGORIES}: {missing}"


async def test_every_optional_tool_exists(advertised):
    """Optional to the sweep, but a wrong *name* is still a bug."""
    missing = [t for t in tools.OPTIONAL_TOOLS if t not in advertised]
    assert missing == [], f"tool names that do not exist: {missing}"


def _properties(schema: dict) -> set[str]:
    return set((schema or {}).get("properties", {}))


async def test_query_prometheus_accepts_every_argument_we_send(advertised):
    """The bug that would have killed the project: endTime is required."""
    props = _properties(advertised[tools.QUERY_PROMETHEUS])
    assert _properties({"properties": dict.fromkeys(tools.instant_query("u", "up"))}) <= props
    assert "endTime" in props
    required = set(advertised[tools.QUERY_PROMETHEUS].get("required", []))
    assert required <= set(tools.instant_query("u", "up")), (
        f"instant_query omits required argument(s): "
        f"{required - set(tools.instant_query('u', 'up'))}"
    )


async def test_range_query_satisfies_the_schema(advertised):
    props = _properties(advertised[tools.QUERY_PROMETHEUS])
    assert set(tools.range_query("u", "up", start="now-1h")) <= props


async def test_label_values_arguments_are_accepted(advertised):
    props = _properties(advertised[tools.LIST_LABEL_VALUES])
    assert set(tools.label_values("u", "service_name", "some_metric")) <= props


async def test_metric_names_arguments_are_accepted(advertised):
    props = _properties(advertised[tools.LIST_METRIC_NAMES])
    assert set(tools.metric_names("u", regex="grafana_slo.*")) <= props


async def test_loki_arguments_are_accepted(advertised):
    """Loki takes `logql`, not `expr` -- an easy and silent mistake."""
    for tool in (tools.QUERY_LOKI_LOGS, tools.QUERY_LOKI_PATTERNS):
        props = _properties(advertised[tool])
        sent = set(tools.loki_query("u", '{a="b"}', start="now-3d"))
        # query_loki_patterns takes no limit.
        assert sent - {"limit"} <= props, f"{tool} rejects {sent - props}"
        assert "logql" in props and "expr" not in props


async def test_annotation_arguments_are_accepted(advertised):
    props = _properties(advertised[tools.CREATE_ANNOTATION])
    assert set(tools.annotation("d", "text", 1, 2, panel_id=3)) <= props


async def test_incident_arguments_are_accepted(advertised):
    props = _properties(advertised[tools.CREATE_INCIDENT])
    assert set(tools.incident("title")) <= props
    required = set(advertised[tools.CREATE_INCIDENT].get("required", []))
    assert required <= set(tools.incident("title")), (
        f"incident() omits required argument(s): {required - set(tools.incident('title'))}"
    )


async def test_deeplink_arguments_are_accepted(advertised):
    props = _properties(advertised[tools.GENERATE_DEEPLINK])
    assert set(tools.deeplink_dashboard("uid", {"from": "now-3d", "to": "now"})) <= props


async def test_every_mutating_tool_on_the_server_is_gated(advertised):
    """The safety property that matters.

    An extra name in WRITE_TOOLS is harmless; a mutating tool the gate does
    not recognise is a dry run that quietly writes to someone's Grafana.
    """
    from slo_watchdog.mcp_client import is_write_tool

    mutating = {
        name for name in advertised
        if name.split("_")[0] in {"create", "update", "delete", "add"}
        or name.startswith("alerting_manage")
    }
    ungated = {name for name in mutating if not is_write_tool(name)}
    assert ungated == set(), f"mutating tools the dry-run gate would let through: {ungated}"


async def test_the_read_tools_the_sweep_needs_are_not_gated(advertised):
    """Over-blocking would break the investigation instead of the writes."""
    from slo_watchdog.mcp_client import is_write_tool

    for name in (tools.QUERY_PROMETHEUS, tools.QUERY_LOKI_LOGS,
                 tools.QUERY_LOKI_PATTERNS, tools.LIST_DATASOURCES,
                 tools.SEARCH_DASHBOARDS, tools.GENERATE_DEEPLINK,
                 tools.GET_PANEL_QUERIES):
        assert not is_write_tool(name), f"{name} must stay callable in a dry run"

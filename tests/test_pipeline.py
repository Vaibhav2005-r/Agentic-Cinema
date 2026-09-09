"""Stages 1-2 end to end against a fake MCP server.

No Grafana, no network, no LLM -- this is the Phase 1 exit criterion.
"""

from __future__ import annotations

import re

import pytest

from slo_watchdog import tools
from slo_watchdog.detect import DetectionSettings, describe, detect
from slo_watchdog.discovery import discover
from slo_watchdog.mcp_client import ReplayToolCaller

SLO = 0.999


def ratio_for_burn(burn: float, target: float = SLO) -> float:
    return burn * (1.0 - target)


class FakeGrafana:
    """A minimal mcp-grafana stand-in driven by a per-service burn profile."""

    def __init__(self, services: dict[str, dict], defined_slos: dict[str, float] | None = None):
        self.services = services
        self.defined_slos = defined_slos or {}
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name: str, arguments: dict):
        self.calls.append((name, arguments))

        if name == tools.LIST_DATASOURCES:
            return [
                {"uid": "prom-1", "type": "prometheus", "name": "Metrics"},
                {"uid": "loki-1", "type": "loki", "name": "Logs"},
            ]

        if name == tools.LIST_METRIC_NAMES:
            if arguments.get("regex", "").startswith("grafana_slo"):
                return ["grafana_slo_objective"] if self.defined_slos else []
            return ["http_server_request_duration_seconds_count"]

        if name == tools.LIST_LABEL_VALUES:
            return sorted(self.services)

        if name == tools.SEARCH_DASHBOARDS:
            return [{"uid": "dash-pay", "title": "Payment Service"}]

        if name == tools.QUERY_PROMETHEUS:
            return self._prometheus(arguments["expr"])

        return None

    def _prometheus(self, expr: str):
        if expr == "grafana_slo_info":
            return [{"metric": {"grafana_slo_uuid": f"u{i}",
                                "grafana_slo_name": f"{svc} availability"}, "value": [1, "1"]}
                    for i, svc in enumerate(self.defined_slos)]
        if expr == "grafana_slo_objective":
            return [{"metric": {"grafana_slo_uuid": f"u{i}"}, "value": [1, str(t)]}
                    for i, t in enumerate(self.defined_slos.values())]

        match = re.search(r'service_name="([^"]+)"', expr)
        if not match:
            return []
        profile = self.services.get(match.group(1))
        if profile is None:
            return []

        window = re.search(r"\[(\w+)\]", expr).group(1)
        if expr.startswith("sum(increase("):  # request-count query
            return [[1, str(profile.get("count", 500_000))]]

        burn = profile["windows"].get(window, profile["default"])
        return [[1, str(ratio_for_burn(burn))]]


HEALTHY = {"default": 0.1, "windows": {}}
SLOW_BURN = {"default": 2.3, "windows": {}}
PAGING = {"default": 15.0, "windows": {}}
RED_HERRING = {  # burnt over long windows, recovered on every short one
    "default": 8.0,
    "windows": {"5m": 0.05, "30m": 0.05, "2h": 0.05, "6h": 0.05},
}
THIN_TRAFFIC = {"default": 5.0, "windows": {}, "count": 40}


@pytest.mark.asyncio
async def test_discovery_builds_an_inventory():
    fake = FakeGrafana({"frontend": HEALTHY, "paymentservice": SLOW_BURN})
    inv = await discover(fake)
    assert inv.datasources.prometheus_uid == "prom-1"
    assert inv.datasources.loki_uid == "loki-1"
    assert [s.name for s in inv.services] == ["frontend", "paymentservice"]
    assert inv.errors == []


@pytest.mark.asyncio
async def test_services_without_an_slo_are_marked_provisional():
    fake = FakeGrafana({"frontend": HEALTHY, "paymentservice": SLOW_BURN})
    inv = await discover(fake)
    assert all(s.provisional for s in inv.services)
    assert inv.provisional_count == 2
    assert "2 provisional" in inv.summary()


@pytest.mark.asyncio
async def test_defined_slo_overrides_the_provisional_default():
    fake = FakeGrafana(
        {"frontend": HEALTHY, "paymentservice": SLOW_BURN},
        defined_slos={"paymentservice": 0.995},
    )
    inv = await discover(fake)
    payment = next(s for s in inv.services if s.name == "paymentservice")
    assert not payment.provisional
    assert payment.slo.target == 0.995
    assert payment.slo.source == "grafana-slo"


@pytest.mark.asyncio
async def test_dashboard_is_matched_to_its_service():
    fake = FakeGrafana({"paymentservice": SLOW_BURN})
    inv = await discover(fake)
    assert inv.services[0].dashboard_uid == "dash-pay"


@pytest.mark.asyncio
async def test_the_sweep_finds_the_slow_burn_and_ignores_the_healthy_service():
    fake = FakeGrafana({"frontend": HEALTHY, "paymentservice": SLOW_BURN})
    candidates = await detect(fake, await discover(fake))
    assert [c.service for c in candidates] == ["paymentservice"]
    assert candidates[0].burn_rate == pytest.approx(2.3)
    assert candidates[0].tier.name == "watchdog"


@pytest.mark.asyncio
async def test_the_sweep_stays_quiet_about_the_red_herring():
    """A spike that already recovered produces nothing."""
    fake = FakeGrafana({"cartservice": RED_HERRING})
    assert await detect(fake, await discover(fake)) == []


@pytest.mark.asyncio
async def test_paging_tiers_are_excluded_by_default():
    """Conventional alerts own these; the watchdog explains what they ignore."""
    fake = FakeGrafana({"checkoutservice": PAGING, "paymentservice": SLOW_BURN})
    inv = await discover(fake)
    assert [c.service for c in await detect(fake, inv)] == ["paymentservice"]

    everything = await detect(fake, inv, DetectionSettings(include_paging_tiers=True))
    assert {c.service for c in everything} == {"checkoutservice", "paymentservice"}


@pytest.mark.asyncio
async def test_thin_traffic_is_dropped_before_it_becomes_a_finding():
    fake = FakeGrafana({"emailservice": THIN_TRAFFIC})
    assert await detect(fake, await discover(fake)) == []


@pytest.mark.asyncio
async def test_describe_renders_a_ranked_table():
    fake = FakeGrafana({"frontend": HEALTHY, "paymentservice": SLOW_BURN})
    text = describe(await detect(fake, await discover(fake)))
    assert "paymentservice" in text and "watchdog" in text and "provisional" in text


@pytest.mark.asyncio
async def test_describe_says_so_when_everything_is_healthy():
    fake = FakeGrafana({"frontend": HEALTHY})
    assert "inside its budget" in describe(await detect(fake, await discover(fake)))


@pytest.mark.asyncio
async def test_a_broken_datasource_list_degrades_instead_of_crashing():
    class NoDatasources(FakeGrafana):
        async def call(self, name, arguments):
            if name == tools.LIST_DATASOURCES:
                return []
            return await super().call(name, arguments)

    inv = await discover(NoDatasources({}))
    assert inv.errors and "Prometheus" in inv.errors[0]
    assert await detect(NoDatasources({}), inv) == []


# --- fixtures: record once, replay forever ---------------------------------


@pytest.mark.asyncio
async def test_replay_reproduces_a_recorded_sweep(tmp_path):
    from slo_watchdog.mcp_client import FixtureRecorder

    fake = FakeGrafana({"frontend": HEALTHY, "paymentservice": SLOW_BURN})
    recorder = FixtureRecorder(path=tmp_path / "golden.json")

    class Recording:
        async def call(self, name, arguments):
            payload = await fake.call(name, arguments)
            recorder.record(name, arguments, payload)
            return payload

    live = Recording()
    live_result = await detect(live, await discover(live))
    recorder.save()

    replay = ReplayToolCaller.from_file(tmp_path / "golden.json", strict=True)
    replay_result = await detect(replay, await discover(replay))

    assert [c.service for c in replay_result] == [c.service for c in live_result]
    assert replay_result[0].burn_rate == pytest.approx(live_result[0].burn_rate)
    assert replay.misses == []


# --- dashboard binding -----------------------------------------------------


def test_dashboard_matching_prefers_exact_then_longest():
    from slo_watchdog.discovery import _match_dashboard

    dashboards = {
        "paymentservice": ("uid-exact", "Payment Service"),
        "paymentserviceoverview": ("uid-long", "Payment Service Overview"),
    }
    assert _match_dashboard("paymentservice", dashboards)[0] == "uid-exact"


def test_dashboard_matching_is_not_order_dependent():
    from slo_watchdog.discovery import _match_dashboard

    forward = {"cartservicedetail": ("uid-long", "Cart Service Detail"),
               "cartserv": ("uid-short", "Cart Serv")}
    backward = dict(reversed(list(forward.items())))
    assert (_match_dashboard("cartservicedetail", forward)
            == _match_dashboard("cartservicedetail", backward))


def test_a_short_service_name_does_not_grab_an_unrelated_dashboard():
    """"ad" must not bind to the adservice dashboard."""
    from slo_watchdog.discovery import _match_dashboard

    assert _match_dashboard("ad", {"adservice": ("uid-ad", "Ad Service")}) == (None, None)


def test_a_service_with_no_dashboard_gets_none():
    from slo_watchdog.discovery import _match_dashboard

    assert _match_dashboard("quoteservice", {"frontend": ("uid-f", "Frontend")}) == (None, None)

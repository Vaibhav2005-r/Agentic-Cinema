#!/usr/bin/env python3
"""Generate fixtures/golden-sweep.json -- a recorded sweep that runs offline.

Record a golden run once and the demo video never depends on live chaos
cooperating, the tests never need a network, and a judge can clone the repo and
see a real sweep without a Grafana account:

    slo-watchdog sweep --replay fixtures/golden-sweep.json

This models the OpenTelemetry demo's service list: eleven services, SLOs
defined on four of them, and three deliberate outcomes -- one slow burn, one
provisional finding, and one red herring the agent must stay quiet about.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slo_watchdog import tools  # noqa: E402
from slo_watchdog.detect import describe, detect  # noqa: E402
from slo_watchdog.discovery import discover  # noqa: E402
from slo_watchdog.mcp_client import FixtureRecorder  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "fixtures" / "golden-sweep.json"

SLO = 0.999

#: service -> (burn rate by window, request volume)
HEALTHY = {"default": 0.08, "windows": {}}

SERVICES: dict[str, dict] = {
    "frontend": {**HEALTHY, "count": 2_400_000},
    "checkoutservice": {**HEALTHY, "count": 310_000},
    "productcatalogservice": {**HEALTHY, "count": 1_900_000},
    "shippingservice": {**HEALTHY, "count": 290_000},
    "currencyservice": {**HEALTHY, "count": 1_100_000},
    "emailservice": {**HEALTHY, "count": 96_000},
    "adservice": {**HEALTHY, "count": 640_000},
    "quoteservice": {**HEALTHY, "count": 180_000},

    # The hero finding: a 2.3x burn on a service that has a real SLO.
    # Nothing pages. The budget is two-thirds gone.
    "paymentservice": {"default": 2.3, "windows": {}, "count": 305_000},

    # The provisional finding: no SLO defined, so the target is derived.
    "recommendationservice": {"default": 1.8, "windows": {}, "count": 720_000},

    # The red herring: badly burnt over the long windows, fully recovered on
    # every short one. The agent must report nothing.
    "cartservice": {
        "default": 9.5,
        "windows": {"5m": 0.05, "30m": 0.05, "2h": 0.04, "6h": 0.06},
        "count": 880_000,
    },
}

#: Four services have human-defined SLOs; the other seven do not.
DEFINED_SLOS = {
    "frontend": 0.995,
    "checkoutservice": 0.999,
    "cartservice": 0.999,
    "paymentservice": 0.999,
}

#: 30-day consumption, so budget-remaining is a real number in the report.
BUDGET_BURN_30D = {
    "paymentservice": 0.66,        # 34% of the budget left
    "recommendationservice": 0.41,  # 59% left
}


class GoldenStack:
    """A believable mcp-grafana, shaped like the OpenTelemetry demo."""

    async def call(self, name: str, arguments: dict):
        if name == tools.LIST_DATASOURCES:
            return [
                {"uid": "grafanacloud-prom", "type": "prometheus", "name": "grafanacloud-metrics"},
                {"uid": "grafanacloud-logs", "type": "loki", "name": "grafanacloud-logs"},
                {"uid": "grafanacloud-traces", "type": "tempo", "name": "grafanacloud-traces"},
            ]

        if name == tools.LIST_METRIC_NAMES:
            if arguments.get("regex", "").startswith("grafana_slo"):
                return ["grafana_slo_objective", "grafana_slo_error_budget_remaining"]
            return [
                "http_server_request_duration_seconds_count",
                "http_server_request_duration_seconds_bucket",
                "http_server_request_duration_seconds_sum",
                "rpc_server_duration_milliseconds_count",
                "target_info",
                "up",
            ]

        if name == tools.LIST_LABEL_VALUES:
            return sorted(SERVICES)

        if name == tools.SEARCH_DASHBOARDS:
            return [
                {"uid": "otel-demo-frontend", "title": "Frontend"},
                {"uid": "otel-demo-payment", "title": "Payment Service"},
                {"uid": "otel-demo-cart", "title": "Cart Service"},
                {"uid": "otel-demo-checkout", "title": "Checkout Service"},
                {"uid": "otel-demo-recs", "title": "Recommendation Service"},
            ]

        if name == tools.QUERY_PROMETHEUS:
            return self._prometheus(arguments["expr"])
        return None

    def _prometheus(self, expr: str):
        if "grafana_slo" in expr:
            return [
                {"metric": {"service": svc}, "value": [1_757_000_000, str(target)]}
                for svc, target in DEFINED_SLOS.items()
            ]

        match = re.search(r'service_name="([^"]+)"', expr)
        if not match:
            return []
        service = match.group(1)
        profile = SERVICES.get(service)
        if profile is None:
            return []

        window = re.search(r"\[(\w+)\]", expr).group(1)

        # A bare sum(increase(...)) with no error selector is the denominator.
        if expr.startswith("sum(increase(") and "status_code" not in expr:
            return [[1_757_000_000, str(profile["count"])]]

        if window == "30d":
            burn = BUDGET_BURN_30D.get(service, profile["default"] * 0.4)
        else:
            burn = profile["windows"].get(window, profile["default"])
        return [[1_757_000_000, str(burn * (1.0 - SLO))]]


async def main() -> int:
    stack = GoldenStack()
    recorder = FixtureRecorder(path=OUT)

    class Recording:
        async def call(self, name, arguments):
            payload = await stack.call(name, arguments)
            recorder.record(name, arguments, payload)
            return payload

    caller = Recording()
    inventory = await discover(caller)
    candidates = await detect(caller, inventory)

    recorder.save()

    print(f"recorded {len(recorder.entries)} MCP responses -> {OUT}")
    print()
    print(inventory.summary())
    print()
    print(describe(candidates))
    print()
    quiet = sorted(set(SERVICES) - {c.service for c in candidates})
    print(f"stayed quiet about {len(quiet)} services, including the red herring "
          f"(cartservice): {'cartservice' in quiet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

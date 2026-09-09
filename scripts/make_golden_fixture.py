#!/usr/bin/env python3
"""Generate fixtures/golden-sweep.json -- a recorded sweep that runs offline.

Record a golden run once and the demo never depends on live chaos cooperating,
the tests never need a network, and a judge can clone the repo and see a real
sweep without a Grafana account:

    slo-watchdog sweep --replay fixtures/golden-sweep.json

This models the mediastack simulator: eleven services across playback, DRM,
CDN, post-production and VFX, with SLOs defined on four of them and three
deliberate outcomes -- one slow burn, one provisional finding, and one red
herring the agent must stay quiet about.
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

HEALTHY = {"default": 0.08, "windows": {}}

#: service -> (metric family, burn profile, volume)
SERVICES: dict[str, dict] = {
    "playback-api":     {"metric": "playback_session_start_total", **HEALTHY, "count": 2_100_000},
    "cdn-edge":         {"metric": "segment_request_total",        **HEALTHY, "count": 9_400_000},
    "manifest-service": {"metric": "http_server_request_duration_seconds_count", **HEALTHY, "count": 1_800_000},
    "catalog-api":      {"metric": "http_server_request_duration_seconds_count", **HEALTHY, "count": 1_200_000},
    "entitlements":     {"metric": "http_server_request_duration_seconds_count", **HEALTHY, "count": 880_000},
    "search-api":       {"metric": "http_server_request_duration_seconds_count", **HEALTHY, "count": 760_000},
    "recommendations":  {"metric": "http_server_request_duration_seconds_count", **HEALTHY, "count": 690_000},
    "render-farm":      {"metric": "render_task_total",            **HEALTHY, "count": 140_000},

    # The hero finding: DRM denials at 2.3x on a service that has a real SLO.
    # Nothing pages. One viewer in 435 is refused content they paid for.
    "drm-license": {"metric": "drm_license_request_total", "default": 2.3,
                    "windows": {}, "count": 1_740_000},

    # The provisional finding: nobody writes an SLO for subtitles, which is
    # exactly why this went unnoticed. It is also an accessibility failure.
    "subtitle-service": {"metric": "subtitle_fetch_total",
                         "default": 1.8, "windows": {}, "count": 620_000},

    # The red herring: a transcode batch that failed hard for an hour and then
    # recovered. Burnt over 3d, clean over 6h. The agent must stay quiet.
    "transcode-worker": {"metric": "transcode_job_total", "default": 9.5,
                         "windows": {"5m": 0.05, "30m": 0.05, "2h": 0.04, "6h": 0.06},
                         "count": 96_000},
}

#: Four services carry human-defined SLOs; the other seven carry none.
DEFINED_SLOS = {
    "playback-api": 0.999,
    "drm-license": 0.999,
    "cdn-edge": 0.9995,
    "manifest-service": 0.999,
}

#: 30-day consumption, so budget-remaining is a real number in the report.
BUDGET_BURN_30D = {
    "drm-license": 0.66,       # 34% of the budget left
    "subtitle-service": 0.41,  # 59% left
}

DASHBOARDS = [
    {"uid": "media-playback", "title": "Playback API"},
    {"uid": "media-drm", "title": "DRM License"},
    {"uid": "media-cdn", "title": "CDN Edge"},
    {"uid": "media-transcode", "title": "Transcode Worker"},
    {"uid": "media-subtitle", "title": "Subtitle Service"},
]


class GoldenStack:
    """A believable mcp-grafana, shaped like the mediastack simulator."""

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
            return sorted({s["metric"] for s in SERVICES.values()}) + ["target_info", "up"]

        if name == tools.LIST_LABEL_VALUES:
            # `matches` is a list of Selector objects; pull the metric out of it
            # so each profile only sees the services that report it.
            metric = None
            for sel in arguments.get("matches", []):
                for f in sel.get("filters", []):
                    if f.get("name") == "__name__":
                        metric = f.get("value")
            if metric is None:
                return sorted(SERVICES)
            return sorted(n for n, s in SERVICES.items() if s["metric"] == metric)

        if name == tools.SEARCH_DASHBOARDS:
            return DASHBOARDS

        if name == tools.QUERY_PROMETHEUS:
            return self._prometheus(arguments["expr"])
        return None

    def _prometheus(self, expr: str):
        if expr == "grafana_slo_info":
            return [{"metric": {"grafana_slo_uuid": f"u{i}",
                                "grafana_slo_name": f"{svc} availability"},
                     "value": [1_757_000_000, "1"]}
                    for i, svc in enumerate(DEFINED_SLOS)]
        if expr == "grafana_slo_objective":
            return [{"metric": {"grafana_slo_uuid": f"u{i}"},
                     "value": [1_757_000_000, str(t)]}
                    for i, t in enumerate(DEFINED_SLOS.values())]

        match = re.search(r'service_name="([^"]+)"', expr)
        if not match:
            return []
        service = match.group(1)
        profile = SERVICES.get(service)
        if profile is None:
            return []

        # Only answer for the metric family this service actually reports.
        if profile["metric"] not in expr:
            return []

        window = re.search(r"\[(\w+)\]", expr).group(1)

        # A bare sum(increase(...)) with no error selector is the denominator.
        if expr.startswith("sum(increase(") and not any(
            k in expr for k in ("outcome", "status_code")
        ):
            # Scale volume with the window, as a real counter does. A flat
            # count makes every window look equally covered and the budget
            # figure gets withheld for the wrong reason.
            from slo_watchdog.burn_rate import parse_duration

            share = parse_duration(window) / parse_duration("3d")
            return [[1_757_000_000, str(round(profile["count"] * share))]]

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
    print(describe(candidates, inventory))
    print()
    quiet = sorted(set(SERVICES) - {c.service for c in candidates})
    print(f"stayed quiet about {len(quiet)} services, including the red herring "
          f"(transcode-worker): {'transcode-worker' in quiet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""A synthetic media platform that emits OTLP metrics and logs to Grafana Cloud.

This replaces the OpenTelemetry demo webstore for three reasons:

  1. Domain. The watchdog watches playback, DRM, CDN, transcode and render
     traffic -- an astronomy shop tells the wrong story.
  2. Cardinality. The free tier allows 10,000 active series; this stack emits
     roughly 60, so the whole demo fits inside it with room to spare.
  3. No Docker. It is one Python process.

Run it alongside the watchdog:

    python mediastack/simulate.py --rate 40

Then inject a failure and sweep:

    slo-watchdog chaos drm_slow_burn
    slo-watchdog sweep
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

log = logging.getLogger("mediastack")

SCENARIOS_FILE = Path(__file__).resolve().parent / "scenarios.json"


@dataclass
class Service:
    """One media service and the metric family it reports."""

    name: str
    metric: str
    outcomes: tuple[str, ...]
    failure_outcomes: tuple[str, ...]
    #: Share of overall traffic this service sees.
    weight: float
    #: Healthy background failure rate. Never zero -- a perfectly clean service
    #: is not a realistic baseline and hides the confidence check.
    baseline: float = 0.0002
    log_template: str = "{outcome} on {service}"
    counter: Any = field(default=None, repr=False)


#: Eleven services, mirroring how a streaming platform and its post pipeline
#: are actually split. Only four will get real SLOs -- see README.
SERVICES: list[Service] = [
    Service("playback-api", "playback_session_start", ("success", "error", "timeout"),
            ("error", "timeout"), weight=3.0,
            log_template="playback start {outcome} manifest_ms=<n> cdn=<pop>"),
    Service("drm-license", "drm_license_request", ("success", "denied", "error", "timeout"),
            ("denied", "error", "timeout"), weight=2.4,
            log_template="license {outcome} key_id=<id> policy=<policy>"),
    Service("cdn-edge", "segment_request", ("200", "404", "500", "503"),
            ("500", "503"), weight=8.0,
            log_template="segment {outcome} pop=<pop> bytes=<n>"),
    Service("manifest-service", "http_request",
            ("200", "500"), ("500",), weight=2.0),
    Service("catalog-api", "http_request",
            ("200", "500"), ("500",), weight=1.6),
    Service("entitlements", "http_request",
            ("200", "500"), ("500",), weight=1.2),
    Service("subtitle-service", "subtitle_fetch", ("success", "missing", "error"),
            ("missing", "error"), weight=0.9,
            log_template="subtitle fetch {outcome} lang=<lang> asset=<id>"),
    Service("search-api", "http_request",
            ("200", "500"), ("500",), weight=1.1),
    Service("recommendations", "http_request",
            ("200", "500"), ("500",), weight=1.0),
    Service("transcode-worker", "transcode_job", ("completed", "failed", "aborted"),
            ("failed", "aborted"), weight=0.35,
            log_template="transcode {outcome} asset=<id> profile=<profile>"),
    Service("render-farm", "render_task", ("completed", "failed", "timeout"),
            ("failed", "timeout"), weight=0.5,
            log_template="render {outcome} shot=<shot> frame=<n>"),
]


def build_providers(endpoint: str, headers: dict[str, str], interval_ms: int):
    """Configure the metric and log providers.

    Providers first, then anything that uses them -- the SDK silently discards
    telemetry produced before a provider is installed.
    """
    from opentelemetry import metrics as otel_metrics
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource

    resource = Resource.create({"service.name": "mediastack", "service.version": "0.1.0"})
    reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics", headers=headers),
        export_interval_millis=interval_ms,
    )
    otel_metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))
    return otel_metrics.get_meter("mediastack")


def load_injections(path: Path) -> dict[str, dict]:
    """Re-read the scenario file each tick so `chaos` takes effect live."""
    try:
        return json.loads(path.read_text()).get("injected", {})
    except (OSError, json.JSONDecodeError):
        return {}


def failure_rate(service: Service, injected: dict[str, dict], now: float,
                 started: dict[str, float]) -> float:
    entry = injected.get(service.name)
    if not entry:
        started.pop(service.name, None)
        return service.baseline

    began = started.setdefault(service.name, now)
    duration = entry.get("duration_seconds")
    if duration and (now - began) > duration:
        # The red herring: recovered on its own, which is precisely what the
        # short window is there to notice.
        return service.baseline
    return float(entry.get("error_fraction", service.baseline))


def run(rate: float, endpoint: str, headers: dict[str, str], interval_ms: int,
        once: bool = False) -> int:
    meter = build_providers(endpoint, headers, interval_ms)
    for service in SERVICES:
        service.counter = meter.create_counter(
            service.metric,
            description=f"{service.name} events by outcome",
            unit="1",
        )

    total_weight = sum(s.weight for s in SERVICES)
    started: dict[str, float] = {}
    tick = 0

    print(f"mediastack: {len(SERVICES)} services, ~{rate:.0f} events/s -> {endpoint}")
    print(f"scenarios : {SCENARIOS_FILE}")

    while True:
        tick += 1
        now = time.time()
        injected = load_injections(SCENARIOS_FILE)

        for service in SERVICES:
            events = max(1, round(rate * (service.weight / total_weight)))
            bad_rate = failure_rate(service, injected, now, started)
            failures = sum(1 for _ in range(events) if random.random() < bad_rate)

            good_outcome = service.outcomes[0]
            attrs_base = {"service_name": service.name}

            if events - failures:
                service.counter.add(
                    events - failures, {**attrs_base, "outcome": good_outcome}
                )
            for _ in range(failures):
                outcome = random.choice(service.failure_outcomes)
                service.counter.add(1, {**attrs_base, "outcome": outcome})
                log.warning(service.log_template.format(
                    outcome=outcome, service=service.name))

        if tick % 10 == 0:
            active = ", ".join(injected) or "none"
            print(f"  tick {tick:>5}  injected: {active}")
        if once:
            return 0
        time.sleep(1.0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=float, default=40.0,
                        help="approximate events per second across all services")
    parser.add_argument("--interval", type=int, default=15000,
                        help="OTLP export interval in ms")
    parser.add_argument("--once", action="store_true", help="emit one tick and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    endpoint = os.environ.get("MEDIASTACK_OTLP_ENDPOINT") or os.environ.get(
        "AGENTO11Y_ENDPOINT", ""
    )
    if not endpoint:
        print("error: set MEDIASTACK_OTLP_ENDPOINT (or AGENTO11Y_ENDPOINT) to your "
              "Grafana Cloud OTLP gateway.", file=sys.stderr)
        return 1

    import base64

    user = os.environ.get("MEDIASTACK_OTLP_USER", "")
    token = os.environ.get("MEDIASTACK_OTLP_TOKEN", "")
    headers: dict[str, str] = {}
    if user and token:
        headers["Authorization"] = "Basic " + base64.b64encode(
            f"{user}:{token}".encode()
        ).decode()

    return run(args.rate, endpoint.rstrip("/"), headers, args.interval, args.once)


if __name__ == "__main__":
    raise SystemExit(main())

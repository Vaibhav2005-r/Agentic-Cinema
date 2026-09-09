"""The reflexive layer: an observability agent that is itself observable.

One root span per sweep, child spans per stage, and custom attributes that put
a number on screen -- `sweep_cost_usd` is concrete in a way that "it's
efficient" is not.

Two things this module is careful about:

  1. `TracerProvider` and `MeterProvider` are configured *before* the agento11y
     client is constructed. Without them the SDK silently discards everything.
  2. Nothing here is required. If AGENTO11Y_* is unset the whole module becomes
     a no-op, so the sweep still runs on a machine with no Grafana Cloud
     access-policy token.

Note on packages: install `agento11y` alone. `agento11y-gemini` pins
google-genai<2 while google-adk 2.8 requires >=2.19, so the two cannot coexist.
ADK already emits OpenTelemetry spans for its own LLM calls, which is the layer
we want instrumented anyway.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

log = logging.getLogger(__name__)

SERVICE_NAME = "slo-watchdog"

#: USD per million tokens, (input, output). Deliberately incomplete: a made-up
#: rate produces a confident wrong number on screen, which is worse than no
#: number. Unpriced models report None and the CLI says so. Add your model's
#: current rate here from https://ai.google.dev/pricing before quoting a cost.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
}


def estimate_cost_usd(
    model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """None when we have no published rate for this model."""
    rates = MODEL_PRICING.get(model)
    if rates is None:
        return None
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000


@dataclass
class SweepMetrics:
    """The numbers worth putting on screen at the end of the video."""

    candidates_detected: int = 0
    findings_confirmed: int = 0
    findings_dismissed: int = 0
    incidents_created: int = 0
    annotations_created: int = 0
    mcp_tool_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""

    @property
    def sweep_cost_usd(self) -> float | None:
        return estimate_cost_usd(self.model, self.prompt_tokens, self.completion_tokens)

    def as_attributes(self) -> dict[str, Any]:
        return {
            "slo_watchdog.candidates_detected": self.candidates_detected,
            "slo_watchdog.findings_confirmed": self.findings_confirmed,
            "slo_watchdog.findings_dismissed": self.findings_dismissed,
            "slo_watchdog.incidents_created": self.incidents_created,
            "slo_watchdog.annotations_created": self.annotations_created,
            "slo_watchdog.mcp_tool_calls": self.mcp_tool_calls,
            "slo_watchdog.prompt_tokens": self.prompt_tokens,
            "slo_watchdog.completion_tokens": self.completion_tokens,
            "gen_ai.request.model": self.model,
            **(
                {"slo_watchdog.sweep_cost_usd": round(self.sweep_cost_usd, 6)}
                if self.sweep_cost_usd is not None
                else {}
            ),
        }


@dataclass
class Telemetry:
    """Wraps OTel so the rest of the codebase never imports it conditionally."""

    enabled: bool = False
    tracer: Any = None
    client: Any = None
    metrics: SweepMetrics = field(default_factory=SweepMetrics)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Any]:
        if not self.enabled or self.tracer is None:
            yield None
            return
        with self.tracer.start_as_current_span(name) as current:
            for key, value in attributes.items():
                if value is not None:
                    current.set_attribute(key, value)
            yield current

    def count_tool_call(self, tool_name: str) -> None:
        """Record a tool call as an event on the enclosing stage span.

        An event, not a span: the call has not happened yet when this runs, so
        a zero-duration span here would misreport tool latency. ADK emits its
        own spans for the calls themselves.
        """
        self.metrics.mcp_tool_calls += 1
        if not self.enabled:
            return
        try:
            from opentelemetry import trace as otel_trace

            current = otel_trace.get_current_span()
            if current is not None:
                current.add_event("mcp.tool_call", {"mcp.tool.name": tool_name})
        except Exception:  # noqa: BLE001 - telemetry must never fail a sweep
            log.debug("failed to record tool call event", exc_info=True)

    def finish(self, root_span: Any) -> None:
        if root_span is None:
            return
        for key, value in self.metrics.as_attributes().items():
            root_span.set_attribute(key, value)

    def shutdown(self) -> None:
        for target, method in ((self.client, "shutdown"), (self.client, "flush")):
            if target is None:
                continue
            fn = getattr(target, method, None)
            if callable(fn):
                try:
                    fn()
                except Exception:  # noqa: BLE001 - telemetry must never fail a sweep
                    log.debug("telemetry %s failed", method, exc_info=True)


def _otlp_headers() -> dict[str, str]:
    """Grafana Cloud OTLP uses basic auth: instance ID as user, token as pass."""
    import base64

    tenant = os.environ.get("AGENTO11Y_AUTH_TENANT_ID", "").strip()
    token = os.environ.get("AGENTO11Y_AUTH_TOKEN", "").strip()
    if not (tenant and token):
        return {}
    encoded = base64.b64encode(f"{tenant}:{token}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def setup(agent_version: str = "0.1.0") -> Telemetry:
    """Configure providers, then the client. Order matters -- see module docs."""
    endpoint = os.environ.get("AGENTO11Y_ENDPOINT", "").strip()
    if not endpoint:
        log.debug("AGENTO11Y_ENDPOINT unset; telemetry disabled")
        return Telemetry(enabled=False)

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry import trace as otel_trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:
        log.warning("OpenTelemetry SDK missing (%s); telemetry disabled", exc)
        return Telemetry(enabled=False)

    resource = Resource.create(
        {"service.name": SERVICE_NAME, "service.version": agent_version}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            OTLPSpanExporter(
                endpoint=f"{endpoint.rstrip('/')}/v1/traces", headers=_otlp_headers()
            )
        )
    )
    otel_trace.set_tracer_provider(provider)

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(
                endpoint=f"{endpoint.rstrip('/')}/v1/metrics", headers=_otlp_headers()
            )
        )
        otel_metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader])
        )
    except ImportError:
        log.debug("metric exporter unavailable; traces only")

    tracer = otel_trace.get_tracer(SERVICE_NAME)

    # Only now, with providers installed, is it safe to build the client.
    client = None
    try:
        from agento11y import Client, ClientConfig

        client = Client(
            ClientConfig(
                agent_name=SERVICE_NAME,
                agent_version=agent_version,
                tracer=tracer,
                meter=otel_metrics.get_meter(SERVICE_NAME),
            )
        )
    except Exception as exc:  # noqa: BLE001 - the OTel spans are the primary signal
        log.warning("agento11y client unavailable (%s); exporting raw OTel only", exc)

    return Telemetry(enabled=True, tracer=tracer, client=client)

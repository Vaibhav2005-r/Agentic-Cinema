"""PromQL construction for SLI ratios.

The agent never writes PromQL. These builders do, deterministically, so every
query in a finding can be replayed by a human verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass



@dataclass(frozen=True)
class MetricProfile:
    """How to compute a RED-style SLI from one metric family.

    The OpenTelemetry demo emits both HTTP and gRPC server metrics; a generic
    Prometheus stack usually has neither in the same shape. Rather than guess,
    discovery probes each profile and keeps the ones that return series.
    """

    name: str
    total_metric: str
    service_label: str
    error_selector: str
    extra_selector: str = ""

    def selector(self, service: str, *, errors_only: bool = False) -> str:
        parts = [f'{self.service_label}="{service}"']
        if self.extra_selector:
            parts.append(self.extra_selector)
        if errors_only:
            parts.append(self.error_selector)
        return "{" + ", ".join(parts) + "}"


#: OTel semantic-convention HTTP server metrics, as exported to Prometheus.
OTEL_HTTP = MetricProfile(
    name="otel_http",
    total_metric="http_server_request_duration_seconds_count",
    service_label="service_name",
    error_selector='http_response_status_code=~"5.."',
)

#: OTel gRPC server metrics. status code 2 == ERROR in the gRPC status enum.
OTEL_GRPC = MetricProfile(
    name="otel_grpc",
    total_metric="rpc_server_duration_milliseconds_count",
    service_label="service_name",
    error_selector='rpc_grpc_status_code!="0"',
)

#: Classic Prometheus client_golang / promhttp style.
GENERIC_HTTP = MetricProfile(
    name="generic_http",
    total_metric="http_requests_total",
    service_label="job",
    error_selector='code=~"5.."',
)

PROFILES: tuple[MetricProfile, ...] = (OTEL_HTTP, OTEL_GRPC, GENERIC_HTTP)


def error_ratio_query(profile: MetricProfile, service: str, window: str) -> str:
    """Bad events / total events over `window`, as a single scalar.

    The `or vector(0)` guard makes a service with zero errors return 0 rather
    than an empty result, which would otherwise be indistinguishable from a
    service that stopped reporting.
    """
    bad = f"sum(increase({profile.total_metric}{profile.selector(service, errors_only=True)}[{window}]))"
    total = f"sum(increase({profile.total_metric}{profile.selector(service)}[{window}]))"
    return f"({bad} or vector(0)) / ({total} > 0)"


def request_count_query(profile: MetricProfile, service: str, window: str) -> str:
    """Denominator volume, used for the statistical confidence check."""
    return f"sum(increase({profile.total_metric}{profile.selector(service)}[{window}]))"


def service_discovery_query(profile: MetricProfile) -> str:
    """Every service currently reporting this metric family."""
    return f"count by ({profile.service_label}) ({profile.total_metric})"

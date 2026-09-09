"""PromQL construction for media-delivery and production SLIs.

The agent never writes PromQL. These builders do, deterministically, so every
query in a finding can be replayed by a human verbatim.

The metric families here are the ones a streaming platform and a post house
actually run on: playback session starts, DRM license issuance, CDN segment
delivery, transcode jobs and VFX render tasks. Each is a counter partitioned by
an `outcome` label, which is all a RED-style availability SLI needs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MetricProfile:
    """How to compute an availability SLI from one metric family.

    Discovery probes each profile and keeps the ones that return series, so a
    stack that only runs playback services is not asked about render farms.
    """

    name: str
    total_metric: str
    service_label: str
    error_selector: str
    #: What one failed event means to a person. Used to turn a burn rate into
    #: a sentence a producer or a duty manager can act on.
    unit: str = "request"
    impact: str = "a failed request"
    extra_selector: str = ""

    def selector(self, service: str, *, errors_only: bool = False) -> str:
        parts = [f'{self.service_label}="{service}"']
        if self.extra_selector:
            parts.append(self.extra_selector)
        if errors_only:
            parts.append(self.error_selector)
        return "{" + ", ".join(parts) + "}"


#: Playback start failures. The canonical streaming SLI: the viewer pressed
#: play and nothing happened. Rarely alerted on per-service, always noticed by
#: the audience.
PLAYBACK = MetricProfile(
    name="playback",
    total_metric="playback_session_start_total",
    service_label="service_name",
    error_selector='outcome!="success"',
    unit="playback start",
    impact="a viewer pressed play and got an error",
)

#: DRM license issuance. A textbook silent failure: a small denial rate looks
#: like nothing on a dashboard and looks like a broken product to the viewer.
DRM = MetricProfile(
    name="drm",
    total_metric="drm_license_request_total",
    service_label="service_name",
    error_selector='outcome=~"denied|error|timeout"',
    unit="license request",
    impact="a viewer was refused a licence for content they paid for",
)

#: CDN segment delivery. Drives rebuffering rather than hard failure.
SEGMENT = MetricProfile(
    name="segment",
    total_metric="segment_request_total",
    service_label="service_name",
    error_selector='outcome=~"5.."',
    unit="segment request",
    impact="a video segment failed to deliver, causing a rebuffer",
)

#: VOD transcode jobs. A failed job is a title that misses its release window.
TRANSCODE = MetricProfile(
    name="transcode",
    total_metric="transcode_job_total",
    service_label="service_name",
    error_selector='outcome=~"failed|aborted"',
    unit="transcode job",
    impact="an asset failed to encode and will miss its publish window",
)

#: VFX render farm tasks. A quiet failure rate here burns artist days.
RENDER = MetricProfile(
    name="render",
    total_metric="render_task_total",
    service_label="service_name",
    error_selector='outcome=~"failed|timeout"',
    unit="render task",
    impact="a frame failed to render and must be resubmitted",
)

#: Subtitle and caption delivery. Almost nobody defines an SLO for this, which
#: is exactly why it degrades unnoticed -- and a silent failure here is an
#: accessibility failure, not a cosmetic one.
SUBTITLE = MetricProfile(
    name="subtitle",
    total_metric="subtitle_fetch_total",
    service_label="service_name",
    error_selector='outcome!="success"',
    unit="subtitle fetch",
    impact="a viewer who needs captions was served none (an accessibility failure)",
)

#: A plain HTTP request counter. OpenTelemetry appends `_total` to counters and
#: `_count` to histograms, so these are two different metrics and a profile that
#: expects one will silently match nothing of the other.
HTTP_REQUESTS = MetricProfile(
    name="http_requests",
    total_metric="http_request_total",
    service_label="service_name",
    error_selector='outcome=~"5.."',
    unit="API request",
    impact="an API call failed",
)

#: Generic RED fallback for real OpenTelemetry stacks, where server duration is
#: a histogram and the request count arrives as `_count`.
OTEL_HTTP = MetricProfile(
    name="otel_http",
    total_metric="http_server_request_duration_seconds_count",
    service_label="service_name",
    error_selector='http_response_status_code=~"5.."',
    unit="API request",
    impact="an API call failed",
)

PROFILES: tuple[MetricProfile, ...] = (
    PLAYBACK,
    DRM,
    SEGMENT,
    TRANSCODE,
    RENDER,
    SUBTITLE,
    HTTP_REQUESTS,
    OTEL_HTTP,
)


def error_ratio_query(profile: MetricProfile, service: str, window: str) -> str:
    """Bad events / total events over `window`, as a single scalar.

    The `or vector(0)` guard makes a service with zero failures return 0 rather
    than an empty result, which would otherwise be indistinguishable from a
    service that stopped reporting altogether.
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

"""The simulator has to produce a workload the detector can actually find."""

from __future__ import annotations

import json

import pytest

from mediastack_services import SERVICE_NAMES, SERVICES, failure_rate, load_injections
from slo_watchdog.promql import PROFILES


def test_every_service_metric_has_a_matching_profile():
    """A metric family with no profile is a service the sweep cannot see."""
    known = {p.total_metric for p in PROFILES}
    for service in SERVICES:
        # Counters arrive in Prometheus with a _total (or _count) suffix.
        assert (
            f"{service.metric}_total" in known or f"{service.metric}_count" in known
        ), f"{service.name} emits {service.metric}, which no MetricProfile reads"


def test_the_stack_is_small_enough_for_the_free_tier():
    """10k active series is the cap; this must not come close."""
    series = sum(len(s.outcomes) for s in SERVICES)
    assert series < 500, f"{series} series is too many for a 10k budget with SLOs"


def test_every_service_has_a_nonzero_baseline():
    """A perfectly clean service is unrealistic and hides the confidence check."""
    assert all(s.baseline > 0 for s in SERVICES)


def test_failure_outcomes_are_a_subset_of_outcomes():
    for service in SERVICES:
        assert set(service.failure_outcomes) <= set(service.outcomes)
        assert service.outcomes[0] not in service.failure_outcomes


def test_the_stack_covers_delivery_and_production():
    """The cinema value chain, not just a web backend."""
    assert {"playback-api", "drm-license", "cdn-edge"} <= SERVICE_NAMES   # delivery
    assert {"transcode-worker", "render-farm"} <= SERVICE_NAMES           # production


def test_an_uninjected_service_runs_at_baseline():
    service = SERVICES[0]
    assert failure_rate(service, {}, now=100.0, started={}) == service.baseline


def test_an_injected_service_uses_the_requested_fraction():
    service = SERVICES[0]
    injected = {service.name: {"error_fraction": 0.0023}}
    assert failure_rate(service, injected, now=100.0, started={}) == pytest.approx(0.0023)


def test_a_time_boxed_injection_recovers_on_its_own():
    """This is what makes the red herring a red herring."""
    service = SERVICES[0]
    injected = {service.name: {"error_fraction": 0.012, "duration_seconds": 900}}
    started: dict = {}

    during = failure_rate(service, injected, now=1_000.0, started=started)
    after = failure_rate(service, injected, now=1_000.0 + 901, started=started)

    assert during == pytest.approx(0.012)
    assert after == service.baseline


def test_missing_or_corrupt_scenario_file_is_not_fatal(tmp_path):
    assert load_injections(tmp_path / "absent.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{ not json")
    assert load_injections(broken) == {}


def test_a_valid_scenario_file_is_read(tmp_path):
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps({"injected": {"drm-license": {"error_fraction": 0.0023}}}))
    assert load_injections(path)["drm-license"]["error_fraction"] == 0.0023

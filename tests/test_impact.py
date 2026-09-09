"""A burn rate is opaque to everyone except an SRE.

These translations are what let the report say "1 in 435 viewers was refused
content they paid for" instead of "2.3x". Deterministic arithmetic, like the
rest of the detection layer -- the agent is handed the sentence.
"""

from __future__ import annotations

import pytest

from slo_watchdog.burn_rate import REQUIRED_WINDOWS, RatioSample, evaluate
from slo_watchdog.impact import (
    describe_impact,
    events_per_day,
    failures_per_day,
    one_in_n,
    pluralise,
)
from slo_watchdog.promql import DRM, OTEL_HTTP, SUBTITLE


@pytest.mark.parametrize(
    "ratio,expected", [(0.0023, 435), (0.001, 1000), (0.5, 2), (1.0, 1)]
)
def test_one_in_n(ratio, expected):
    assert one_in_n(ratio) == expected


@pytest.mark.parametrize("ratio", [0.0, -0.1, 1.5])
def test_one_in_n_rejects_nonsense(ratio):
    assert one_in_n(ratio) is None


def test_events_per_day_extrapolates_from_the_window():
    assert events_per_day(300_000, "3d") == pytest.approx(100_000)
    assert events_per_day(1_000, "6h") == pytest.approx(4_000)


def test_events_per_day_needs_real_volume():
    assert events_per_day(0, "3d") is None
    assert events_per_day(None, "3d") is None


def test_failures_per_day_combines_rate_and_volume():
    assert failures_per_day(0.0023, 300_000, "3d") == pytest.approx(230.0)


@pytest.mark.parametrize(
    "noun,plural",
    [("request", "requests"), ("fetch", "fetches"), ("playback start", "playback starts"),
     ("box", "boxes"), ("delivery", "deliveries"), ("day", "days")],
)
def test_pluralise(noun, plural):
    assert pluralise(noun) == plural


def a_candidate(ratio=0.0023, count=1_740_000):
    samples = {w: RatioSample(w, ratio, request_count=count) for w in REQUIRED_WINDOWS}
    return evaluate("drm-license", samples, 0.999)


def test_the_drm_sentence_names_the_human_consequence():
    impact = describe_impact(a_candidate(), DRM)
    sentence = impact.sentence()
    assert "1 in 435 license requests fail" in sentence
    assert "refused a licence" in sentence


def test_the_subtitle_sentence_names_the_accessibility_failure():
    assert "accessibility" in describe_impact(a_candidate(), SUBTITLE).sentence()


def test_a_generic_service_still_gets_a_sentence():
    assert describe_impact(a_candidate(), OTEL_HTTP).sentence()


def test_the_sentence_degrades_gracefully_without_volume():
    """No request count means no daily figure, but still a consequence."""
    samples = {w: RatioSample(w, 0.0023, request_count=None) for w in REQUIRED_WINDOWS}
    candidate = evaluate("drm-license", samples, 0.999)
    sentence = describe_impact(candidate, DRM).sentence()
    assert "1 in 435" in sentence
    assert "a day" not in sentence


def test_a_daily_figure_below_one_is_not_claimed():
    """"about 0 a day" would be worse than saying nothing."""
    impact = describe_impact(a_candidate(ratio=0.0023, count=10), DRM)
    assert "a day" not in impact.sentence()

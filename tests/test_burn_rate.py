"""The detection layer is unit-testable precisely because no LLM touches it."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from slo_watchdog.burn_rate import (
    DEFAULT_SLO_WINDOW,
    MIN_WINDOW_SECONDS,
    REQUIRED_WINDOWS,
    TIERS,
    compress,
    required_windows,
    RatioSample,
    budget_consumed_fraction,
    burn_rate,
    classify,
    confidence_for,
    evaluate,
    parse_duration,
    rank,
)

SLO = 0.999  # 99.9% -> a 0.1% error budget
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def ratio_for_burn(target_burn: float, slo_target: float = SLO) -> float:
    """The error ratio that produces `target_burn`."""
    return target_burn * (1.0 - slo_target)


# --------------------------------------------------------------------------
# duration parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,seconds",
    [("5m", 300), ("30m", 1800), ("1h", 3600), ("2h", 7200),
     ("6h", 21600), ("1d", 86400), ("3d", 259200), ("30d", 2592000)],
)
def test_parse_duration(text: str, seconds: int) -> None:
    assert parse_duration(text).total_seconds() == seconds


def test_parse_duration_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        parse_duration("banana")


# --------------------------------------------------------------------------
# core math
# --------------------------------------------------------------------------


def test_burn_rate_definition() -> None:
    # 0.23% errors against a 0.1% budget is a 2.3x burn.
    assert burn_rate(0.0023, SLO) == pytest.approx(2.3)


def test_burn_rate_one_means_exactly_on_budget() -> None:
    assert burn_rate(0.001, SLO) == pytest.approx(1.0)


def test_burn_rate_rejects_impossible_slo() -> None:
    for bad in (0.0, 1.0, 1.5, -0.2):
        with pytest.raises(ValueError):
            burn_rate(0.001, bad)


def test_ratio_sample_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        RatioSample("1h", 1.4)


@pytest.mark.parametrize(
    "tier_name,burn,window,expected_budget_fraction",
    [
        # This is the canonical SRE Workbook table. Each row says: burning at
        # `burn` for `window` consumes `expected_budget_fraction` of a 30d budget.
        ("page-fast", 14.4, "1h", 0.02),
        ("page-slow", 6.0, "6h", 0.05),
        ("ticket", 3.0, "1d", 0.10),
        ("watchdog", 1.0, "30d", 1.00),
    ],
)
def test_burn_rate_table_budget_consumption(
    tier_name: str, burn: float, window: str, expected_budget_fraction: float
) -> None:
    consumed = budget_consumed_fraction(
        ratio_for_burn(burn), SLO, parse_duration(window), DEFAULT_SLO_WINDOW
    )
    assert consumed == pytest.approx(expected_budget_fraction), (
        f"{tier_name}: {burn}x over {window} should burn "
        f"{expected_budget_fraction:.0%} of a 30d budget"
    )


def test_tier_table_matches_the_spec() -> None:
    assert [(t.threshold, t.long_window, t.short_window) for t in TIERS] == [
        (14.4, "1h", "5m"),
        (6.0, "6h", "30m"),
        (3.0, "1d", "2h"),
        (1.0, "3d", "6h"),
    ]


def test_required_windows_cover_every_tier_plus_slo_window() -> None:
    for tier in TIERS:
        assert tier.long_window in REQUIRED_WINDOWS
        assert tier.short_window in REQUIRED_WINDOWS
    assert "30d" in REQUIRED_WINDOWS


# --------------------------------------------------------------------------
# classification: both windows must agree
# --------------------------------------------------------------------------


def test_classify_picks_most_severe_tier() -> None:
    assert classify(20.0, 20.0).name == "page-fast"
    assert classify(7.0, 7.0).name == "page-slow"
    assert classify(3.5, 3.5).name == "ticket"
    assert classify(1.5, 1.5).name == "watchdog"


def test_classify_silent_below_one_x() -> None:
    assert classify(0.9, 0.9) is None


def test_classify_requires_both_windows() -> None:
    """A long window alone is a burn that may already be over."""
    assert classify(5.0, 0.1) is None


# --------------------------------------------------------------------------
# evaluate(): the slow burn, the red herring, the paging tiers
# --------------------------------------------------------------------------


def samples(**by_window: float) -> dict[str, RatioSample]:
    return {
        w: RatioSample(w, ratio, request_count=500_000)
        for w, ratio in by_window.items()
    }


def test_the_hero_finding_a_two_x_slow_burn() -> None:
    """2x burn: never pages, quietly eats the month."""
    result = evaluate(
        "paymentservice",
        samples(**{w: ratio_for_burn(2.0) for w in REQUIRED_WINDOWS}),
        SLO,
        now=NOW,
    )
    assert result is not None
    assert result.tier.name == "watchdog"
    assert result.burn_rate == pytest.approx(2.0)
    assert result.windows == ("3d", "6h")
    assert result.severity == "watchdog"
    assert result.is_watchdog_territory


def test_red_herring_is_suppressed_by_the_short_window() -> None:
    """A spike that already recovered must not be reported.

    Showing the agent correctly staying quiet is the trust signal in the demo.
    """
    burnt = {w: ratio_for_burn(8.0) for w in REQUIRED_WINDOWS}
    # ...but every short window has recovered to healthy.
    for short in ("5m", "30m", "2h", "6h"):
        burnt[short] = ratio_for_burn(0.05)
    assert evaluate("cartservice", samples(**burnt), SLO, now=NOW) is None


def test_paging_tier_is_detected_but_flagged_as_not_our_territory() -> None:
    result = evaluate(
        "checkoutservice",
        samples(**{w: ratio_for_burn(15.0) for w in REQUIRED_WINDOWS}),
        SLO,
        now=NOW,
    )
    assert result is not None
    assert result.tier.name == "page-fast"
    assert result.severity == "page"
    assert not result.is_watchdog_territory


def test_healthy_service_produces_nothing() -> None:
    assert evaluate(
        "frontend", samples(**{w: 0.0 for w in REQUIRED_WINDOWS}), SLO, now=NOW
    ) is None


def test_missing_windows_fall_through_to_a_lower_tier() -> None:
    """Partial data must never crash or invent a tier."""
    partial = samples(**{"3d": ratio_for_burn(2.0), "6h": ratio_for_burn(2.0)})
    result = evaluate("adservice", partial, SLO, now=NOW)
    assert result is not None and result.tier.name == "watchdog"


# --------------------------------------------------------------------------
# budget accounting
# --------------------------------------------------------------------------


def test_budget_remaining_comes_from_the_full_slo_window() -> None:
    data = samples(**{w: ratio_for_burn(2.0) for w in REQUIRED_WINDOWS})
    data["30d"] = RatioSample("30d", ratio_for_burn(0.6), request_count=5_000_000)
    result = evaluate("paymentservice", data, SLO, now=NOW)
    assert result is not None
    # 60% of the budget consumed over 30d -> 40% remains.
    assert result.budget_remaining_pct == pytest.approx(40.0)


def test_projected_exhaustion_scales_with_burn_rate() -> None:
    data = samples(**{w: ratio_for_burn(2.0) for w in REQUIRED_WINDOWS})
    data["30d"] = RatioSample("30d", ratio_for_burn(0.6), request_count=5_000_000)
    result = evaluate("paymentservice", data, SLO, now=NOW)
    assert result is not None and result.projected_exhaustion is not None
    # 40% of a 30d budget burned at 2x -> 6 days.
    assert result.projected_exhaustion - NOW == pytest.approx(
        timedelta(days=6), abs=timedelta(minutes=1)
    )


def test_exhausted_budget_reports_zero_not_negative() -> None:
    data = samples(**{w: ratio_for_burn(2.0) for w in REQUIRED_WINDOWS})
    data["30d"] = RatioSample("30d", ratio_for_burn(3.0), request_count=5_000_000)
    result = evaluate("paymentservice", data, SLO, now=NOW)
    assert result is not None
    assert result.budget_remaining_pct == 0.0
    assert result.projected_exhaustion is None


# --------------------------------------------------------------------------
# confidence
# --------------------------------------------------------------------------


def test_low_traffic_service_is_not_reported_as_a_crisis() -> None:
    """Three errors out of two hundred requests is noise, not a finding."""
    assert confidence_for(0.015, 200, threshold_ratio=0.001) == "low"


def test_high_volume_clear_breach_is_high_confidence() -> None:
    assert confidence_for(0.0023, 1_000_000, threshold_ratio=0.001) == "high"


def test_unknown_volume_is_never_high_confidence() -> None:
    assert confidence_for(0.0023, None, threshold_ratio=0.001) == "low"


def test_evaluate_propagates_confidence_from_volume() -> None:
    thin = {
        w: RatioSample(w, ratio_for_burn(2.0), request_count=150)
        for w in REQUIRED_WINDOWS
    }
    result = evaluate("emailservice", thin, SLO, now=NOW)
    assert result is not None and result.confidence == "low"


# --------------------------------------------------------------------------
# provisional SLOs and ranking
# --------------------------------------------------------------------------


def test_provisional_flag_is_carried_through() -> None:
    result = evaluate(
        "recommendationservice",
        samples(**{w: ratio_for_burn(2.0) for w in REQUIRED_WINDOWS}),
        SLO,
        provisional=True,
        now=NOW,
    )
    assert result is not None and result.provisional


def test_rank_puts_watchdog_territory_above_paging_tiers() -> None:
    """Paging tiers already have an owner; surfacing them first buries the point."""
    paging = evaluate(
        "checkoutservice",
        samples(**{w: ratio_for_burn(15.0) for w in REQUIRED_WINDOWS}),
        SLO, now=NOW,
    )
    slow = evaluate(
        "paymentservice",
        samples(**{w: ratio_for_burn(2.0) for w in REQUIRED_WINDOWS}),
        SLO, now=NOW,
    )
    assert paging and slow
    assert [c.service for c in rank([paging, slow])][0] == "paymentservice"


def test_rank_orders_by_budget_remaining_within_a_tier() -> None:
    def with_remaining(service: str, consumed_burn: float):
        data = samples(**{w: ratio_for_burn(2.0) for w in REQUIRED_WINDOWS})
        data["30d"] = RatioSample("30d", ratio_for_burn(consumed_burn), request_count=1e6)
        return evaluate(service, data, SLO, now=NOW)

    healthy = with_remaining("adservice", 0.2)     # 80% left
    dire = with_remaining("paymentservice", 0.9)   # 10% left
    assert healthy and dire
    assert [c.service for c in rank([healthy, dire])] == ["paymentservice", "adservice"]


# --------------------------------------------------------------------------
# invariants from the SRE Workbook
# --------------------------------------------------------------------------


def test_short_window_is_one_twelfth_of_the_long_window() -> None:
    """The Workbook's rule of thumb, and the reason the short window works.

    A short window 1/12 the length of the long one is responsive enough to
    notice a burn has stopped, without being so twitchy that ordinary variance
    resets the alert.
    """
    for tier in TIERS:
        assert tier.short.total_seconds() == pytest.approx(
            tier.long.total_seconds() / 12
        ), f"{tier.name} breaks the 1/12 short-window rule"


def test_low_traffic_pathology_from_the_workbook_is_caught_by_confidence() -> None:
    """The Workbook's own worked example of where burn-rate alerting fails.

    Ten requests an hour, one fails: a 10% hourly error rate that burns 13.9%
    of a 30-day budget on the strength of a single request. The burn rate is
    real arithmetic, so it fires -- the confidence check is what stops it being
    reported as a crisis.
    """
    error_ratio = 1 / 10
    assert burn_rate(error_ratio, SLO) == pytest.approx(100.0)
    budget = budget_consumed_fraction(error_ratio, SLO, parse_duration("1h"))
    assert budget == pytest.approx(0.1389, abs=1e-4)
    assert confidence_for(error_ratio, 10, threshold_ratio=0.001) == "low"


def test_evaluate_and_classify_never_disagree() -> None:
    """`evaluate` must not carry its own copy of the threshold rule.

    It used to. The tier tests exercised `classify`, which nothing in
    production called, so a change to `evaluate`'s comparison would have gone
    unnoticed. This sweeps the whole burn range and asserts the two agree.
    """
    for burn in [0.5, 0.99, 1.0, 1.5, 2.9, 3.0, 5.9, 6.0, 14.39, 14.4, 30.0]:
        uniform = {
            w: RatioSample(w, ratio_for_burn(burn), request_count=500_000)
            for w in REQUIRED_WINDOWS
        }
        result = evaluate("svc", uniform, SLO, now=NOW)
        expected = classify(burn, burn)
        assert (result.tier if result else None) == expected, f"burn={burn}"


# --------------------------------------------------------------------------
# compressed windows, for demos against a freshly started stack
# --------------------------------------------------------------------------


def test_compression_preserves_every_threshold() -> None:
    """Burn rate is a rate: shortening the window must not move the bar."""
    for original, scaled in zip(TIERS, compress(288), strict=True):
        assert scaled.threshold == original.threshold
        assert scaled.name == original.name
        assert scaled.severity == original.severity


def test_compression_shortens_the_watchdog_window() -> None:
    watchdog = compress(288)[-1]
    assert watchdog.long_window == "15m"
    assert watchdog.short_window == "75s"


def test_compression_keeps_the_ratio_where_the_floor_allows() -> None:
    watchdog = compress(288)[-1]
    assert watchdog.long.total_seconds() / watchdog.short.total_seconds() == pytest.approx(12)


def test_compression_never_produces_a_window_below_the_floor() -> None:
    """A window shorter than one export interval holds no usable points."""
    for tier in compress(10_000):
        assert tier.short.total_seconds() >= MIN_WINDOW_SECONDS
        assert tier.long.total_seconds() >= MIN_WINDOW_SECONDS


def test_compression_rejects_a_nonsense_factor() -> None:
    for bad in (0, -1):
        with pytest.raises(ValueError):
            compress(bad)


def test_a_compressed_table_still_detects_the_same_burn() -> None:
    """The whole justification for the flag."""
    tiers = compress(288)
    windows = required_windows(tiers, "2h")
    samples = {
        w: RatioSample(w, ratio_for_burn(2.3), request_count=500_000) for w in windows
    }
    result = evaluate("drm-license", samples, SLO, tiers=tiers,
                      slo_window=parse_duration("2h"), now=NOW)
    assert result is not None
    assert result.tier.name == "watchdog"
    assert result.burn_rate == pytest.approx(2.3)


def test_required_windows_follows_the_tier_table() -> None:
    assert required_windows(compress(288), "2h") == ("1m", "75s", "5m", "15m", "2h")

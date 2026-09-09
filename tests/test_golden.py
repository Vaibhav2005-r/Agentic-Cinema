"""The recorded sweep must keep telling the same story.

This is the demo. If a refactor changes what the golden run reports, the video
is wrong -- so the narrative is pinned here rather than trusted.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from slo_watchdog.detect import detect
from slo_watchdog.discovery import discover
from slo_watchdog.mcp_client import ReplayToolCaller

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "golden-sweep.json"


@pytest.fixture()
def caller():
    if not FIXTURE.exists():
        pytest.skip("run scripts/make_golden_fixture.py first")
    return ReplayToolCaller.from_file(FIXTURE, strict=True)


async def test_the_golden_inventory(caller):
    inventory = await discover(caller)
    assert len(inventory.services) == 11
    assert inventory.provisional_count == 7
    assert inventory.datasources.prometheus_uid == "grafanacloud-prom"
    assert inventory.datasources.loki_uid == "grafanacloud-logs"


async def test_the_golden_sweep_reports_exactly_two_findings(caller):
    candidates = await detect(caller, await discover(caller))
    assert [c.service for c in candidates] == ["drm-license", "subtitle-service"]


async def test_the_hero_finding_is_a_slow_burn_that_would_never_page(caller):
    """DRM denials: far too small to page, immediately obvious to a viewer."""
    drm = (await detect(caller, await discover(caller)))[0]
    assert drm.service == "drm-license"
    assert drm.burn_rate == pytest.approx(2.3, abs=0.05)
    assert drm.tier.name == "watchdog"
    assert drm.severity != "page"
    assert drm.budget_remaining_pct == pytest.approx(34.0, abs=0.5)
    assert drm.projected_exhaustion is not None
    assert not drm.provisional  # this one has a real, human-defined SLO


async def test_the_hero_finding_translates_into_audience_impact(caller):
    """A burn rate is opaque to a duty manager; "1 in 435 viewers" is not."""
    from slo_watchdog.impact import describe_impact

    inventory = await discover(caller)
    drm = (await detect(caller, inventory))[0]
    profile = next(s.profile for s in inventory.services if s.name == "drm-license")
    impact = describe_impact(drm, profile)
    assert impact.one_in == 435
    assert impact.failures_per_day > 1000
    assert "licence" in impact.sentence()


async def test_the_second_finding_exercises_the_provisional_path(caller):
    """Nobody writes an SLO for subtitles, which is why it went unnoticed."""
    subs = (await detect(caller, await discover(caller)))[1]
    assert subs.service == "subtitle-service"
    assert subs.provisional
    assert subs.burn_rate == pytest.approx(1.8, abs=0.05)


async def test_the_red_herring_is_never_reported(caller):
    """The transcode batch is badly burnt over 3d and fully recovered over 6h."""
    candidates = await detect(caller, await discover(caller))
    assert "transcode-worker" not in {c.service for c in candidates}


async def test_the_sweep_is_quiet_about_the_other_nine_services(caller):
    inventory = await discover(caller)
    candidates = await detect(caller, inventory)
    assert len(inventory.services) - len(candidates) == 9


async def test_the_fixture_covers_every_query_the_pipeline_makes(caller):
    """A fixture miss would silently weaken the demo rather than fail it."""
    await detect(caller, await discover(caller))
    assert caller.misses == []

"""Chaos scenarios are specified by burn rate, so they can be checked.

Asking for "a 2.3x burn" and getting back a failure fraction that reproduces
exactly 2.3x is what makes the demo trustworthy: the detector's answer can be
compared against the number the injector was told to produce.
"""

from __future__ import annotations

import json

import pytest

from slo_watchdog.burn_rate import burn_rate
from slo_watchdog.chaos import SCENARIOS, apply, describe, reset


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "scenarios.json"
    path.write_text(json.dumps({"injected": {}}))
    return path


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_injected_fraction_reproduces_the_requested_burn_rate(name):
    scenario = SCENARIOS[name]
    assert burn_rate(scenario.error_fraction(), scenario.slo_target) == pytest.approx(
        scenario.target_burn_rate, rel=1e-9
    )


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_scenario_names_a_real_media_service(name):
    """A scenario pointing at a service the simulator does not run is inert."""
    from mediastack_services import SERVICE_NAMES

    assert SCENARIOS[name].service in SERVICE_NAMES


def test_the_hero_scenario_sits_under_every_paging_threshold():
    """If the DRM slow burn paged, the whole premise would collapse."""
    assert SCENARIOS["drm_slow_burn"].target_burn_rate < 3.0


def test_the_red_herring_is_loud_and_time_boxed():
    """Big enough that only the short window suppresses it, and it recovers."""
    spike = SCENARIOS["transcode_spike"]
    assert spike.target_burn_rate > 6.0
    assert spike.duration_seconds is not None


def test_one_in_n_is_reported_for_the_hero():
    assert SCENARIOS["drm_slow_burn"].one_in() == 435


def test_apply_writes_the_injection(config):
    apply(config, ["drm_slow_burn"])
    injected = json.loads(config.read_text())["injected"]
    assert injected["drm-license"]["error_fraction"] == pytest.approx(0.0023)
    assert injected["drm-license"]["scenario"] == "drm_slow_burn"


def test_apply_is_additive_across_scenarios(config):
    apply(config, ["drm_slow_burn"])
    apply(config, ["subtitle_degradation"])
    injected = json.loads(config.read_text())["injected"]
    assert set(injected) == {"drm-license", "subtitle-service"}


def test_disabling_removes_only_that_service(config):
    apply(config, ["drm_slow_burn", "subtitle_degradation"])
    apply(config, ["drm_slow_burn"], enabled=False)
    assert set(json.loads(config.read_text())["injected"]) == {"subtitle-service"}


def test_reset_clears_everything(config):
    apply(config, list(SCENARIOS))
    reset(config)
    assert json.loads(config.read_text())["injected"] == {}


def test_apply_creates_the_file_if_absent(tmp_path):
    path = tmp_path / "nested" / "scenarios.json"
    apply(path, ["drm_slow_burn"])
    assert path.exists()


def test_an_unknown_scenario_is_a_clear_error(config):
    with pytest.raises(KeyError, match="unknown scenario"):
        apply(config, ["nope"])


def test_describe_lists_every_scenario():
    text = describe()
    assert all(name in text for name in SCENARIOS)

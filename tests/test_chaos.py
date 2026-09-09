"""Chaos scenarios are specified by burn rate, so they can be checked."""

from __future__ import annotations

import json

import pytest

from slo_watchdog.burn_rate import burn_rate
from slo_watchdog.chaos import FRACTION_BASE, SCENARIOS, apply, describe, reset

FLAGD = {
    "flags": {
        "paymentServiceFailure": {"state": "ENABLED", "variants": {"on": True, "off": False},
                                  "defaultVariant": "off"},
        "recommendationServiceCacheFailure": {"state": "ENABLED", "variants": {"on": True, "off": False},
                                              "defaultVariant": "off"},
        "cartServiceFailure": {"state": "ENABLED", "variants": {"on": True, "off": False},
                               "defaultVariant": "off"},
    }
}


@pytest.fixture()
def config(tmp_path):
    path = tmp_path / "demo.flagd.json"
    path.write_text(json.dumps(FLAGD))
    return path


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_the_injected_fraction_reproduces_the_requested_burn_rate(name):
    scenario = SCENARIOS[name]
    on, off = scenario.weights()
    assert on + off == FRACTION_BASE
    assert burn_rate(on / FRACTION_BASE, scenario.slo_target) == pytest.approx(
        scenario.target_burn_rate, rel=1e-3
    )


def test_the_hero_scenario_sits_under_every_paging_threshold():
    """If the slow burn paged, the whole premise would collapse."""
    assert SCENARIOS["slow_burn"].target_burn_rate < 3.0


def test_the_red_herring_is_deliberately_loud():
    """It must be big enough that only the short window suppresses it."""
    assert SCENARIOS["red_herring"].target_burn_rate > 6.0


def test_apply_writes_fractional_targeting(config):
    apply(config, ["slow_burn"])
    flag = json.loads(config.read_text())["flags"]["paymentServiceFailure"]
    assert flag["targeting"]["fractional"] == [["on", 23], ["off", 9977]]
    assert flag["defaultVariant"] == "off"


def test_reset_removes_targeting(config):
    apply(config, ["slow_burn"])
    reset(config)
    assert "targeting" not in json.loads(config.read_text())["flags"]["paymentServiceFailure"]


def test_an_unknown_scenario_is_a_clear_error(config):
    with pytest.raises(KeyError, match="unknown scenario"):
        apply(config, ["nope"])


def test_a_missing_flag_names_what_is_available(tmp_path):
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"flags": {}}))
    with pytest.raises(KeyError, match="not in flagd config"):
        apply(path, ["slow_burn"])


def test_describe_lists_every_scenario():
    text = describe()
    assert all(name in text for name in SCENARIOS)

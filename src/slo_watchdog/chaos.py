"""Drive the OpenTelemetry demo's feature flags to produce findable problems.

An agent that finds nothing is not a demo, so the demo environment is the first
thing to build and the biggest risk in the project.

The failure probabilities here are computed from the same burn-rate math the
detector uses, which means a scenario is specified as "produce a 2.3x burn"
rather than as a magic percentage -- and the detector's answer can be checked
against the number we asked for.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: flagd resolves fractional variants out of this many parts.
FRACTION_BASE = 10_000


@dataclass(frozen=True)
class Scenario:
    """One injected failure, specified by the burn rate it should produce."""

    name: str
    flag: str
    target_burn_rate: float
    slo_target: float
    description: str
    on_variant: str = "on"
    off_variant: str = "off"

    def error_fraction(self) -> float:
        """The request failure probability that yields `target_burn_rate`."""
        return self.target_burn_rate * (1.0 - self.slo_target)

    def weights(self) -> tuple[int, int]:
        """(on, off) integer weights for flagd fractional targeting."""
        on = max(1, round(self.error_fraction() * FRACTION_BASE))
        return on, FRACTION_BASE - on


#: The three outcomes one sweep should produce for the video.
SCENARIOS: dict[str, Scenario] = {
    # The hero finding: never pages, quietly eats the month.
    "slow_burn": Scenario(
        name="slow_burn",
        flag="paymentServiceFailure",
        target_burn_rate=2.3,
        slo_target=0.999,
        description="payment service failing at 0.23% -- a 2.3x burn, well under any page",
    ),
    # A service with no SLO defined, so the agent must derive one from RED.
    "provisional": Scenario(
        name="provisional",
        flag="recommendationServiceCacheFailure",
        target_burn_rate=1.8,
        slo_target=0.999,
        description="degrades a service with no SLO attached; exercises the provisional path",
    ),
    # Proves the short-window check suppresses a burn that already ended.
    "red_herring": Scenario(
        name="red_herring",
        flag="cartServiceFailure",
        target_burn_rate=12.0,
        slo_target=0.999,
        description="a brief spike that recovers; the agent should stay quiet about it",
    ),
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _save(path: Path, config: dict[str, Any]) -> None:
    path.write_text(json.dumps(config, indent=2) + "\n")


def set_flag(config: dict[str, Any], scenario: Scenario, enabled: bool) -> dict[str, Any]:
    """Point one flagd flag at a fractional split, or turn it off.

    flagd hot-reloads its config file, so writing the file is the whole
    mechanism -- no restart, no API call.
    """
    flags = config.setdefault("flags", {})
    flag = flags.get(scenario.flag)
    if flag is None:
        raise KeyError(
            f"flag {scenario.flag!r} not in flagd config. "
            f"Available: {', '.join(sorted(flags)) or '(none)'}"
        )

    flag["state"] = "ENABLED"
    if not enabled:
        flag.pop("targeting", None)
        flag["defaultVariant"] = scenario.off_variant
        return config

    on_weight, off_weight = scenario.weights()
    flag["defaultVariant"] = scenario.off_variant
    flag["targeting"] = {
        "fractional": [
            [scenario.on_variant, on_weight],
            [scenario.off_variant, off_weight],
        ]
    }
    return config


def apply(path: Path, names: list[str], enabled: bool = True) -> list[Scenario]:
    """Enable or disable named scenarios in the flagd config."""
    config = _load(path)
    applied: list[Scenario] = []
    for name in names:
        scenario = SCENARIOS.get(name)
        if scenario is None:
            raise KeyError(f"unknown scenario {name!r}; try: {', '.join(SCENARIOS)}")
        set_flag(config, scenario, enabled)
        applied.append(scenario)
    _save(path, config)
    return applied


def reset(path: Path) -> None:
    """Turn every scenario off."""
    apply(path, list(SCENARIOS), enabled=False)


def describe() -> str:
    lines = ["Scenarios (probability computed from the target burn rate):", ""]
    for scenario in SCENARIOS.values():
        on, _ = scenario.weights()
        lines.append(
            f"  {scenario.name:<13} {scenario.flag:<36} "
            f"{scenario.target_burn_rate:>4.1f}x  "
            f"{scenario.error_fraction() * 100:>6.3f}% of requests  ({on}/{FRACTION_BASE})"
        )
        lines.append(f"                {scenario.description}")
    return "\n".join(lines)

"""Inject findable failures into the media stack.

An agent that finds nothing is not a demo, so the workload is the first thing
to build and the biggest risk in the project.

Failure probabilities are computed from the same burn-rate math the detector
uses, so a scenario is specified as "produce a 2.3x burn" rather than as a
magic percentage -- and the detector's answer can be checked against the number
we asked for.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Default location of the simulator's hot-reloaded scenario file.
DEFAULT_CONFIG = Path("mediastack/scenarios.json")


@dataclass(frozen=True)
class Scenario:
    """One injected failure, specified by the burn rate it should produce."""

    name: str
    service: str
    target_burn_rate: float
    slo_target: float
    description: str
    #: Seconds to stay on before auto-recovering. None means indefinite.
    duration_seconds: int | None = None

    def error_fraction(self) -> float:
        """The failure probability that yields `target_burn_rate`."""
        return self.target_burn_rate * (1.0 - self.slo_target)

    def one_in(self) -> int:
        return round(1.0 / self.error_fraction())


SCENARIOS: dict[str, Scenario] = {
    # The hero finding. DRM denial is the textbook silent failure in
    # streaming: far too small to page, immediately obvious to the viewer,
    # and it looks like the product is broken rather than the platform.
    "drm_slow_burn": Scenario(
        name="drm_slow_burn",
        service="drm-license",
        target_burn_rate=2.3,
        slo_target=0.999,
        description="licence denials at 0.23% -- 1 in 435 viewers refused content they paid for",
    ),
    # The provisional finding. Nobody writes an SLO for subtitles, which is
    # exactly why it degrades unnoticed -- and it is an accessibility failure.
    "subtitle_degradation": Scenario(
        name="subtitle_degradation",
        service="subtitle-service",
        target_burn_rate=1.8,
        slo_target=0.999,
        description="subtitle fetch failures on a service with no SLO defined",
    ),
    # The red herring. A transcode batch spikes and recovers; the short window
    # must suppress it.
    "transcode_spike": Scenario(
        name="transcode_spike",
        service="transcode-worker",
        target_burn_rate=12.0,
        slo_target=0.999,
        description="a transcode batch that fails hard and then recovers",
        duration_seconds=900,
    ),
    # A render farm burn, for the production-side story.
    "render_farm_burn": Scenario(
        name="render_farm_burn",
        service="render-farm",
        target_burn_rate=2.9,
        slo_target=0.999,
        description="frames failing on the render farm, quietly burning artist days",
    ),
}


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"injected": {}}
    return json.loads(path.read_text())


def _save(path: Path, config: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2) + "\n")


def apply(path: Path, names: list[str], enabled: bool = True) -> list[Scenario]:
    """Enable or disable named scenarios. The simulator hot-reloads the file."""
    config = _load(path)
    injected = config.setdefault("injected", {})
    applied: list[Scenario] = []
    for name in names:
        scenario = SCENARIOS.get(name)
        if scenario is None:
            raise KeyError(f"unknown scenario {name!r}; try: {', '.join(SCENARIOS)}")
        if enabled:
            injected[scenario.service] = {
                "scenario": scenario.name,
                "error_fraction": scenario.error_fraction(),
                "duration_seconds": scenario.duration_seconds,
            }
        else:
            injected.pop(scenario.service, None)
        applied.append(scenario)
    _save(path, config)
    return applied


def reset(path: Path) -> None:
    """Turn every scenario off."""
    _save(path, {"injected": {}})


def describe() -> str:
    lines = ["Scenarios (failure rate computed from the target burn rate):", ""]
    for scenario in SCENARIOS.values():
        window = (
            f"  for {scenario.duration_seconds // 60}m"
            if scenario.duration_seconds
            else ""
        )
        lines.append(
            f"  {scenario.name:<22} {scenario.service:<18} "
            f"{scenario.target_burn_rate:>4.1f}x  "
            f"{scenario.error_fraction() * 100:>6.3f}%  "
            f"1 in {scenario.one_in():,}{window}"
        )
        lines.append(f"                         {scenario.description}")
    return "\n".join(lines)

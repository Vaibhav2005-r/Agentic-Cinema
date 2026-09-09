"""Translate a burn rate into something a producer can act on.

A burn rate of 2.3x is meaningful to an SRE and meaningless to a duty manager
or a post supervisor. The same number expressed as "1 in 435 playback starts
fails, roughly 2,100 viewers a day" is actionable to both.

This is deterministic arithmetic, like everything in the detection layer -- the
agent is handed the sentence, it does not compute it.
"""

from __future__ import annotations

from dataclasses import dataclass

from .burn_rate import Candidate, parse_duration
from .promql import MetricProfile

SECONDS_PER_DAY = 86_400.0


def pluralise(noun: str) -> str:
    """Enough English to keep the report readable: "fetch" -> "fetches"."""
    if noun.endswith(("s", "x", "z", "ch", "sh")):
        return noun + "es"
    if noun.endswith("y") and not noun.endswith(("ay", "ey", "iy", "oy", "uy")):
        return noun[:-1] + "ies"
    return noun + "s"


def one_in_n(error_ratio: float) -> int | None:
    """`0.0023` -> 435. None when the ratio is zero or nonsensical."""
    if error_ratio <= 0.0 or error_ratio > 1.0:
        return None
    return round(1.0 / error_ratio)


def events_per_day(request_count: float | None, window: str) -> float | None:
    """Extrapolate the window's volume to a daily rate."""
    if not request_count or request_count <= 0:
        return None
    seconds = parse_duration(window).total_seconds()
    if seconds <= 0:
        return None
    return request_count * (SECONDS_PER_DAY / seconds)


def failures_per_day(
    error_ratio: float, request_count: float | None, window: str
) -> float | None:
    daily = events_per_day(request_count, window)
    return None if daily is None else daily * error_ratio


@dataclass(frozen=True)
class Impact:
    """What a candidate means in the physical world."""

    one_in: int | None
    failures_per_day: float | None
    unit: str
    consequence: str

    def sentence(self) -> str:
        parts: list[str] = []
        if self.one_in:
            parts.append(f"1 in {self.one_in:,} {pluralise(self.unit)} fail")
        if self.failures_per_day and self.failures_per_day >= 1:
            parts.append(f"about {round(self.failures_per_day):,} a day")
        if not parts:
            return self.consequence
        return f"{'; '.join(parts)} -- {self.consequence}"


def describe_impact(candidate: Candidate, profile: MetricProfile) -> Impact:
    """Turn a detected candidate into audience- or production-facing terms."""
    long_window = candidate.windows[0]
    sample = candidate.samples.get(long_window)
    ratio = sample.error_ratio if sample else candidate.burn_rate * (1 - candidate.slo_target)
    return Impact(
        one_in=one_in_n(ratio),
        failures_per_day=failures_per_day(ratio, candidate.request_count, long_window),
        unit=profile.unit,
        consequence=profile.impact,
    )

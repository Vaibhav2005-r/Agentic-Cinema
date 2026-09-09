"""Multi-window, multi-burn-rate error-budget math.

Every number the agent reasons about is produced *here*, in deterministic
Python, from raw PromQL results -- never by the language model. The agent
receives structured `Candidate` objects and reasons about meaning, not
arithmetic. See README section "Why the math is not in the LLM".

Reference: Google SRE Workbook, "Alerting on SLOs", multiwindow multi-burn-rate.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping

Severity = Literal["page", "ticket", "watchdog"]
Confidence = Literal["high", "medium", "low"]

#: Default SLO compliance window. Burn rate 1.0 means the budget is exhausted
#: exactly at the end of this window.
DEFAULT_SLO_WINDOW = timedelta(days=30)

_DURATION_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h|d|w)$")
_UNIT_SECONDS = {
    "ms": 0.001,
    "s": 1.0,
    "m": 60.0,
    "h": 3600.0,
    "d": 86400.0,
    "w": 604800.0,
}


def parse_duration(text: str) -> timedelta:
    """Parse a Prometheus-style duration (``"5m"``, ``"3d"``) into a timedelta."""
    match = _DURATION_RE.match(text.strip())
    if not match:
        raise ValueError(f"unparseable duration: {text!r}")
    value, unit = match.groups()
    return timedelta(seconds=float(value) * _UNIT_SECONDS[unit])


@dataclass(frozen=True)
class Tier:
    """One row of the multi-burn-rate alerting table."""

    name: str
    severity: Severity
    threshold: float
    long_window: str
    short_window: str
    rationale: str

    @property
    def long(self) -> timedelta:
        return parse_duration(self.long_window)

    @property
    def short(self) -> timedelta:
        return parse_duration(self.short_window)


#: Ordered most-severe first. `classify` returns the first tier that fires.
#:
#: The top two rows are what conventional paging alerts already catch. The
#: bottom two are the watchdog's territory: a 1x-3x burn never pages, but it
#: quietly eats a month of error budget in days.
TIERS: tuple[Tier, ...] = (
    Tier("page-fast", "page", 14.4, "1h", "5m",
         "2% of a 30d budget in one hour; conventional alerts catch this"),
    Tier("page-slow", "page", 6.0, "6h", "30m",
         "5% of a 30d budget in six hours; conventional alerts catch this"),
    Tier("ticket", "ticket", 3.0, "1d", "2h",
         "10% of a 30d budget in a day; often defined but unrouted"),
    Tier("watchdog", "watchdog", 1.0, "3d", "6h",
         "budget exhausted on schedule or faster; nobody is looking"),
)

#: Every distinct window the detector needs to sample, plus the SLO window
#: itself (used for budget-remaining).
REQUIRED_WINDOWS: tuple[str, ...] = tuple(
    dict.fromkeys(
        [w for tier in TIERS for w in (tier.long_window, tier.short_window)] + ["30d"]
    )
)


@dataclass(frozen=True)
class RatioSample:
    """An observed bad-event ratio over one window, from `query_prometheus`."""

    window: str
    error_ratio: float
    request_count: float | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.error_ratio <= 1.0:
            raise ValueError(
                f"error_ratio for window {self.window!r} out of range: {self.error_ratio}"
            )


def burn_rate(error_ratio: float, slo_target: float) -> float:
    """Budget burn rate: ``error_ratio / (1 - slo_target)``.

    A burn rate of 1.0 consumes exactly the whole budget over the SLO window.
    14.4 consumes 2% of a 30-day budget in one hour.
    """
    if not 0.0 < slo_target < 1.0:
        raise ValueError(f"slo_target must be in (0, 1), got {slo_target}")
    return error_ratio / (1.0 - slo_target)


def budget_consumed_fraction(
    error_ratio: float,
    slo_target: float,
    window: timedelta,
    slo_window: timedelta = DEFAULT_SLO_WINDOW,
) -> float:
    """Fraction of the total error budget burned by `window` at this ratio."""
    return burn_rate(error_ratio, slo_target) * (
        window.total_seconds() / slo_window.total_seconds()
    )


def confidence_for(
    error_ratio: float,
    request_count: float | None,
    threshold_ratio: float,
) -> Confidence:
    """How sure are we the true ratio exceeds `threshold_ratio`?

    A one-sided z-score on the binomial proportion. This is what stops a
    low-traffic service with three errors from being reported as a crisis.
    """
    if request_count is None or request_count <= 0:
        return "low"
    variance = error_ratio * (1.0 - error_ratio) / request_count
    if variance <= 0:
        # A clean 0% or 100% ratio; trust it only with real volume behind it.
        return "high" if request_count >= 1000 else "low"
    z = (error_ratio - threshold_ratio) / math.sqrt(variance)
    if z >= 3.0:
        return "high"
    if z >= 2.0:
        return "medium"
    return "low"


def classify(
    long_burn: float, short_burn: float, tiers: tuple[Tier, ...] = TIERS
) -> Tier | None:
    """Most severe tier where **both** windows exceed the threshold.

    Requiring the short window is what stops the agent reporting a burn that
    has already ended -- the red-herring suppression in the demo.
    """
    for tier in tiers:
        if long_burn >= tier.threshold and short_burn >= tier.threshold:
            return tier
    return None


@dataclass
class Candidate:
    """A deterministic detection, handed to the agent for judgement."""

    service: str
    sli: str
    slo_target: float
    tier: Tier
    burn_rate: float
    short_burn_rate: float
    windows: tuple[str, str]
    budget_remaining_pct: float
    projected_exhaustion: datetime | None
    provisional: bool
    confidence: Confidence
    request_count: float | None = None
    samples: dict[str, RatioSample] = field(default_factory=dict)

    @property
    def severity(self) -> Severity:
        return self.tier.severity

    @property
    def is_watchdog_territory(self) -> bool:
        """True when conventional paging alerts would stay silent."""
        return self.tier.severity in ("ticket", "watchdog")


def evaluate(
    service: str,
    samples: Mapping[str, RatioSample],
    slo_target: float,
    *,
    sli: str = "availability",
    provisional: bool = False,
    slo_window: timedelta = DEFAULT_SLO_WINDOW,
    now: datetime | None = None,
    tiers: tuple[Tier, ...] = TIERS,
) -> Candidate | None:
    """Evaluate one service's samples against the burn-rate table.

    `samples` maps window string -> RatioSample; see `REQUIRED_WINDOWS`.
    Returns None when no tier fires.
    """
    now = now or datetime.now(timezone.utc)

    def burn_for(window: str) -> float | None:
        sample = samples.get(window)
        if sample is None:
            return None
        return burn_rate(sample.error_ratio, slo_target)

    fired: Tier | None = None
    long_burn = short_burn = 0.0
    for tier in tiers:
        lb, sb = burn_for(tier.long_window), burn_for(tier.short_window)
        if lb is None or sb is None:
            continue
        # Delegate the threshold comparison to `classify` so the rule lives in
        # exactly one place; each tier is checked against its own windows.
        if classify(lb, sb, (tier,)) is not None:
            fired, long_burn, short_burn = tier, lb, sb
            break

    if fired is None:
        return None

    # Budget remaining is measured over the full SLO window, not the tier's.
    slo_sample = samples.get("30d")
    if slo_sample is not None:
        consumed = burn_rate(slo_sample.error_ratio, slo_target)
    else:
        consumed = budget_consumed_fraction(
            samples[fired.long_window].error_ratio, slo_target, fired.long, slo_window
        )
    remaining = max(0.0, 1.0 - consumed)

    projected: datetime | None = None
    if long_burn > 0 and remaining > 0:
        seconds_left = remaining * slo_window.total_seconds() / long_burn
        projected = now + timedelta(seconds=seconds_left)

    long_sample = samples[fired.long_window]
    threshold_ratio = fired.threshold * (1.0 - slo_target)

    return Candidate(
        service=service,
        sli=sli,
        slo_target=slo_target,
        tier=fired,
        burn_rate=long_burn,
        short_burn_rate=short_burn,
        windows=(fired.long_window, fired.short_window),
        budget_remaining_pct=remaining * 100.0,
        projected_exhaustion=projected,
        provisional=provisional,
        confidence=confidence_for(
            long_sample.error_ratio, long_sample.request_count, threshold_ratio
        ),
        request_count=long_sample.request_count,
        samples=dict(samples),
    )


_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


def rank(candidates: list[Candidate]) -> list[Candidate]:
    """Rank for human attention.

    Watchdog territory first -- the paging tiers already have an owner and a
    pager; surfacing them above the unwatched burns would bury the point.
    Within that, least budget remaining, then highest confidence.
    """
    return sorted(
        candidates,
        key=lambda c: (
            not c.is_watchdog_territory,
            c.budget_remaining_pct,
            _CONFIDENCE_RANK[c.confidence],
            -c.burn_rate,
        ),
    )

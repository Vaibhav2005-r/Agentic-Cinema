"""Stage 2: run the burn-rate table against every discovered service.

This module does the I/O; `burn_rate.py` does the arithmetic. Nothing here
calls a language model, which is what makes the whole detection layer
reproducible and unit-testable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Iterable

from . import tools
from .burn_rate import (
    TIERS,
    Candidate,
    RatioSample,
    Tier,
    evaluate,
    parse_duration,
    rank,
    required_windows,
)
from .discovery import DiscoveredService, Inventory
from .impact import describe_impact
from .promql import error_ratio_query, request_count_query

log = logging.getLogger(__name__)

#: Prometheus instant queries per service is 8 windows x 2 (ratio + count).
#: Bounded so a 50-service stack does not open 800 concurrent requests.
DEFAULT_CONCURRENCY = 8


def scalar(payload: Any) -> float | None:
    """Pull a single number out of whatever shape query_prometheus returned."""
    if payload is None:
        return None
    if isinstance(payload, (int, float)):
        return float(payload)
    if isinstance(payload, str):
        try:
            return float(payload)
        except ValueError:
            return None
    if isinstance(payload, dict):
        for key in ("value", "values", "result", "data"):
            if key in payload:
                return scalar(payload[key])
        return None
    if isinstance(payload, list):
        if not payload:
            return None
        # A Prometheus instant vector sample is [timestamp, "value"].
        if len(payload) == 2 and isinstance(payload[0], (int, float)):
            return scalar(payload[1])
        return scalar(payload[0])
    return None


@dataclass
class DetectionSettings:
    concurrency: int = DEFAULT_CONCURRENCY
    min_requests: float = 100.0
    include_paging_tiers: bool = False
    #: Tier table to evaluate against. `burn_rate.compress()` returns a scaled
    #: one for demos and integration tests against a freshly started stack.
    tiers: tuple[Tier, ...] = TIERS
    #: The SLO compliance window, sampled for budget-remaining.
    slo_window: str = "30d"

    @property
    def windows(self) -> tuple[str, ...]:
        return required_windows(self.tiers, self.slo_window)

    @property
    def compressed(self) -> bool:
        return self.tiers is not TIERS


async def _sample_window(
    caller, prom_uid: str, service: DiscoveredService, window: str
) -> RatioSample | None:
    profile = service.profile
    try:
        ratio_payload = await caller.call(
            tools.QUERY_PROMETHEUS,
            tools.instant_query(prom_uid, error_ratio_query(profile, service.name, window)),
        )
        ratio = scalar(ratio_payload)
        if ratio is None:
            return None
        count_payload = await caller.call(
            tools.QUERY_PROMETHEUS,
            tools.instant_query(prom_uid, request_count_query(profile, service.name, window)),
        )
        count = scalar(count_payload)
    except Exception as exc:  # noqa: BLE001 - one bad window must not kill the sweep
        log.debug("sampling %s@%s failed: %s", service.name, window, exc)
        return None

    # A ratio can legitimately exceed 1 only through a broken query; clamp
    # rather than raise, and let the low request count drive confidence down.
    return RatioSample(window=window, error_ratio=min(max(ratio, 0.0), 1.0), request_count=count)


async def sample_service(
    caller,
    prom_uid: str,
    service: DiscoveredService,
    windows: tuple[str, ...] = None,
) -> dict[str, RatioSample]:
    windows = windows or required_windows()
    results = await asyncio.gather(
        *(_sample_window(caller, prom_uid, service, w) for w in windows)
    )
    return {s.window: s for s in results if s is not None}


async def detect(
    caller,
    inventory: Inventory,
    settings: DetectionSettings | None = None,
) -> list[Candidate]:
    """Evaluate every service and return candidates, ranked for attention."""
    settings = settings or DetectionSettings()
    prom_uid = inventory.datasources.prometheus_uid
    if not prom_uid:
        return []

    semaphore = asyncio.Semaphore(settings.concurrency)

    async def one(service: DiscoveredService) -> Candidate | None:
        async with semaphore:
            samples = await sample_service(caller, prom_uid, service, settings.windows)
        if not samples:
            log.debug("no samples for %s", service.name)
            return None

        watchdog_window = settings.tiers[-1].long_window
        long_sample = samples.get(watchdog_window) or next(iter(samples.values()))
        if (long_sample.request_count or 0) < settings.min_requests:
            # Too little traffic to say anything honest about.
            return None

        return evaluate(
            service.name,
            samples,
            service.slo.target,
            sli=service.slo.sli,
            provisional=service.slo.provisional,
            tiers=settings.tiers,
            slo_window=parse_duration(settings.slo_window),
        )

    candidates = [c for c in await asyncio.gather(*(one(s) for s in inventory.services)) if c]

    if not settings.include_paging_tiers:
        # The paging tiers already have an owner and a pager. Reporting them
        # here would bury the burns nobody is looking at.
        candidates = [c for c in candidates if c.is_watchdog_territory]

    return rank(candidates)


def describe(candidates: Iterable[Candidate], inventory: Inventory | None = None) -> str:
    """Human-readable ranked table for the CLI and the demo terminal.

    With an inventory in hand each row also carries what the burn means to a
    viewer or a production, because "2.3x" is actionable to an SRE and opaque
    to a duty manager.
    """
    rows = list(candidates)
    if not rows:
        return "No burn-rate candidates. Every service is inside its budget."

    profiles = (
        {svc.name: svc.profile for svc in inventory.services} if inventory else {}
    )

    header = (
        f"{'SERVICE':<28} {'TIER':<11} {'BURN':>6} {'WINDOWS':<10} "
        f"{'BUDGET':>8} {'CONF':<7} SLO"
    )
    lines = [header, "-" * len(header)]
    for c in rows:
        exhaustion = ""
        if c.projected_exhaustion:
            exhaustion = f"  exhausts {c.projected_exhaustion:%Y-%m-%d}"
        lines.append(
            f"{c.service:<28} {c.tier.name:<11} {c.burn_rate:>5.1f}x "
            f"{'/'.join(c.windows):<10} {c.budget_remaining_pct:>7.1f}% "
            f"{c.confidence:<7} "
            f"{'provisional' if c.provisional else 'defined'}"
            f"{exhaustion}"
        )
        profile = profiles.get(c.service)
        if profile is not None:
            lines.append(f"    -> {describe_impact(c, profile).sentence()}")
    return "\n".join(lines)

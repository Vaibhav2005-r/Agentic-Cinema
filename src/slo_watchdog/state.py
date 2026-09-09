"""Findings must dedupe across runs.

A sweep on a schedule sees the same slow burn every time it runs. Without a
state store the agent files a fresh incident each sweep, which is exactly the
alert-fatigue problem it exists to avoid.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Finding

log = logging.getLogger(__name__)

#: How long a filed finding stays suppressed. Long enough that a multi-day slow
#: burn produces one incident, short enough that a genuinely new episode after a
#: quiet week is reported again.
DEFAULT_COOLDOWN = timedelta(days=3)


def fingerprint(finding: Finding) -> str:
    """Identity of a *problem*, not of an observation.

    Deliberately excludes the burn rate: a burn drifting from 2.1x to 2.4x is
    the same problem, and including the number would defeat the dedupe.
    """
    return f"{finding.service}:{finding.sli}:{finding.tier}"


@dataclass
class SeenFinding:
    fingerprint: str
    first_seen: datetime
    last_seen: datetime
    times_seen: int
    incident_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "first_seen": self.first_seen.isoformat(),
            "last_seen": self.last_seen.isoformat(),
            "times_seen": self.times_seen,
            "incident_id": self.incident_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SeenFinding":
        return cls(
            fingerprint=data["fingerprint"],
            first_seen=datetime.fromisoformat(data["first_seen"]),
            last_seen=datetime.fromisoformat(data["last_seen"]),
            times_seen=int(data.get("times_seen", 1)),
            incident_id=data.get("incident_id"),
        )


@dataclass
class StateStore:
    """A JSON file. Deliberately not a database -- one process, one writer."""

    path: Path
    cooldown: timedelta = DEFAULT_COOLDOWN
    seen: dict[str, SeenFinding] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path, cooldown: timedelta = DEFAULT_COOLDOWN) -> "StateStore":
        store = cls(path=Path(path), cooldown=cooldown)
        if store.path.exists():
            try:
                raw = json.loads(store.path.read_text())
            except json.JSONDecodeError:
                log.warning("state file %s is corrupt; starting fresh", store.path)
                return store
            for entry in raw.get("seen", []):
                try:
                    item = SeenFinding.from_dict(entry)
                except (KeyError, ValueError):
                    continue
                store.seen[item.fingerprint] = item
        return store

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {"seen": [s.to_dict() for s in self.seen.values()]}, indent=2
            )
        )
        return self.path

    def is_suppressed(self, finding: Finding, now: datetime | None = None) -> bool:
        """True when this problem was already reported inside the cooldown."""
        now = now or datetime.now(timezone.utc)
        previous = self.seen.get(fingerprint(finding))
        if previous is None or previous.incident_id is None:
            return False
        return (now - previous.last_seen) < self.cooldown

    def record(self, finding: Finding, now: datetime | None = None) -> SeenFinding:
        now = now or datetime.now(timezone.utc)
        key = fingerprint(finding)
        previous = self.seen.get(key)
        if previous is None:
            entry = SeenFinding(
                fingerprint=key, first_seen=now, last_seen=now, times_seen=1
            )
        else:
            entry = previous
            entry.last_seen = now
            entry.times_seen += 1
        # Carry an existing incident forward so repeat sightings append to it
        # rather than opening a second one.
        if finding.incident_id:
            entry.incident_id = finding.incident_id
        elif entry.incident_id:
            finding.incident_id = entry.incident_id
        self.seen[key] = entry
        return entry

    def partition(
        self, findings: list[Finding], now: datetime | None = None
    ) -> tuple[list[Finding], list[Finding]]:
        """Split into (to report, suppressed as already known)."""
        fresh, suppressed = [], []
        for finding in findings:
            (suppressed if self.is_suppressed(finding, now) else fresh).append(finding)
        return fresh, suppressed

    def prune(self, older_than: timedelta = timedelta(days=30), now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        stale = [k for k, v in self.seen.items() if (now - v.last_seen) > older_than]
        for key in stale:
            del self.seen[key]
        return len(stale)

"""The evidence bundle: structured artifacts, not prose.

Every claim the agent makes ships with a deeplink back into Grafana so a human
can verify it in one click. That is the difference between an agent you trust
and a chatbot that hallucinated a service name.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Literal

from .burn_rate import Candidate, Confidence

EvidenceKind = Literal["log_pattern", "trace", "panel_image", "metric", "deeplink"]


@dataclass
class Evidence:
    """One piece of correlated support for a hypothesis."""

    kind: EvidenceKind
    summary: str
    source: str
    detail: dict[str, Any] = field(default_factory=dict)
    grafana_link: str | None = None


@dataclass
class ServiceSLO:
    """An SLO target for one service SLI.

    `provisional=True` means no SLO was defined in Grafana and we derived one
    from RED metrics -- the highest-value finding class, because it surfaces
    services nobody thought to instrument an alert for.
    """

    service: str
    sli: str
    target: float
    provisional: bool
    source: str
    success_query: str | None = None
    total_query: str | None = None


@dataclass
class Finding:
    """A confirmed, investigated problem -- the agent's unit of output."""

    service: str
    sli: str
    slo_target: float
    burn_rate: float
    windows: tuple[str, str]
    budget_remaining_pct: float
    projected_exhaustion: datetime | None
    provisional: bool
    evidence: list[Evidence] = field(default_factory=list)
    hypothesis: str = ""
    confidence: Confidence = "low"
    grafana_links: list[str] = field(default_factory=list)
    incident_id: str | None = None
    annotation_created: bool = False
    tier: str = ""
    dismissed: bool = False
    dismissal_reason: str | None = None

    @classmethod
    def from_candidate(cls, candidate: Candidate) -> "Finding":
        """Lift a deterministic detection into a findings record.

        The numbers are copied verbatim from the detector; the agent may only
        add narrative, evidence and links -- never edit the arithmetic.
        """
        return cls(
            service=candidate.service,
            sli=candidate.sli,
            slo_target=candidate.slo_target,
            burn_rate=candidate.burn_rate,
            windows=candidate.windows,
            budget_remaining_pct=candidate.budget_remaining_pct,
            projected_exhaustion=candidate.projected_exhaustion,
            provisional=candidate.provisional,
            confidence=candidate.confidence,
            tier=candidate.tier.name,
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.projected_exhaustion is not None:
            data["projected_exhaustion"] = self.projected_exhaustion.isoformat()
        data["windows"] = list(self.windows)
        return data


@dataclass
class SweepReport:
    """The output of one scheduled run (stage 7)."""

    started_at: datetime
    finished_at: datetime | None = None
    services_discovered: int = 0
    candidates_detected: int = 0
    findings: list[Finding] = field(default_factory=list)
    dismissed: list[Finding] = field(default_factory=list)
    dry_run: bool = True
    errors: list[str] = field(default_factory=list)

    @property
    def incidents_created(self) -> int:
        return sum(1 for f in self.findings if f.incident_id)

    @property
    def annotations_created(self) -> int:
        return sum(1 for f in self.findings if f.annotation_created)

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "services_discovered": self.services_discovered,
            "candidates_detected": self.candidates_detected,
            "findings_confirmed": len(self.findings),
            "incidents_created": self.incidents_created,
            "annotations_created": self.annotations_created,
            "dry_run": self.dry_run,
            "findings": [f.to_dict() for f in self.findings],
            "dismissed": [f.to_dict() for f in self.dismissed],
            "errors": self.errors,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

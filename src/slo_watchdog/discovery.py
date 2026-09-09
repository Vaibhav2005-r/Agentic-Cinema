"""Stage 1: build a service inventory.

Nothing here decides whether a service is healthy; it decides what exists and
what we are allowed to measure. The point of the sweep is that it starts from
an inventory rather than from an alert, so this stage is what makes the agent
able to find the surfaces nobody instrumented.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import tools
from .models import ServiceSLO
from .promql import PROFILES, MetricProfile, service_discovery_query

log = logging.getLogger(__name__)

#: Grafana SLO's generated recording rules carry this prefix, which is how we
#: tell a real, human-defined SLO from one we invented.
SLO_RULE_PREFIX = "grafana_slo"

#: Assumed target when a service has no SLO defined. Flagged `provisional`
#: everywhere it surfaces so a reviewer never mistakes it for a real objective.
PROVISIONAL_TARGET = 0.999


@dataclass
class Datasources:
    prometheus_uid: str | None = None
    loki_uid: str | None = None
    names: dict[str, str] = field(default_factory=dict)


@dataclass
class DiscoveredService:
    name: str
    profile: MetricProfile
    slo: ServiceSLO
    dashboard_uid: str | None = None
    dashboard_title: str | None = None

    @property
    def provisional(self) -> bool:
        return self.slo.provisional


@dataclass
class Inventory:
    datasources: Datasources
    services: list[DiscoveredService] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def provisional_count(self) -> int:
        return sum(1 for s in self.services if s.provisional)

    def summary(self) -> str:
        total = len(self.services)
        real = total - self.provisional_count
        return (
            f"{total} services; {real} with defined SLOs, "
            f"{self.provisional_count} provisional"
        )


#: Envelope keys mcp-grafana wraps list responses in. It is not consistent:
#: list_datasources returns {"datasources": [...]}, other tools return a bare
#: list or {"result": [...]}, so we check the known names and then fall back to
#: "the dict has exactly one list in it, use that".
_LIST_KEYS = (
    "datasources", "dashboards", "result", "results",
    "data", "items", "values", "metrics", "labels",
)


def _as_list(payload: Any) -> list[Any]:
    """Normalise any mcp-grafana list response into a plain list."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in _LIST_KEYS:
            inner = payload.get(key)
            if isinstance(inner, list):
                return inner
        lists = [v for v in payload.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
        return [payload]
    return [payload]


#: Grafana Cloud provisions Loki datasources that are not application logs.
#: Picking the first `loki` by id lands on alert-state-history, which contains
#: no service logs at all and makes stage 4 silently useless.
_LOKI_DEPRIORITISED = ("alert-state-history", "usage-insights", "cardinality")


def _rank_loki(uid: str, name: str, is_default: bool) -> tuple[int, int]:
    """Lower sorts first."""
    haystack = f"{uid} {name}".lower()
    if any(marker in haystack for marker in _LOKI_DEPRIORITISED):
        return (2, 0)
    if "logs" in haystack:
        return (0, 0 if is_default else 1)
    return (1, 0 if is_default else 1)


async def find_datasources(caller) -> Datasources:
    payload = await caller.call(tools.LIST_DATASOURCES, {})
    found = Datasources()
    prometheus: list[tuple[tuple[int, int], str]] = []
    loki: list[tuple[tuple[int, int], str]] = []

    for entry in _as_list(payload):
        if not isinstance(entry, dict):
            continue
        ds_type = str(entry.get("type", "")).lower()
        uid = entry.get("uid") or entry.get("id")
        name = str(entry.get("name", ""))
        is_default = bool(entry.get("isDefault"))
        if not uid:
            continue
        uid = str(uid)
        found.names[uid] = name
        if ds_type == "prometheus":
            prometheus.append(((0 if is_default else 1, 0), uid))
        elif ds_type == "loki":
            loki.append((_rank_loki(uid, name, is_default), uid))

    if prometheus:
        found.prometheus_uid = min(prometheus)[1]
    if loki:
        found.loki_uid = min(loki)[1]
    return found


async def probe_profiles(caller, prometheus_uid: str) -> list[MetricProfile]:
    """Keep only the metric families this stack actually exports."""
    payload = await caller.call(
        tools.LIST_METRIC_NAMES, tools.metric_names(prometheus_uid)
    )
    available = {str(m) for m in _as_list(payload) if isinstance(m, (str, int))}
    if not available:
        log.warning("no metric names returned; falling back to every known profile")
        return list(PROFILES)
    return [p for p in PROFILES if p.total_metric in available]


def _extract_label(sample: Any, label: str) -> str | None:
    if isinstance(sample, str):
        return sample
    if isinstance(sample, dict):
        metric = sample.get("metric")
        if isinstance(metric, dict) and label in metric:
            return str(metric[label])
        if label in sample:
            return str(sample[label])
    return None


async def list_services(
    caller, prometheus_uid: str, profile: MetricProfile
) -> list[str]:
    """Every service reporting this metric family.

    Tries the cheap label-values endpoint first and falls back to an aggregating
    instant query, because label enumeration can be restricted on locked-down
    stacks while `query_prometheus` stays available.
    """
    try:
        payload = await caller.call(
            tools.LIST_LABEL_VALUES,
            tools.label_values(prometheus_uid, profile.service_label, profile.total_metric),
        )
        names = [n for n in (_extract_label(s, profile.service_label) for s in _as_list(payload)) if n]
        if names:
            return sorted(set(names))
    except Exception as exc:  # noqa: BLE001 - fall back rather than abort the sweep
        log.debug("label enumeration failed for %s: %s", profile.name, exc)

    payload = await caller.call(
        tools.QUERY_PROMETHEUS,
        tools.instant_query(prometheus_uid, service_discovery_query(profile)),
    )
    names = [
        n
        for n in (_extract_label(s, profile.service_label) for s in _as_list(payload))
        if n
    ]
    return sorted(set(names))


async def find_defined_slos(caller, prometheus_uid: str) -> dict[str, float]:
    """Services with a real, human-defined SLO in Grafana SLO.

    Grafana SLO splits this across two recording rules and joins them on a
    uuid: `grafana_slo_info` carries the SLO's name, `grafana_slo_objective`
    carries its target. Neither alone names the service, so both are needed.

    The service is taken from a `service` label when the SLO carries one, and
    otherwise from the SLO's name -- "drm-license availability" -> drm-license.
    """
    targets: dict[str, float] = {}

    async def series(expr: str) -> list[Any]:
        try:
            return _as_list(
                await caller.call(
                    tools.QUERY_PROMETHEUS, tools.instant_query(prometheus_uid, expr)
                )
            )
        except Exception as exc:  # noqa: BLE001 - a stack with no SLOs is normal
            log.debug("SLO query %r failed: %s", expr, exc)
            return []

    def labels(sample: Any) -> dict[str, Any]:
        if not isinstance(sample, dict):
            return {}
        metric = sample.get("metric")
        return metric if isinstance(metric, dict) else sample

    def value(sample: Any) -> float | None:
        raw = sample.get("value") if isinstance(sample, dict) else None
        if isinstance(raw, list) and len(raw) == 2:
            raw = raw[1]
        try:
            return float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    # uuid -> service name
    names: dict[str, str] = {}
    for sample in await series("grafana_slo_info"):
        meta = labels(sample)
        uuid = meta.get("grafana_slo_uuid")
        if not uuid:
            continue
        service = meta.get("service") or meta.get("service_name")
        if not service:
            name = str(meta.get("grafana_slo_name", ""))
            # Strip a trailing SLI word, so "drm-license availability" resolves.
            service = re.sub(
                r"\s+(availability|latency|errors?|slo)$", "", name, flags=re.I
            ).strip()
        if service:
            names[str(uuid)] = str(service)

    # uuid -> target
    for sample in await series("grafana_slo_objective"):
        meta = labels(sample)
        uuid = str(meta.get("grafana_slo_uuid", ""))
        target = value(sample)
        service = names.get(uuid)
        if service and target is not None and 0.0 < target < 1.0:
            targets[service] = target

    return targets


async def map_dashboards(caller) -> dict[str, tuple[str, str]]:
    """service -> (dashboard uid, title), best effort.

    Used to anchor annotations on a panel a human already looks at.
    """
    mapping: dict[str, tuple[str, str]] = {}
    try:
        payload = await caller.call(tools.SEARCH_DASHBOARDS, {"query": ""})
    except Exception as exc:  # noqa: BLE001
        log.debug("dashboard search failed: %s", exc)
        return mapping

    for entry in _as_list(payload):
        if not isinstance(entry, dict):
            continue
        uid, title = entry.get("uid"), entry.get("title", "")
        if not uid or not title:
            continue
        slug = re.sub(r"[^a-z0-9]", "", str(title).lower())
        mapping[slug] = (str(uid), str(title))
    return mapping


#: Below this, a substring match is more likely to be a coincidence than a
#: real association -- "ad" would otherwise bind to the "adservice" dashboard.
MIN_FUZZY_MATCH = 5


def _match_dashboard(
    service: str, dashboards: dict[str, tuple[str, str]]
) -> tuple[str | None, str | None]:
    """Bind a service to the dashboard a human already looks at.

    Exact slug match wins. Otherwise take the *longest* overlapping candidate
    rather than the first one iteration happens to reach, so results do not
    depend on dictionary order and the most specific dashboard wins.
    """
    slug = re.sub(r"[^a-z0-9]", "", service.lower())
    if not slug:
        return None, None
    if slug in dashboards:
        return dashboards[slug]
    if len(slug) < MIN_FUZZY_MATCH:
        return None, None

    best: tuple[int, tuple[str, str]] | None = None
    for key, value in dashboards.items():
        if len(key) < MIN_FUZZY_MATCH:
            continue
        if slug in key or key in slug:
            score = len(key)
            if best is None or score > best[0]:
                best = (score, value)
    return best[1] if best else (None, None)


async def discover(caller) -> Inventory:
    """Stage 1 end to end."""
    datasources = await find_datasources(caller)
    inventory = Inventory(datasources=datasources)

    if not datasources.prometheus_uid:
        inventory.errors.append(
            "no Prometheus datasource found; cannot detect burn rates"
        )
        return inventory

    prom = datasources.prometheus_uid
    profiles = await probe_profiles(caller, prom)
    if not profiles:
        inventory.errors.append(
            "no known RED metric family present. Add a MetricProfile in promql.py "
            "matching this stack's request counter."
        )
        return inventory

    defined = await find_defined_slos(caller, prom)
    dashboards = await map_dashboards(caller)

    seen: set[str] = set()
    for profile in profiles:
        for name in await list_services(caller, prom, profile):
            if name in seen:
                continue
            seen.add(name)
            target = defined.get(name)
            slo = ServiceSLO(
                service=name,
                sli="availability",
                target=target if target is not None else PROVISIONAL_TARGET,
                provisional=target is None,
                source="grafana-slo" if target is not None else "derived-from-red",
            )
            uid, title = _match_dashboard(name, dashboards)
            inventory.services.append(
                DiscoveredService(
                    name=name,
                    profile=profile,
                    slo=slo,
                    dashboard_uid=uid,
                    dashboard_title=title,
                )
            )

    inventory.services.sort(key=lambda s: s.name)
    return inventory

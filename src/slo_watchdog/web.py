"""HTTP layer: a live console for the sweep.

Everything here is a thin wrapper over the same functions the CLI calls, so the
page cannot show anything the terminal could not. No detection logic lives in
the web layer.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import chaos as chaos_module
from .burn_rate import compress
from .detect import DetectionSettings
from .discovery import Inventory
from .impact import describe_impact
from .mcp_client import GrafanaConfig, open_session
from .sweep import run_detection

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"

#: One sweep at a time. Two concurrent sweeps would double the Prometheus load
#: and interleave their MCP sessions for no benefit.
_lock = asyncio.Lock()

_state: dict[str, Any] = {
    "last_sweep": None,
    "inventory": None,
    "running": False,
    "error": None,
}


def _serialise_candidate(candidate, profiles) -> dict[str, Any]:
    profile = profiles.get(candidate.service)
    impact = describe_impact(candidate, profile) if profile else None
    return {
        "service": candidate.service,
        "tier": candidate.tier.name,
        "severity": candidate.severity,
        "burn_rate": round(candidate.burn_rate, 2),
        "short_burn_rate": round(candidate.short_burn_rate, 2),
        "windows": list(candidate.windows),
        "threshold": candidate.tier.threshold,
        "budget_remaining_pct": (
            None if candidate.budget_is_estimate
            else round(candidate.budget_remaining_pct, 1)
        ),
        "budget_coverage": round(candidate.budget_coverage, 3),
        "budget_is_estimate": candidate.budget_is_estimate,
        "projected_exhaustion": (
            candidate.projected_exhaustion.isoformat()
            if candidate.projected_exhaustion and not candidate.budget_is_estimate
            else None
        ),
        "provisional": candidate.provisional,
        "slo_target": candidate.slo_target,
        "confidence": candidate.confidence,
        "requests": candidate.request_count,
        "impact": impact.sentence() if impact else None,
        "one_in": impact.one_in if impact else None,
        "per_day": round(impact.failures_per_day) if impact and impact.failures_per_day else None,
        "unit": profile.unit if profile else "request",
        "watchdog_territory": candidate.is_watchdog_territory,
    }


def _serialise_inventory(inventory: Inventory) -> dict[str, Any]:
    return {
        "total": len(inventory.services),
        "defined": len(inventory.services) - inventory.provisional_count,
        "provisional": inventory.provisional_count,
        "errors": inventory.errors,
        "datasources": {
            "prometheus": inventory.datasources.prometheus_uid,
            "loki": inventory.datasources.loki_uid,
        },
        "services": [
            {
                "name": s.name,
                "profile": s.profile.name,
                "provisional": s.provisional,
                "target": s.slo.target,
                "source": s.slo.source,
                "dashboard_uid": s.dashboard_uid,
                "unit": s.profile.unit,
            }
            for s in inventory.services
        ],
    }


async def perform_sweep(compress_factor: float | None, slo_window: str) -> dict[str, Any]:
    """Stages 1-2 against live Grafana, through MCP."""
    settings = DetectionSettings()
    if compress_factor:
        settings.tiers = compress(compress_factor)
        settings.slo_window = slo_window

    config = GrafanaConfig.from_env()
    started = datetime.now(timezone.utc)
    async with open_session(config) as caller:
        inventory, candidates = await run_detection(caller, settings)

    profiles = {s.name: s.profile for s in inventory.services}
    quiet = sorted(set(profiles) - {c.service for c in candidates})

    return {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
        "grafana_url": config.url,
        "windows": {t.name: f"{t.long_window}/{t.short_window}" for t in settings.tiers},
        "compressed": bool(compress_factor),
        "inventory": _serialise_inventory(inventory),
        "findings": [_serialise_candidate(c, profiles) for c in candidates],
        "quiet": quiet,
    }


def create_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse

    app = FastAPI(title="SLO Watchdog", docs_url="/api/docs")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        return JSONResponse(_state)

    @app.post("/api/sweep")
    async def sweep(compress: float | None = 288, slo_window: str = "2h"):
        if _lock.locked():
            raise HTTPException(409, "a sweep is already running")
        async with _lock:
            _state["running"] = True
            _state["error"] = None
            try:
                result = await perform_sweep(compress, slo_window)
                _state["last_sweep"] = result
                _state["inventory"] = result["inventory"]
                return JSONResponse(result)
            except Exception as exc:  # noqa: BLE001 - surfaced to the page
                log.exception("sweep failed")
                _state["error"] = str(exc)
                raise HTTPException(500, str(exc)) from exc
            finally:
                _state["running"] = False

    @app.get("/api/scenarios")
    async def scenarios():
        path = chaos_module.DEFAULT_CONFIG
        active = chaos_module._load(path).get("injected", {})
        return JSONResponse({
            "scenarios": [
                {
                    "name": s.name,
                    "service": s.service,
                    "burn": s.target_burn_rate,
                    "fraction": s.error_fraction(),
                    "one_in": s.one_in(),
                    "description": s.description,
                    "duration_seconds": s.duration_seconds,
                    "active": s.service in active,
                }
                for s in chaos_module.SCENARIOS.values()
            ]
        })

    @app.post("/api/scenarios/{name}")
    async def toggle(name: str, enabled: bool = True):
        if name not in chaos_module.SCENARIOS:
            raise HTTPException(404, f"unknown scenario {name}")
        applied = chaos_module.apply(chaos_module.DEFAULT_CONFIG, [name], enabled=enabled)
        return JSONResponse({"name": name, "enabled": enabled,
                             "service": applied[0].service})

    @app.get("/api/health")
    async def health():
        """Proves the MCP connection, which is what the whole project rests on."""
        from . import tools as toolnames
        try:
            config = GrafanaConfig.from_env()
            async with open_session(config) as caller:
                listing = await caller.session.list_tools()
                names = {t.name for t in listing.tools}
                return JSONResponse({
                    "ok": True,
                    "grafana_url": config.url,
                    "tools_advertised": len(names),
                    "required_present": sum(1 for t in toolnames.REQUIRED_TOOLS if t in names),
                    "required_total": len(toolnames.REQUIRED_TOOLS),
                })
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)

    return app


async def serve(host: str = "127.0.0.1", port: int = 8080) -> int:
    """Await uvicorn rather than calling uvicorn.run().

    The CLI already runs inside asyncio.run(), and uvicorn.run() starts an
    event loop of its own -- nesting them fails outright.
    """
    import uvicorn

    print(f"SLO Watchdog console -> http://{host}:{port}")
    config = uvicorn.Config(create_app(), host=host, port=port, log_level="warning")
    await uvicorn.Server(config).serve()
    return 0

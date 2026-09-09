"""Command line entry point.

    slo-watchdog doctor              verify the MCP connection and tool surface
    slo-watchdog discover            stage 1 only: what services exist
    slo-watchdog sweep               stages 1-2: ranked candidates, no LLM
    slo-watchdog sweep --agent       the full seven-stage run
    slo-watchdog sweep --replay F    re-run a recorded sweep offline
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from .agent import AgentSettings, DEFAULT_MODEL
from .burn_rate import compress
from .detect import DetectionSettings, describe
from .discovery import discover
from .mcp_client import (
    FixtureRecorder,
    GrafanaConfig,
    ReplayToolCaller,
    open_session,
)
from .observability import setup as setup_telemetry
from .state import StateStore
from .sweep import run_detection, run_sweep, write_report
from . import tools as toolnames


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"):
        if candidate.exists():
            load_dotenv(candidate)
            return


async def cmd_doctor(args: argparse.Namespace) -> int:
    """Verify the MCP server is reachable and exposes what the sweep needs.

    Run this first. mcp-grafana silently registers nothing for a category you
    forgot in --enabled-tools, so a missing tool otherwise shows up much later
    as an empty result rather than an error.
    """
    config = GrafanaConfig.from_env()
    print(f"Grafana URL      {config.url}")
    print(f"MCP binary       {config.binary}")
    print(f"Enabled tools    {config.enabled_tools}\n")

    async with open_session(config) as caller:
        listing = await caller.session.list_tools()
        available = {t.name for t in listing.tools}
        print(f"Server advertises {len(available)} tools.\n")

        missing = [t for t in toolnames.REQUIRED_TOOLS if t not in available]
        for name in toolnames.REQUIRED_TOOLS:
            print(f"  {'OK  ' if name in available else 'MISS'}  {name}")
        print()
        for name in toolnames.OPTIONAL_TOOLS:
            print(f"  {'OK  ' if name in available else 'skip'}  {name}  (optional)")

        if args.schemas:
            print("\nArgument schemas for the tools this project calls:")
            for tool in listing.tools:
                if tool.name in set(toolnames.REQUIRED_TOOLS) | set(toolnames.OPTIONAL_TOOLS):
                    props = (tool.inputSchema or {}).get("properties", {})
                    print(f"  {tool.name}: {sorted(props)}")

        if missing:
            print(
                f"\nMissing {len(missing)} required tool(s): {', '.join(missing)}\n"
                "Add the owning category to --enabled-tools in mcp_client.py.",
                file=sys.stderr,
            )
            return 1

        print("\nAll required tools present.")
        return 0


async def _caller_for(args: argparse.Namespace):
    """Either a replayed fixture or a live MCP session."""
    if args.replay:
        return ReplayToolCaller.from_file(Path(args.replay), strict=args.strict), None
    config = GrafanaConfig.from_env()
    recorder = FixtureRecorder(path=Path(args.record)) if args.record else None
    return None, (config, recorder)


async def cmd_discover(args: argparse.Namespace) -> int:
    replay, live = await _caller_for(args)
    if replay is not None:
        inventory = await discover(replay)
    else:
        config, recorder = live
        async with open_session(config, recorder) as caller:
            inventory = await discover(caller)
        if recorder:
            print(f"fixtures written to {recorder.save()}")

    print(inventory.summary())
    for error in inventory.errors:
        print(f"  ! {error}")
    print()
    for service in inventory.services:
        flag = "provisional" if service.provisional else f"SLO {service.slo.target:.4g}"
        dash = f"  dashboard={service.dashboard_uid}" if service.dashboard_uid else ""
        print(f"  {service.name:<30} {service.profile.name:<12} {flag}{dash}")
    return 0


async def cmd_sweep(args: argparse.Namespace) -> int:
    detection = DetectionSettings(include_paging_tiers=args.include_paging)
    if args.compress:
        # Same thresholds and the same arithmetic on shorter windows, so a
        # freshly started stack can produce a real finding in minutes rather
        # than days. Burn rate is a rate; only the history needed changes.
        detection.tiers = compress(args.compress)
        detection.slo_window = args.slo_window
        print(f"[compressed x{args.compress:g}] windows: "
              + ", ".join(f"{t.name}={t.long_window}/{t.short_window}"
                          for t in detection.tiers)
              + f"; budget window={detection.slo_window}\n")
    agent_settings = AgentSettings(model=args.model, dry_run=not args.execute)
    state = StateStore.load(Path(args.state)) if args.state else None

    replay, live = await _caller_for(args)

    if replay is not None:
        if args.agent:
            print("--agent needs a live MCP session; replay covers stages 1-2 only.",
                  file=sys.stderr)
            return 2
        inventory, candidates = await run_detection(replay, detection)
        print(inventory.summary())
        print()
        print(describe(candidates, inventory))
        return 0

    config, recorder = live
    async with open_session(config, recorder) as caller:
        if not args.agent:
            inventory, candidates = await run_detection(caller, detection)
            print(inventory.summary())
            print()
            print(describe(candidates, inventory))
            if recorder:
                print(f"\nfixtures written to {recorder.save()}")
            return 0

        telemetry = setup_telemetry()
        telemetry.metrics.model = args.model
        with telemetry.span("slo_watchdog.sweep", dry_run=agent_settings.dry_run) as root:
            report = await run_sweep(
                caller,
                config=config,
                agent_settings=agent_settings,
                detection_settings=detection,
                use_agent=True,
                max_candidates=args.max_candidates,
                state=state,
                telemetry=telemetry,
                max_retries=args.max_retries,
                pace_seconds=args.pace,
            )
            telemetry.metrics.candidates_detected = report.candidates_detected
            telemetry.metrics.findings_confirmed = len(report.findings)
            telemetry.metrics.findings_dismissed = len(report.dismissed)
            telemetry.metrics.incidents_created = report.incidents_created
            telemetry.metrics.annotations_created = report.annotations_created
            telemetry.finish(root)
        telemetry.shutdown()

    if recorder:
        recorder.save()

    mode = "DRY RUN - no writes performed" if agent_settings.dry_run else "LIVE"
    print(f"\n[{mode}]")
    print(f"services discovered  {report.services_discovered}")
    print(f"candidates detected  {report.candidates_detected}")
    print(f"findings confirmed   {len(report.findings)}")
    print(f"dismissed            {len(report.dismissed)}")
    print(f"incidents created    {report.incidents_created}")
    print(f"annotations created  {report.annotations_created}")
    print(f"mcp tool calls       {telemetry.metrics.mcp_tool_calls}")
    cost = telemetry.metrics.sweep_cost_usd
    if cost is not None:
        print(f"sweep cost           ${cost:.4f}")
    elif telemetry.metrics.prompt_tokens:
        print(f"sweep cost           unpriced model ({args.model}); "
              "add a rate to MODEL_PRICING")

    for finding in report.findings:
        print(f"\n  {finding.service}  {finding.burn_rate:.1f}x  "
              f"{finding.budget_remaining_pct:.1f}% budget  ({finding.confidence})")
        if finding.hypothesis:
            print(f"    {finding.hypothesis[:300]}")
        for link in finding.grafana_links:
            print(f"    link: {link}")

    for finding in report.dismissed:
        print(f"\n  dismissed {finding.service}: {finding.dismissal_reason}")

    for error in report.errors:
        print(f"  ! {error}", file=sys.stderr)

    if args.out:
        print(f"\nreport written to {write_report(report, Path(args.out))}")
    return 0


async def cmd_chaos(args: argparse.Namespace) -> int:
    """Drive the OpenTelemetry demo's feature flags."""
    from . import chaos

    if args.list:
        print(chaos.describe())
        return 0

    path = Path(args.config)
    if args.reset:
        chaos.reset(path)
        print(f"all scenarios disabled in {path}")
        return 0

    if not args.scenario:
        print("nothing to do: pass a scenario, --reset or --list", file=sys.stderr)
        return 2

    applied = chaos.apply(path, args.scenario, enabled=not args.off)
    verb = "disabled" if args.off else "enabled"
    for scenario in applied:
        print(f"{verb} {scenario.name}: {scenario.service} at "
              f"{scenario.error_fraction() * 100:.3f}% "
              f"(1 in {scenario.one_in():,}; targets a {scenario.target_burn_rate}x burn)")
    print(f"\nthe simulator re-reads {path} each tick; no restart needed.")
    return 0


async def cmd_state(args: argparse.Namespace) -> int:
    store = StateStore.load(Path(args.path))
    if args.clear:
        store.seen.clear()
        store.save()
        print("state cleared")
        return 0
    if not store.seen:
        print("no findings recorded yet")
        return 0
    print(f"{len(store.seen)} tracked finding(s) in {args.path}\n")
    for entry in sorted(store.seen.values(), key=lambda e: e.last_seen, reverse=True):
        incident = entry.incident_id or "-"
        print(f"  {entry.fingerprint:<48} seen x{entry.times_seen:<3} "
              f"last {entry.last_seen:%Y-%m-%d %H:%M}  incident={incident}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slo-watchdog",
        description="Find the reliability problems nobody paged on.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="verify the MCP connection and tool surface")
    doctor.add_argument("--schemas", action="store_true",
                        help="also print each tool's argument names")
    doctor.set_defaults(func=cmd_doctor)

    def add_io(p: argparse.ArgumentParser) -> None:
        p.add_argument("--replay", metavar="FILE", help="replay a recorded sweep offline")
        p.add_argument("--record", metavar="FILE", help="record this sweep to fixtures")
        p.add_argument("--strict", action="store_true",
                       help="fail on a fixture miss instead of returning nothing")

    disco = sub.add_parser("discover", help="stage 1: list services and their SLOs")
    add_io(disco)
    disco.set_defaults(func=cmd_discover)

    sweep = sub.add_parser("sweep", help="run a sweep")
    add_io(sweep)
    sweep.add_argument("--agent", action="store_true",
                       help="run stages 3-6 (costs tokens; needs a live session)")
    sweep.add_argument("--execute", action="store_true",
                       help="actually write incidents and annotations (default: dry run)")
    sweep.add_argument("--model", default=DEFAULT_MODEL)
    sweep.add_argument("--max-candidates", type=int, default=5)
    sweep.add_argument("--max-retries", type=int, default=3,
                       help="retries when the model is rate limited or busy")
    sweep.add_argument("--pace", type=float, default=0.0, metavar="SECONDS",
                       help="wait between candidates; free-tier Gemini allows "
                            "5 requests/minute, so try 60 on a free key")
    sweep.add_argument("--include-paging", action="store_true",
                       help="also report burns that conventional alerts already catch")
    sweep.add_argument("--compress", type=float, metavar="N", default=None,
                       help="scale every window down by N for a live demo "
                            "(288 turns the 3d/6h watchdog window into 15m/75s)")
    sweep.add_argument("--slo-window", default="30d",
                       help="SLO compliance window (use e.g. 2h with --compress)")
    sweep.add_argument("--out", metavar="FILE", help="write the JSON report here")
    sweep.add_argument("--state", metavar="FILE", default=None,
                       help="dedupe findings across runs using this state file")
    sweep.set_defaults(func=cmd_sweep)

    chaos = sub.add_parser("chaos", help="drive the OTel demo's failure injection")
    chaos.add_argument("scenario", nargs="*", help="slow_burn, provisional, red_herring")
    chaos.add_argument("--config", default="mediastack/scenarios.json",
                       help="path to the simulator's scenario file")
    chaos.add_argument("--off", action="store_true", help="disable instead of enable")
    chaos.add_argument("--reset", action="store_true", help="disable every scenario")
    chaos.add_argument("--list", action="store_true", help="describe the scenarios")
    chaos.set_defaults(func=cmd_chaos)

    state = sub.add_parser("state", help="inspect the dedupe state store")
    state.add_argument("--path", default="state/findings.json")
    state.add_argument("--clear", action="store_true")
    state.set_defaults(func=cmd_state)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    _load_dotenv()
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

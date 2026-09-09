"""Grafana MCP transport for the deterministic stages.

Stages 1-2 call MCP directly, with no LLM in the loop -- so they need a plain
client session rather than an ADK toolset. Stages 3-6 use `build_toolset()`,
which hands the same server to the agent through ADK.

Connection choice (see README section "MCP connection"): the hosted endpoint at
mcp.grafana.com authenticates interactively via OAuth 2.1, which is fatal for an
agent whose whole premise is running unattended on a schedule. We use the
open-source grafana/mcp-grafana server with a service-account token instead.
"""

from __future__ import annotations

import json
import os
import shutil
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Protocol

#: Scoped deliberately. All 60+ tools in context is a large, noisy prompt that
#: degrades tool selection; these are the categories the pipeline actually calls.
#:
#: Every category here is load-bearing -- dropping one silently removes a tool
#: the sweep depends on, and mcp-grafana gives no error for a tool that was
#: never registered:
#:   search      -> search_dashboards                (stage 1)
#:   datasource  -> list_datasources                 (stage 1)
#:   prometheus  -> query_prometheus, list_prometheus_*  (stages 1-3)
#:   loki        -> query_loki_logs, query_loki_patterns (stage 4)
#:   sift        -> find_error_pattern_logs          (stage 4)
#:   dashboard   -> get_dashboard_panel_queries      (stage 3)
#:   incident    -> create_incident                  (stage 6)
#:   annotations -> create_annotation                (stage 6)
#:   navigation  -> generate_deeplink                (stage 6)
#:   rendering   -> get_panel_image                  (stage 6, optional evidence)
#:   alerting    -> list_alert_rules, to check what already pages
ENABLED_TOOL_CATEGORIES = (
    "search,datasource,prometheus,loki,sift,dashboard,"
    "incident,annotations,navigation,rendering,alerting"
)

#: Tools mcp-grafana classifies as writes. We do *not* pass --disable-write,
#: because stages 4 and 6 need three of these; the service account's RBAC is
#: what bounds the blast radius instead.
#:
#: `find_error_pattern_logs` is a write tool despite reading logs -- it creates
#: a Sift investigation. Running the investigator read-only therefore costs you
#: log-pattern analysis, so `--dry-run` gates on our side, not the server's.
WRITE_TOOLS = frozenset(
    {
        "create_incident",
        "add_activity_to_incident",
        "update_incident",
        "create_annotation",
        "update_annotation",
        "delete_annotation",
        "find_error_pattern_logs",
        "find_slow_requests",
    }
)

#: Tools the responder is allowed to call. Anything else it attempts is a bug.
RESPONDER_TOOLS = frozenset(
    {"create_incident", "add_activity_to_incident", "create_annotation", "generate_deeplink"}
)


class ToolCaller(Protocol):
    """What the detection stages need from a transport."""

    async def call(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass
class GrafanaConfig:
    url: str
    service_account_token: str
    binary: str = "mcp-grafana"
    enabled_tools: str = ENABLED_TOOL_CATEGORIES
    timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "GrafanaConfig":
        url = os.environ.get("GRAFANA_URL", "").strip()
        token = os.environ.get("GRAFANA_SA_TOKEN", "").strip()
        missing = [
            name
            for name, value in (("GRAFANA_URL", url), ("GRAFANA_SA_TOKEN", token))
            if not value
        ]
        if missing:
            raise RuntimeError(
                f"missing required environment variable(s): {', '.join(missing)}. "
                "Copy .env.example to .env and fill it in, or run with --replay."
            )
        return cls(
            url=url.rstrip("/"),
            service_account_token=token,
            binary=os.environ.get("MCP_GRAFANA_BINARY", "mcp-grafana"),
        )

    def server_env(self) -> dict[str, str]:
        # Inherit PATH so the child process can find its own dependencies, but
        # pass only the Grafana credentials beyond that.
        return {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "GRAFANA_URL": self.url,
            "GRAFANA_SERVICE_ACCOUNT_TOKEN": self.service_account_token,
        }

    def server_args(self) -> list[str]:
        return ["--enabled-tools", self.enabled_tools]

    def check_binary(self) -> None:
        if shutil.which(self.binary) is None:
            raise RuntimeError(
                f"{self.binary!r} not found on PATH. Install it with:\n"
                "  go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@latest\n"
                "or see https://github.com/grafana/mcp-grafana#installation"
            )


# ---------------------------------------------------------------------------
# Live transport
# ---------------------------------------------------------------------------


@dataclass
class LiveToolCaller:
    """Direct MCP tool calls, optionally recording every response to fixtures."""

    session: Any
    timeout_seconds: int = 60
    recorder: "FixtureRecorder | None" = None

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self.session.call_tool(
            name,
            arguments,
            read_timeout_seconds=timedelta(seconds=self.timeout_seconds),
        )
        payload = _unwrap(result)
        if self.recorder is not None:
            self.recorder.record(name, arguments, payload)
        return payload


def _unwrap(result: Any) -> Any:
    """Turn an MCP CallToolResult into plain Python.

    mcp-grafana returns JSON in a text content block; structured content is
    preferred when the server provides it.
    """
    if getattr(result, "isError", False):
        text = " ".join(
            getattr(c, "text", "") for c in getattr(result, "content", []) or []
        )
        raise RuntimeError(f"MCP tool error: {text.strip() or 'unknown'}")

    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured

    chunks: list[Any] = []
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if text is None:
            continue
        try:
            chunks.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            chunks.append(text)
    if not chunks:
        return None
    return chunks[0] if len(chunks) == 1 else chunks


@asynccontextmanager
async def open_session(
    config: GrafanaConfig, recorder: "FixtureRecorder | None" = None
) -> AsyncIterator[LiveToolCaller]:
    """Open one MCP session for the duration of a single sweep.

    Bounded per sweep on purpose: holding a long-lived session across scheduled
    runs is where ADK/MCP session churn bites.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    config.check_binary()
    params = StdioServerParameters(
        command=config.binary, args=config.server_args(), env=config.server_env()
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield LiveToolCaller(
            session=session,
            timeout_seconds=config.timeout_seconds,
            recorder=recorder,
        )


def build_toolset(config: GrafanaConfig, read_only: bool = False):
    """The ADK toolset handed to the agent for stages 3-6.

    Recreated per sweep rather than held open, for the same reason as above.
    """
    from google.adk.tools.mcp_tool import McpToolset
    from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
    from mcp import StdioServerParameters

    config.check_binary()
    tool_filter = None
    if read_only:
        tool_filter = (lambda tool, ctx=None: tool.name not in WRITE_TOOLS)

    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=config.binary,
                args=config.server_args(),
                env=config.server_env(),
            ),
            timeout=config.timeout_seconds,
        ),
        tool_filter=tool_filter,
    )


# ---------------------------------------------------------------------------
# Fixtures: record a golden run, then never depend on live chaos again
# ---------------------------------------------------------------------------


def _key(name: str, arguments: dict[str, Any]) -> str:
    return json.dumps({"tool": name, "args": arguments}, sort_keys=True)


@dataclass
class FixtureRecorder:
    """Snapshot raw MCP responses so the demo and tests run offline."""

    path: Path
    entries: dict[str, Any] = field(default_factory=dict)

    def record(self, name: str, arguments: dict[str, Any], payload: Any) -> None:
        self.entries[_key(name, arguments)] = payload

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, indent=2, default=str))
        return self.path


@dataclass
class ReplayToolCaller:
    """Serve recorded MCP responses. No network, no tokens, no LLM cost."""

    entries: dict[str, Any]
    strict: bool = False
    misses: list[str] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path, strict: bool = False) -> "ReplayToolCaller":
        return cls(entries=json.loads(Path(path).read_text()), strict=strict)

    async def call(self, name: str, arguments: dict[str, Any]) -> Any:
        key = _key(name, arguments)
        if key in self.entries:
            return self.entries[key]
        self.misses.append(key)
        if self.strict:
            raise KeyError(f"no fixture recorded for {name} with {arguments}")
        return None

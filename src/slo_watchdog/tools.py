"""Tool names and argument shapes for grafana/mcp-grafana.

Argument shapes here are transcribed from the mcp-grafana Go structs
(`tools/*.go`, the `json:"..."` tags), not inferred -- an earlier version
guessed them and omitted `endTime`, which is required on `query_prometheus`
and would have failed every query the detector makes.

Centralised for two reasons. First, the agent's prompts must name tools that
actually exist -- `list_loki_patterns` does not; the tool is
`query_loki_patterns`. Second, mcp-grafana is a moving target and its README
does not document argument names, so `slo-watchdog doctor` validates every name
in `REQUIRED_TOOLS` against the live server's advertised schema. Run it first.
"""

from __future__ import annotations

from typing import Any

# --- stage 1: discover -----------------------------------------------------
LIST_DATASOURCES = "list_datasources"
LIST_METRIC_NAMES = "list_prometheus_metric_names"
LIST_LABEL_VALUES = "list_prometheus_label_values"
SEARCH_DASHBOARDS = "search_dashboards"

# --- stage 2-3: detect and triage ------------------------------------------
QUERY_PROMETHEUS = "query_prometheus"
GET_PANEL_QUERIES = "get_dashboard_panel_queries"
LIST_ALERT_RULES = "list_alert_rules"

# --- stage 4: correlate ----------------------------------------------------
QUERY_LOKI_LOGS = "query_loki_logs"
QUERY_LOKI_PATTERNS = "query_loki_patterns"  # NOT list_loki_patterns
FIND_ERROR_PATTERN_LOGS = "find_error_pattern_logs"  # Sift; classified as write

# --- stage 6: act ----------------------------------------------------------
CREATE_INCIDENT = "create_incident"
ADD_ACTIVITY_TO_INCIDENT = "add_activity_to_incident"
CREATE_ANNOTATION = "create_annotation"
GENERATE_DEEPLINK = "generate_deeplink"
GET_PANEL_IMAGE = "get_panel_image"

#: Everything the sweep depends on. `doctor` fails loudly if any is absent,
#: which is the only way to catch an --enabled-tools category you forgot.
REQUIRED_TOOLS: tuple[str, ...] = (
    LIST_DATASOURCES,
    LIST_METRIC_NAMES,
    LIST_LABEL_VALUES,
    SEARCH_DASHBOARDS,
    QUERY_PROMETHEUS,
    GET_PANEL_QUERIES,
    QUERY_LOKI_LOGS,
    QUERY_LOKI_PATTERNS,
    CREATE_INCIDENT,
    CREATE_ANNOTATION,
    GENERATE_DEEPLINK,
)

#: Nice to have; the sweep degrades gracefully without these.
OPTIONAL_TOOLS: tuple[str, ...] = (
    FIND_ERROR_PATTERN_LOGS,
    GET_PANEL_IMAGE,
    LIST_ALERT_RULES,
    ADD_ACTIVITY_TO_INCIDENT,
)


def instant_query(
    datasource_uid: str, expr: str, at: str = "now"
) -> dict[str, Any]:
    """Arguments for a single-point `query_prometheus` call.

    `endTime` is REQUIRED by mcp-grafana even for an instant query -- omitting
    it fails the call, which would take the entire detection engine with it.
    Relative forms ("now", "now-1.5h") are accepted alongside RFC3339.
    `startTime` is ignored when queryType is "instant", so it is not sent.
    """
    return {
        "datasourceUid": datasource_uid,
        "expr": expr,
        "queryType": "instant",
        "endTime": at,
    }


def range_query(
    datasource_uid: str,
    expr: str,
    start: str,
    end: str = "now",
    step_seconds: int = 60,
) -> dict[str, Any]:
    """Arguments for a range `query_prometheus` call.

    Both `startTime` and `stepSeconds` are required when queryType is "range".
    """
    return {
        "datasourceUid": datasource_uid,
        "expr": expr,
        "queryType": "range",
        "startTime": start,
        "endTime": end,
        "stepSeconds": step_seconds,
    }


def selector(**labels: str) -> dict[str, Any]:
    """Build one mcp-grafana `Selector`.

    `matches` is a list of these objects, not a list of PromQL strings -- a
    bare metric name is silently not a selector.
    """
    return {
        "filters": [
            {"name": name, "value": value, "type": "="}
            for name, value in labels.items()
        ]
    }


def label_values(
    datasource_uid: str, label: str, metric: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"datasourceUid": datasource_uid, "labelName": label}
    if metric:
        args["matches"] = [selector(__name__=metric)]
    return args


def metric_names(datasource_uid: str, regex: str = "", limit: int = 5000) -> dict[str, Any]:
    """List metric names.

    `limit` defaults to 10 server-side, which silently truncates discovery on
    any real stack, so it is always sent explicitly.
    """
    args: dict[str, Any] = {"datasourceUid": datasource_uid, "limit": limit}
    if regex:
        args["regex"] = regex
    return args


def loki_query(
    datasource_uid: str, logql: str, start: str, end: str = "now", limit: int = 100
) -> dict[str, Any]:
    """Arguments for `query_loki_logs` / `query_loki_patterns`.

    The expression parameter is `logql`, not `expr` as on the Prometheus side,
    and the time bounds are `startRfc3339` / `endRfc3339`.
    """
    return {
        "datasourceUid": datasource_uid,
        "logql": logql,
        "startRfc3339": start,
        "endRfc3339": end,
        "limit": limit,
    }


def annotation(
    dashboard_uid: str,
    text: str,
    start_ms: int,
    end_ms: int,
    tags: list[str] | None = None,
    panel_id: int | None = None,
) -> dict[str, Any]:
    """A region annotation spanning the burn window.

    `time`/`timeEnd` are epoch milliseconds; passing both makes it a region
    rather than a point, which is what puts the marker across the anomaly.
    """
    args: dict[str, Any] = {
        "dashboardUid": dashboard_uid,
        "text": text,
        "time": start_ms,
        "timeEnd": end_ms,
        "tags": tags or ["slo-watchdog"],
    }
    if panel_id is not None:
        args["panelId"] = panel_id
    return args


def incident(title: str, severity: str = "minor", status: str = "active") -> dict[str, Any]:
    """`create_incident` requires title, severity, roomPrefix, isDrill, status."""
    return {
        "title": title,
        "severity": severity,
        "status": status,
        "roomPrefix": "slo-watchdog",
        "isDrill": False,
    }


def deeplink_dashboard(dashboard_uid: str, time_range: dict[str, str] | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {
        "resourceType": "dashboard",
        "dashboardUid": dashboard_uid,
    }
    if time_range:
        args["timeRange"] = time_range
    return args

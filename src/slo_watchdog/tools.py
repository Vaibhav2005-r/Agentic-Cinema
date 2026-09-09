"""Tool names and argument shapes for grafana/mcp-grafana.

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


def instant_query(datasource_uid: str, expr: str, at: str | None = None) -> dict[str, Any]:
    """Arguments for a single-point `query_prometheus` call."""
    args: dict[str, Any] = {
        "datasourceUid": datasource_uid,
        "expr": expr,
        "queryType": "instant",
    }
    if at:
        args["startTime"] = at
    return args


def label_values(
    datasource_uid: str, label: str, matches: str | None = None
) -> dict[str, Any]:
    args: dict[str, Any] = {"datasourceUid": datasource_uid, "labelName": label}
    if matches:
        args["matches"] = [matches]
    return args


def metric_names(datasource_uid: str, regex: str = "", limit: int = 5000) -> dict[str, Any]:
    args: dict[str, Any] = {"datasourceUid": datasource_uid, "limit": limit}
    if regex:
        args["regex"] = regex
    return args

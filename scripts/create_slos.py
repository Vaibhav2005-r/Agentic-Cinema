#!/usr/bin/env python3
"""Define the mediastack SLOs in Grafana SLO.

The watchdog's central contrast is between services with a real, human-defined
objective and services with none. That only means something if some of the SLOs
are genuinely defined, so this creates four of them and deliberately leaves the
other seven bare.

There is no MCP tool for Grafana SLO, so this talks to the plugin's REST API
directly. It is setup, not part of the sweep -- the agent still reads these back
through `query_prometheus` like any other recording rule.

    python scripts/create_slos.py            # create the four
    python scripts/create_slos.py --list     # show what exists
    python scripts/create_slos.py --delete   # remove the ones we created

Note: each SLO compiles to 10-12 recording rules, so four costs roughly 50
active series against the free tier's 10,000.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

API = "api/plugins/grafana-slo-app/resources/v1/slo"

#: Marks the SLOs this script owns, so --delete never touches a hand-made one.
#: Keys must match Prometheus label syntax, [a-zA-Z_][a-zA-Z0-9_]* -- a hyphen
#: is rejected.
OWNER_LABEL = ("managed_by", "slo_watchdog_demo")


@dataclass(frozen=True)
class SLODefinition:
    service: str
    metric: str
    success_outcome: str
    target: float
    description: str

    @property
    def name(self) -> str:
        return f"{self.service} availability"

    def success_query(self) -> str:
        return f'{self.metric}{{service_name="{self.service}", outcome="{self.success_outcome}"}}'

    def total_query(self) -> str:
        return f'{self.metric}{{service_name="{self.service}"}}'


#: Four of eleven. Playback, DRM, CDN and manifests are the surfaces a
#: streaming platform actually writes objectives for; subtitles, transcode and
#: the render farm are exactly the ones nobody does -- which is the point.
DEFINITIONS: tuple[SLODefinition, ...] = (
    SLODefinition("playback-api", "playback_session_start_total", "success", 0.999,
                  "Playback sessions that start successfully."),
    SLODefinition("drm-license", "drm_license_request_total", "success", 0.999,
                  "DRM licences issued to entitled viewers."),
    SLODefinition("cdn-edge", "segment_request_total", "200", 0.9995,
                  "Video segments delivered from the edge."),
    SLODefinition("manifest-service", "http_request_total", "200", 0.999,
                  "Manifest requests served successfully."),
)


def _request(method: str, base: str, token: str, path: str = "", data: dict | None = None):
    url = f"{base.rstrip('/')}/{API}{path}"
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            body = response.read().decode()
            return response.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:300]}


def payload(definition: SLODefinition, datasource_uid: str) -> dict:
    return {
        "name": definition.name,
        "description": definition.description,
        "destinationDatasource": {"uid": datasource_uid},
        "objectives": [{"value": definition.target, "window": "28d"}],
        "labels": [
            {"key": OWNER_LABEL[0], "value": OWNER_LABEL[1]},
            {"key": "service", "value": definition.service},
        ],
        "query": {
            "type": "ratio",
            "ratio": {
                "successMetric": {"prometheusMetric": definition.success_query()},
                "totalMetric": {"prometheusMetric": definition.total_query()},
                "groupByLabels": [],
            },
        },
        # Deliberately no alerting. These objectives exist and nothing pages on
        # them, which is the situation the watchdog was built to find.
    }


def existing(base: str, token: str) -> list[dict]:
    _, body = _request("GET", base, token)
    return body.get("slos", []) if isinstance(body, dict) else []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="show existing SLOs")
    parser.add_argument("--delete", action="store_true",
                        help="remove only the SLOs this script created")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    except ImportError:
        pass

    base, token = os.environ.get("GRAFANA_URL", ""), os.environ.get("GRAFANA_SA_TOKEN", "")
    if not (base and token):
        print("error: set GRAFANA_URL and GRAFANA_SA_TOKEN (see .env.example)",
              file=sys.stderr)
        return 1

    current = existing(base, token)

    if args.list:
        print(f"{len(current)} SLO(s):")
        for slo in current:
            objectives = slo.get("objectives") or [{}]
            print(f"  {slo.get('name'):<34} target={objectives[0].get('value')}"
                  f"  uuid={slo.get('uuid')}")
        return 0

    if args.delete:
        owned = [
            s for s in current
            if any(label.get("key") == OWNER_LABEL[0]
                   and label.get("value") == OWNER_LABEL[1]
                   for label in (s.get("labels") or []))
        ]
        for slo in owned:
            code, _ = _request("DELETE", base, token, f"/{slo['uuid']}")
            print(f"deleted {slo['name']} -> {code}")
        print(f"{len(owned)} removed; {len(current) - len(owned)} left untouched")
        return 0

    # Discover the Prometheus datasource rather than hard-coding a Cloud uid.
    ds_req = urllib.request.Request(
        f"{base.rstrip('/')}/api/datasources",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(ds_req, timeout=45) as response:
        datasources = json.loads(response.read().decode())
    prometheus = next(
        (d for d in datasources if d.get("type") == "prometheus" and d.get("isDefault")),
        next((d for d in datasources if d.get("type") == "prometheus"), None),
    )
    if prometheus is None:
        print("error: no Prometheus datasource on this stack", file=sys.stderr)
        return 1
    print(f"destination datasource: {prometheus['uid']}\n")

    by_name = {s.get("name"): s for s in current}
    created = 0
    for definition in DEFINITIONS:
        if definition.name in by_name:
            print(f"  exists   {definition.name}")
            continue
        code, body = _request("POST", base, token, data=payload(definition, prometheus["uid"]))
        if code in (200, 201, 202):
            print(f"  created  {definition.name:<34} target={definition.target}")
            created += 1
        else:
            print(f"  FAILED   {definition.name}: {code} {body.get('error', '')}",
                  file=sys.stderr)

    total = len(existing(base, token))
    if created or total:
        print(f"\n{created} created. {total} of 11 services now carry an objective; "
              "the rest stay provisional on purpose.")
    return 0 if total else 1


if __name__ == "__main__":
    raise SystemExit(main())

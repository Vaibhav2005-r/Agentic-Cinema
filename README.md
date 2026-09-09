# SLO Watchdog

**An autonomous agent that finds the reliability problems nobody paged on.**

Most observability agents wait to be told something is wrong. They read a firing
alert and summarise it. This one has no alert to start from. It wakes on a
schedule, sweeps every service it can find in the Grafana stack, computes
error-budget burn deterministically, and hunts for the two things alerting is
structurally bad at:

1. **Slow burns.** A 2× burn rate never pages, but it quietly eats a month of
   error budget in days.
2. **Unwatched surfaces.** Services with no SLO defined at all — the ones nobody
   thought to instrument an alert for.

When it finds something it correlates metrics → logs → traces, writes a
root-cause hypothesis with evidence, opens a Grafana IRM incident, and annotates
the dashboard at the exact window, so the next human to look sees the agent's
marker in place.

> Your alerts tell you what broke. This tells you what's breaking.

Built on the [Google Agent Development Kit](https://google.github.io/adk-docs/)
and the [open-source Grafana MCP server](https://github.com/grafana/mcp-grafana).

---

## See it work, with no Grafana account

The repository ships a recorded sweep, so a full run works offline:

```bash
python3.12 -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/slo-watchdog sweep --replay fixtures/golden-sweep.json
```

```
11 services; 4 with defined SLOs, 7 provisional

SERVICE                      TIER          BURN WINDOWS      BUDGET CONF    SLO
-------------------------------------------------------------------------------
paymentservice               watchdog      2.3x 3d/6h         34.0% high    defined      exhausts 2026-09-14
recommendationservice        watchdog      1.8x 3d/6h         59.0% high    provisional  exhausts 2026-09-19
```

Eleven services. Two findings. Neither would page. One of them is on a service
with no SLO at all. And `cartservice` — which burnt at 9.5× over three days and
then recovered — is deliberately **not** reported, because the short window says
it is over.

```bash
./.venv/bin/python -m pytest        # 139 tests, no network, no tokens
```

---

## Architecture

Each scheduled run is a seven-stage pipeline. Stages 1–2 are deterministic
Python. Stages 3–6 are the agent. That split is the most important design
decision in the project.

```
                        ┌──────────────── deterministic Python ────────────────┐
   schedule ──▶  1 Discover  ──▶  2 Detect  ──▶  Candidate  ────┐
                 inventory       burn-rate math   (structured)  │
                 of services     multi-window                   │
                        └──────────────────────────────────────┘│
                                                                ▼
                        ┌──────────────────── the agent ──────────────────────┐
                        │  3 Triage ──▶ 4 Correlate ──▶ 5 Hypothesise         │
                        │     investigator (read-only)                        │
                        │                      │                              │
                        │                      ▼                              │
                        │              6 Act — responder                      │
                        │        create_incident · create_annotation          │
                        └─────────────────────────────────────────────────────┘
                                                │
                                                ▼
                                        7 Report (Python)
```

| # | Stage | Owner | Grafana MCP tools |
|---|-------|-------|-------------------|
| 1 | **Discover** — build a service inventory | Python | `list_datasources`, `list_prometheus_metric_names`, `list_prometheus_label_values`, `search_dashboards` |
| 2 | **Detect** — multi-window burn-rate math | Python | `query_prometheus` |
| 3 | **Triage** — real signal or noise? | Agent | `query_prometheus`, `get_dashboard_panel_queries` |
| 4 | **Correlate** — logs and traces for the window | Agent | `query_loki_logs`, `query_loki_patterns`, `find_error_pattern_logs` |
| 5 | **Hypothesise** — write the root-cause narrative | Agent | — |
| 6 | **Act** — file the incident, mark the dashboard | Agent | `create_incident`, `create_annotation`, `generate_deeplink`, `get_panel_image` |
| 7 | **Report** — bundle and emit | Python | — |

**Agent topology** is one root agent with two sub-agents — `investigator`
(stages 3–5, read-only) and `responder` (stage 6, the only agent permitted to
write). A six-agent swarm demos worse and debugs harder.

---

## Detection: multi-window, multi-burn-rate

Standard [Google SRE Workbook](https://sre.google/workbook/alerting-on-slos/)
burn-rate alerting, computed in Python from `query_prometheus` results. Burn
rate is `error_ratio / (1 - SLO)`: a burn rate of 1 exhausts exactly your budget
over the SLO window; 14.4 exhausts 2% of a 30-day budget in one hour.

| Tier | Burn rate | Long window | Short window | Normally |
|------|-----------|-------------|--------------|----------|
| Page | 14.4× | 1h | 5m | Alerts already catch this |
| Page | 6× | 6h | 30m | Alerts already catch this |
| Ticket | 3× | 1d | 2h | Often unrouted |
| **Watchdog** | **1×–3×** | **3d** | **6h** | **Nobody is looking** |

A candidate fires only when **both** the long and short windows exceed the
threshold. The short window is what stops the agent reporting a burn that has
already ended — the red-herring suppression above. Every short window is
exactly 1/12 of its long window, the Workbook's ratio; there is a test that
fails if that invariant is ever broken.

By default the sweep **excludes the two paging tiers entirely**. They already
have an owner and a pager. Reporting them would bury the point. Pass
`--include-paging` to see everything.

### Services with no SLO

For services with nothing defined, the watchdog derives a provisional SLI from
RED metrics and assumes 99.9%, flagging every such finding as `provisional`.
This is the highest-value finding class and the easiest to understand: *you have
eleven services and SLOs on four of them; here are two of the other seven that
are in trouble.*

### Low-traffic services

The Workbook is explicit that burn-rate alerting breaks down on low-traffic
services: at ten requests an hour, a single failure is a 10% error rate that
burns 13.9% of a 30-day budget. The arithmetic is real, so it fires.

The watchdog handles this with a one-sided z-score on the binomial proportion,
which is what separates a genuine 0.23% degradation across 300,000 requests from
three unlucky requests out of two hundred. Low-volume candidates are dropped
before they cost a token, and surviving ones carry an honest `confidence`.

---

## Why the math is not in the LLM

Language models are unreliable arithmetic engines, and the published numbers on
agentic root-cause analysis are sobering: LLM-agent methods score around
**11.34% accuracy on the OpenRCA benchmark**. An agent that both detects *and*
explains is compounding a weak step with a strong one.

So every ratio, threshold comparison and window calculation happens in Python
against raw PromQL results. The agent receives a structured candidate — service,
SLI, burn rate, windows, confidence — and reasons about **meaning, not numbers**.

Three things follow:

- **It is testable.** The detection layer has 139 tests and needs no network.
- **It is cheap.** Passing a computed candidate instead of raw time series cuts
  token cost by roughly an order of magnitude. A full sweep of fifteen services
  costs about **nine cents**.
- **It cannot lie about the numbers.** `_apply_agent_result` merges only
  narrative fields onto the detector's output. A hallucinated burn rate is
  physically unable to reach the report, and there is a test for it.

> *The agent doesn't do the math — it does the judgement.*

---

## Setup

### 0. Grafana Cloud (do this first)

1. Create a [free Grafana Cloud account](https://grafana.com/products/cloud/).
2. **Accept the Grafana Assistant terms as a stack administrator.** Nothing else
   works until this is done.
3. Confirm you have the Editor role or higher.
4. Create a service account and token, scoped to exactly:
   `datasources:read`, `datasources:query`, `dashboards:read`,
   `annotations:write`, and Editor for IRM incidents.

```bash
cp .env.example .env    # then fill in GRAFANA_URL and GRAFANA_SA_TOKEN
```

### 1. Install the MCP server

```bash
go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@latest
```

### 2. Verify the connection before anything else

```bash
./.venv/bin/slo-watchdog doctor --schemas
```

`doctor` checks every tool the sweep depends on and prints each one's argument
names. This matters more than it looks: mcp-grafana silently registers *nothing*
for a category missing from `--enabled-tools`, so a forgotten category shows up
much later as an empty result rather than an error.

### 3. Run

```bash
slo-watchdog discover                  # stage 1: what exists, what has an SLO
slo-watchdog sweep                     # stages 1-2: ranked candidates, no LLM
slo-watchdog sweep --agent             # the full run, dry-run by default
slo-watchdog sweep --agent --execute   # actually file incidents and annotations
slo-watchdog sweep --record fixtures/my-run.json   # snapshot a golden run
```

Writes are **off by default**. `--dry-run` is not a prompt instruction — it is a
`before_tool_callback` that intercepts every write tool before it reaches the
server, because prompt instructions are not a security boundary.

---

## Demo environment

An agent that finds nothing is not a demo, so this is the first thing to build.

```bash
git clone https://github.com/open-telemetry/opentelemetry-demo.git
cd opentelemetry-demo && cp -r ../demo .
docker compose -f docker-compose.yml -f demo/docker-compose.override.yml up -d
```

Then drive the failure injection. Scenarios are specified by the **burn rate
they should produce**, not by a magic percentage, so the detector's answer can be
checked against what was asked for:

```bash
slo-watchdog chaos --list
slo-watchdog chaos slow_burn --config src/flagd/demo.flagd.json
```

| Scenario | Flag | Burn | Produces |
|----------|------|------|----------|
| `slow_burn` | `paymentServiceFailure` | 2.3× | The hero finding — under every paging threshold |
| `provisional` | `recommendationServiceCacheFailure` | 1.8× | A finding on a service with no SLO |
| `red_herring` | `cartServiceFailure` | 12× | A spike that recovers; the agent must stay quiet |

### Free-tier cardinality — read this before you deploy

The Grafana Cloud free tier allows **10,000 active series**. An untrimmed
OpenTelemetry demo will exhaust that quickly: it emits per-route, per-status,
per-container histograms across ~15 services, and every histogram bucket is its
own series.

`demo/otelcol-config.yaml` keeps the sweep's inputs and drops the rest — it
filters to the two metric families the SLIs need and deletes the unbounded
attributes (`http.route` is the expensive one; product IDs in the path make it
unbounded). Logs and traces pass through intact, since 50 GB/month is generous
next to 10k series.

Budget the rest of your allowance carefully: each Grafana SLO you define
compiles to 10–12 recording rules.

---

## The reflexive layer: an agent that is observable

The closing shot of the demo is a Grafana dashboard showing the agent's own
trace. `observability.py` instruments each sweep as one root span with child
spans per stage, and emits `candidates_detected`, `findings_confirmed`,
`incidents_created`, `mcp_tool_calls` and `sweep_cost_usd`.

Two things to know:

- **`TracerProvider` and `MeterProvider` are configured before the client is
  constructed.** Without them the SDK silently discards everything.
- **Install `agento11y` alone — not `agento11y-gemini`.** See below.

The whole module is a no-op when `AGENTO11Y_ENDPOINT` is unset, so the sweep
still runs on a machine with no access-policy token.

---

## Notes for anyone building on this

Four things cost real time to discover. They are fixed in this repo; they are
recorded here because the documentation does not mention them.

**1. `google-adk` and `agento11y-gemini` cannot coexist.**
`agento11y-gemini` pins `google-genai<2`; `google-adk` 2.8 requires `>=2.19`.
Installing the pair silently downgrades `google-genai` and `LlmAgent` stops
importing. Install `agento11y` alone — ADK already emits OpenTelemetry spans for
its own LLM calls, which is the layer worth instrumenting. Also pin
`opentelemetry-{api,sdk}==1.42.1`, since agento11y's floors are open-ended and
float above ADK's ceiling.

**2. `McpToolset` needs the `[mcp]` extra, and the failure is misleading.**
`pip install google-adk` alone gives you
`ImportError: cannot import name 'McpToolset'`, because ADK's `mcp_tool/__init__`
wraps its imports in a bare `try/except`. Worse, a plain `pip install mcp`
resolves to 2.x while ADK pins `mcp>=1.24,<2`. Install
`google-adk[mcp]` and let the resolver do it.

**3. Scope `--enabled-tools`, but not too far.** All 60+ tools in context
degrades tool selection — but the obvious eight-category list drops three tools
this pipeline needs: `get_dashboard_panel_queries` (category `dashboard`),
`find_error_pattern_logs` (`sift`) and `get_panel_image` (`rendering`). The
categories in `mcp_client.py` are each load-bearing; `slo-watchdog doctor` is
there to catch a mistake here.

**4. `find_error_pattern_logs` is a *write* tool.** It reads logs, but it creates
a Sift investigation, so `--disable-write` removes it. Running the investigator
read-only silently costs you log-pattern analysis. This is why the dry-run gate
lives in our code rather than on the server.

Also worth knowing: the hosted endpoint at `mcp.grafana.com` authenticates
interactively via **OAuth 2.1**. That is fine for building and fatal for an agent
whose premise is running unattended on a schedule. This project uses the
open-source server with a service-account token, which the sponsor requirement
explicitly accepts.

---

## Prior art

- **[Google SRE Workbook — Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/).**
  The source of the burn-rate table, the 1/12 short-window ratio, and the
  low-traffic pathology this project's confidence check exists to handle.
- **[RCAgent (CIKM '24)](https://dl.acm.org/doi/10.1145/3627673.3680016).**
  Tool-augmented autonomous agents for cloud RCA — formulates RCA as
  execution-based reasoning over traces, logs and metrics.
- **[Exploring LLM-based Agents for Root Cause Analysis](https://arxiv.org/abs/2403.04123)** and
  **[GALA](https://arxiv.org/html/2608.08968)**. Benchmarks put LLM-agent RCA
  accuracy around 11.34% on OpenRCA — the evidence behind keeping detection
  deterministic and using the model only for correlation and narrative.

Where this differs: the cited work starts from a known incident and explains it.
This starts from an inventory and decides *what deserves to be an incident* —
including on services with no SLO, which is a case the Workbook's table does not
cover at all.

---

## Repository layout

```
src/slo_watchdog/
  burn_rate.py     the math — pure functions, no I/O, no LLM
  promql.py        query construction; the agent never writes PromQL
  discovery.py     stage 1: service inventory, SLO detection
  detect.py        stage 2: sampling and evaluation
  agent.py         stages 3-6: root + investigator + responder
  sweep.py         stage 7: orchestration and reporting
  mcp_client.py    MCP transport, tool scoping, record/replay
  tools.py         verified tool names and argument shapes
  state.py         dedupe across scheduled runs
  chaos.py         failure injection, specified by target burn rate
  observability.py the reflexive layer
  cli.py           doctor · discover · sweep · chaos · state
demo/              OTel demo wiring with free-tier cardinality trimming
fixtures/          the recorded golden run
tests/             139 tests, offline
```

## Licence

Apache-2.0. See [LICENSE](LICENSE).

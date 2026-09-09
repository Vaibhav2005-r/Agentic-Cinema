# SLO Watchdog — reliability for the streaming pipeline

**An autonomous agent that finds the viewer-facing failures nobody paged on.**

Built for **Agentic Cinema** on the **Grafana Labs** track. Powered by Gemini
via the Google Agent Development Kit, and driven end to end by the
[Grafana MCP server](https://github.com/grafana/mcp-grafana).

---

## The problem

A streaming platform's worst failures are the ones that never page.

A DRM licence service denying 0.23% of requests looks like nothing on a
dashboard. No alert fires — it is nowhere near a paging threshold. But that is
**1 viewer in 435 who pressed play on content they paid for and got an error**.
At the volume of a mid-size platform, that is thousands of people a day, every
day, until someone happens to look.

The second failure mode is worse: nobody defines an SLO for subtitle delivery,
transcode workers, or the render farm. Those surfaces have no alerts at all,
because no one thought to write one.

Most observability agents wait to be handed a firing alert and summarise it.
This one has no alert to start from. It wakes on a schedule, sweeps every
service it can find through Grafana MCP, computes error-budget burn
deterministically, and hunts the two things alerting is structurally bad at:

1. **Slow burns** — a 2× burn never pages, but it eats a month of budget in days.
2. **Unwatched surfaces** — services with no SLO defined at all.

> Your alerts tell you what broke. This tells you what's breaking — and what it
> costs the audience.

---

## What a sweep produces

```bash
slo-watchdog sweep --replay fixtures/golden-sweep.json
```

```
11 services; 4 with defined SLOs, 7 provisional

SERVICE                      TIER          BURN WINDOWS      BUDGET CONF    SLO
-------------------------------------------------------------------------------
drm-license                  watchdog      2.3x 3d/6h         34.0% high    defined
    -> 1 in 435 license requests fail; about 1,334 a day -- a viewer was
       refused a licence for content they paid for
subtitle-service             watchdog      1.8x 3d/6h         59.0% high    provisional
    -> 1 in 556 subtitle fetches fail; about 372 a day -- a viewer who needs
       captions was served none (an accessibility failure)
```

Eleven services. Two findings, **neither of which would page**. One is on a
service with no SLO at all. And `transcode-worker` — which burnt at 9.5× for an
hour during a batch job and then recovered — is deliberately **not** reported,
because the short window says it is over.

Every burn rate is also stated in audience terms. "2.3×" is actionable to an
SRE and meaningless to a duty manager; "1 in 435 viewers refused a licence" is
actionable to both.

---

## Grafana MCP is the runtime spine

The agent has no Grafana SDK, no HTTP client, and no direct datasource access.
**Every read and every write goes through the Grafana MCP server.** Remove it
and the project does nothing at all.

| # | Stage | Owner | Grafana MCP tools called |
|---|-------|-------|--------------------------|
| 1 | **Discover** — build a service inventory | Python | `list_datasources`, `list_prometheus_metric_names`, `list_prometheus_label_values`, `search_dashboards` |
| 2 | **Detect** — multi-window burn-rate math | Python | `query_prometheus` |
| 3 | **Triage** — real signal or artefact? | Agent | `query_prometheus`, `get_dashboard_panel_queries` |
| 4 | **Correlate** — logs for the burn window | Agent | `query_loki_logs`, `query_loki_patterns`, `find_error_pattern_logs` |
| 5 | **Hypothesise** — write the root cause | Agent | — |
| 6 | **Act** — file and mark | Agent | `create_incident`, `create_annotation`, `generate_deeplink`, `get_panel_image` |
| 7 | **Report** — bundle and emit | Python | — |

Stages 1–2 open their own MCP session and call tools directly, with no LLM in
the loop. Stages 3–6 reach the same server through ADK's `McpToolset`. Verify
the whole surface before running anything:

```bash
slo-watchdog doctor --schemas
```

That checks all eleven required tools are present and prints each one's
argument names. It matters more than it looks: mcp-grafana silently registers
*nothing* for a category missing from `--enabled-tools`, so a forgotten
category surfaces much later as an empty result rather than an error.

---

## Architecture

```
                    ┌──────────── deterministic Python ────────────┐
 schedule ──▶ 1 Discover ──▶ 2 Detect ──▶ Candidate ───┐
              (MCP)          (MCP)        + audience   │
                                            impact     │
                    └──────────────────────────────────┘
                                                       ▼
                    ┌──────────────── the agent ───────────────────┐
                    │ 3 Triage ─▶ 4 Correlate ─▶ 5 Hypothesise     │
                    │        investigator (read-only, MCP)         │
                    │                    │                         │
                    │                    ▼                         │
                    │            6 Act — responder (MCP)           │
                    │   create_incident · create_annotation        │
                    └──────────────────────────────────────────────┘
                                        │
                                        ▼
                                7 Report (Python)
```

One root agent with two sub-agents — `investigator` (stages 3–5, read-only) and
`responder` (stage 6, the only agent permitted to write). A six-agent swarm
demos worse and debugs harder.

---

## Detection: multi-window, multi-burn-rate

Standard [Google SRE Workbook](https://sre.google/workbook/alerting-on-slos/)
burn-rate alerting, computed in Python from `query_prometheus` results. Burn
rate is `error_ratio / (1 - SLO)`: 1.0 exhausts exactly your budget over the SLO
window; 14.4 exhausts 2% of a 30-day budget in an hour.

| Tier | Burn rate | Long window | Short window | Normally |
|------|-----------|-------------|--------------|----------|
| Page | 14.4× | 1h | 5m | Alerts already catch this |
| Page | 6× | 6h | 30m | Alerts already catch this |
| Ticket | 3× | 1d | 2h | Often unrouted |
| **Watchdog** | **1×–3×** | **3d** | **6h** | **Nobody is looking** |

A candidate fires only when **both** windows exceed the threshold — the short
window is what suppresses the recovered transcode spike. Every short window is
exactly 1/12 of its long window, the Workbook's ratio, and a test fails if that
invariant is ever broken. By default the sweep **excludes the paging tiers
entirely**: they already have an owner and a pager, and reporting them would
bury the point.

**Services with no SLO** get a provisional SLI derived from their RED metrics at
an assumed 99.9%, flagged `provisional` everywhere it surfaces. This is the
highest-value finding class: *you have eleven services and SLOs on four; here
are two of the other seven that are in trouble.*

**Low-traffic services** are where burn-rate alerting is known to break down —
the Workbook's own example is ten requests an hour where one failure burns 13.9%
of a 30-day budget. The watchdog applies a one-sided z-score on the binomial
proportion, so three unlucky requests out of two hundred never becomes a crisis.

---

## Why the math is not in the LLM

Language models are unreliable arithmetic engines, and the published numbers on
agentic root-cause analysis are sobering: LLM-agent methods score around
**11.34% accuracy on the OpenRCA benchmark**. An agent that both detects *and*
explains compounds a weak step with a strong one.

So every ratio, threshold, window and impact figure is computed in Python from
raw PromQL. The agent receives a structured candidate and reasons about
**meaning, not numbers**.

- **It is testable.** 209 tests, 13 of them against the real MCP server binary.
- **It is cheap.** Passing a computed candidate instead of raw time series cuts
  token cost by roughly an order of magnitude — a full sweep costs about **nine cents**.
- **It cannot lie about the numbers.** `_apply_agent_result` merges only
  narrative fields. A hallucinated burn rate is physically unable to reach the
  report, and there is a test for it.

> *The agent doesn't do the math — it does the judgement.*

---

## Setup

### 1. Grafana Cloud

1. Create a [free account](https://grafana.com/products/cloud/).
2. **Accept the Grafana Assistant terms as a stack administrator.** Nothing
   works until this is done.
3. Create a service account token with exactly: `datasources:read`,
   `datasources:query`, `dashboards:read`, `annotations:write`, and Editor for
   IRM incidents.

```bash
cp .env.example .env      # fill in GRAFANA_URL and GRAFANA_SA_TOKEN
```

### 2. The MCP server

```bash
brew install mcp-grafana        # or: go install github.com/grafana/mcp-grafana/cmd/mcp-grafana@latest
slo-watchdog doctor             # verify before anything else
```

With the binary installed you can also run the integration suite, which needs
**no Grafana account** — mcp-grafana registers its tools statically, so it
advertises all 44 before any upstream call succeeds:

```bash
pytest tests/test_mcp_integration.py
```

That is what verifies every tool name and every argument shape this project
sends against the server's own schema, rather than against documentation.

### 3. The workload

`mediastack/` is a synthetic streaming platform — eleven services across
playback, DRM, CDN, post-production and VFX — that emits OTLP straight to
Grafana Cloud. One Python process, **no Docker**, and about 29 active series, so
the whole demo fits inside the free tier's 10,000-series cap with room for SLO
recording rules.

```bash
python mediastack/simulate.py --rate 40
```

Then inject a failure. Scenarios are specified by the **burn rate they should
produce**, not by a magic percentage, so the detector's answer can be checked
against what was asked for:

```bash
slo-watchdog chaos --list
slo-watchdog chaos drm_slow_burn
```

| Scenario | Service | Burn | Produces |
|----------|---------|------|----------|
| `drm_slow_burn` | `drm-license` | 2.3× | The hero finding — 1 in 435 viewers refused a licence |
| `subtitle_degradation` | `subtitle-service` | 1.8× | A finding on a service with no SLO — an accessibility failure |
| `transcode_spike` | `transcode-worker` | 12× | A batch that fails and recovers; the agent must stay quiet |
| `render_farm_burn` | `render-farm` | 2.9× | Frames failing quietly, burning artist days |

### 4. Run

```bash
slo-watchdog discover                  # what exists, what has an SLO
slo-watchdog sweep                     # ranked candidates, no LLM
slo-watchdog sweep --agent             # the full run, dry-run by default
slo-watchdog sweep --agent --execute   # actually file incidents and annotations
slo-watchdog sweep --record fixtures/my-run.json
```

Writes are **off by default**. `--dry-run` is not a prompt instruction — it is a
`before_tool_callback` that intercepts every write tool before it reaches the
server, because prompt instructions are not a security boundary.

---

## The reflexive layer: an agent that is observable

`observability.py` instruments each sweep as one root span with child spans per
stage, emitting `candidates_detected`, `findings_confirmed`, `incidents_created`,
`mcp_tool_calls` and `sweep_cost_usd` to Grafana Cloud AI Observability. It is a
no-op when `AGENTO11Y_ENDPOINT` is unset, so the sweep runs without it.

Two things to know: **providers before client** — `TracerProvider` and
`MeterProvider` must be configured before the agento11y client is constructed or
the SDK silently discards everything. And **install `agento11y` alone**, never
`agento11y-gemini` — see below.

---

## Notes for anyone building on this

Five things cost real time to discover. They are fixed here; the documentation
does not mention them.

**0. The dry-run gate must deny by shape, not by list.** The server advertises
44 tools, eight of which mutate — including `update_dashboard`,
`create_datasource` and `alerting_manage_rules`. An enumerated write-list looks
complete and silently misses whatever the next release adds, so `is_write_tool`
matches mutating verb prefixes as well as known names. An integration test
asserts that every mutating tool the server advertises is caught.

**1. `query_prometheus` requires `endTime`, even for an instant query.** Omit it
and every query fails — which takes the entire detection engine with it. The
argument shapes in `tools.py` are transcribed from mcp-grafana's Go structs
(`tools/*.go`, the `json:"..."` tags), not inferred. Related: `matches` on
`list_prometheus_label_values` is a list of `Selector` **objects**, not metric
name strings; Loki's expression parameter is `logql`, not `expr`; and
`list_prometheus_metric_names` defaults to a limit of **10**, which silently
truncates discovery.

**2. `google-adk` and `agento11y-gemini` cannot coexist.** The former requires
`google-genai>=2.19`, the latter pins `<2`. Installing the pair silently
downgrades genai until `LlmAgent` stops importing. Pin
`opentelemetry-{api,sdk}==1.42.1` too — agento11y's floors are open-ended and
float above ADK's ceiling.

**3. `McpToolset` needs the `[mcp]` extra, and the failure is misleading.**
`pip install google-adk` alone gives `ImportError: cannot import name
'McpToolset'`, because ADK wraps those imports in a bare `try/except`. A plain
`pip install mcp` also resolves to 2.x against ADK's `mcp>=1.24,<2` pin.

**4. Scope `--enabled-tools`, but not too far.** The obvious eight-category list
drops three tools this pipeline needs: `get_dashboard_panel_queries`
(`dashboard`), `find_error_pattern_logs` (`sift`) and `get_panel_image`
(`rendering`). Every category in `mcp_client.py` is load-bearing.

**5. `find_error_pattern_logs` is a *write* tool.** It reads logs but creates a
Sift investigation, so `--disable-write` removes it. Running the investigator
read-only silently costs you log-pattern analysis — which is why the dry-run
gate lives in our code rather than on the server.

Finally: the hosted endpoint at `mcp.grafana.com` authenticates interactively
via **OAuth 2.1**. Fine for building, fatal for an agent whose premise is running
unattended on a schedule. This project uses the open-source server with a
service-account token, which the sponsor requirement explicitly accepts.

---

## Prior art

- **[Google SRE Workbook — Alerting on SLOs](https://sre.google/workbook/alerting-on-slos/)** —
  source of the burn-rate table, the 1/12 short-window ratio, and the low-traffic
  pathology the confidence check exists to handle.
- **[RCAgent (CIKM '24)](https://dl.acm.org/doi/10.1145/3627673.3680016)** —
  tool-augmented autonomous agents for cloud root-cause analysis.
- **[Exploring LLM-based Agents for RCA](https://arxiv.org/abs/2403.04123)**,
  **[GALA](https://arxiv.org/html/2608.08968)** — benchmarks putting LLM-agent RCA
  near 11.34% on OpenRCA, the evidence behind keeping detection deterministic.

Where this differs: the cited work starts from a known incident and explains it.
This starts from an inventory and decides *what deserves to be an incident* —
including on services with no SLO, a case the Workbook's table does not cover at
all, and translates the result into audience impact rather than leaving it as a
ratio.

---

## Repository layout

```
src/slo_watchdog/
  burn_rate.py     the math — pure functions, no I/O, no LLM
  impact.py        burn rate -> "1 in 435 viewers refused a licence"
  promql.py        media SLI queries; the agent never writes PromQL
  discovery.py     stage 1: inventory, real vs provisional SLOs
  detect.py        stage 2: sampling and evaluation
  agent.py         stages 3-6: root + investigator + responder
  sweep.py         stage 7: orchestration and reporting
  mcp_client.py    MCP transport, tool scoping, record/replay
  tools.py         tool names and argument shapes, from the Go source
  state.py         dedupe across scheduled runs
  chaos.py         failure injection, specified by target burn rate
  observability.py the reflexive layer
  cli.py           doctor · discover · sweep · chaos · state
mediastack/        the synthetic streaming platform (no Docker)
fixtures/          the recorded golden run
tests/             209 tests (13 against a real mcp-grafana)
```

## Licence

Apache-2.0. See [LICENSE](LICENSE).

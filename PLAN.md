# Plan: semantic layer + evals for chatting with a local database

**Goal:** learn how accurately Claude answers questions about a local database, and how much a semantic layer helps compared with letting it write raw SQL.

## Decisions

| Area | Choice | Why |
|---|---|---|
| Semantic layer | dbt MetricFlow | Open source (Apache 2.0), basis of the Open Semantic Interchange standard, runs locally. Omni was the first preference but only runs in Omni's cloud. |
| Database | DuckDB | Single file, no server, well supported by dbt and MetricFlow. |
| Data | Synthetic marketing data | We control it, so every eval question has a known correct answer. |
| AI | Claude via the Anthropic SDK (`claude-opus-5-5`) | |
| Runtime | Docker (`compose.yaml`) | Everything runs in containers. |
| Later | LangGraph, OpenWebUI, LiteLLM, Langfuse | Same stack as the existing internal tool. |

## Already done

- `pyproject.toml` and `uv.lock`: Python 3.12 with anthropic, langgraph, dbt-core, dbt-duckdb, dbt-metricflow, duckdb, fastapi, langfuse, pyyaml, typer and rich. Several aren't used yet.
- `scripts/generate_data.py`: builds `data/warehouse.duckdb` (`raw` schema) with Jan–Jun 2026 data for a fictional running-shoe shop. The output is identical every run (seed 42). Verified on the host and in Docker.

  | Table | Rows |
  |---|---|
  | `raw.channels` | 7 |
  | `raw.campaigns` | 11 |
  | `raw.ad_spend` | 1,125 |
  | `raw.sessions` | 368,631 |
  | `raw.customers` | 7,617 |
  | `raw.orders` | 7,699 |

- Traps built into the data on purpose, to test the AI later:
  - Revenue is ambiguous: completed orders only come to €525k, while all orders including refunded and cancelled come to €597k.
  - Organic and direct sessions have no campaign.
  - Ad spend spikes in March (€56k vs about €29k in February) because of a spring sale across 3 channels.
  - Conversion rates differ a lot by channel, from 1.0% (TikTok) to 4.2% (newsletter).
  - One order (TikTok, €48.05) and its new customer fall on 1 July 2026 because the session started late on 30 June. "All revenue" and "revenue in H1" therefore differ, and any grouping by month has a July row.
- `Dockerfile`, `compose.yaml`, `.dockerignore`, `.gitignore`: the image builds and runs.
- Git repo started.
- Step 1 (dbt models) is done: `docker compose run --rm app dbt build` passes (6 marts in the `marts` schema, 28 tests). Staging models are ephemeral, so the warehouse has only `raw` and `marts`.
- Step 2 (MetricFlow) is done: semantic models and metrics are in `dbt/models/marts/semantic.yml`, and the time spine is `metricflow_time_spine`. `mf validate-configs` passes, and `mf query --metrics roas --group-by metric_time__month,channel__channel_name` matches hand-written SQL on all 43 rows. Run `mf` from `dbt/`: `docker compose run --rm -w /app/dbt app mf ...`.
  - Metric definitions chosen: "revenue" means net (completed orders only). `average_order_value` = net revenue / completed orders. `conversion_rate` = all orders / sessions. `roas` = net revenue / ad spend. `customer_acquisition_cost` = ad spend / new customers, grouped by acquisition channel (blended overall).
- Step 3 (golden set) is done: 45 questions in `evals/golden.yaml`. `docker compose run --rm app python scripts/check_golden.py` validates the file and prints every expected answer. Ambiguous questions expect the AI to answer with the default definition and state it (revenue = net).

**Not set up yet:** Anthropic API key (needed from step 4).

To run the data step in Docker:

```sh
docker compose build
docker compose run --rm app python scripts/generate_data.py
```

## Steps

### 1. dbt models

- Staging models over the `raw` tables.
- Fact and dimension tables: orders (with both gross and net revenue), sessions, ad spend, customers, campaigns and channels.
- Data tests: unique, not null, relationships, accepted values for order status.

**Done when:** `dbt build` passes inside the container.

### 2. Semantic layer (MetricFlow)

- Semantic models for orders, sessions, ad spend and customers. Their entities (order, session, visitor, customer, campaign, channel) are the keys MetricFlow joins on.
- Dimensions: time, channel, campaign, country, device, order status.
- A time spine model, which MetricFlow requires.
- Metrics: orders, gross revenue, net revenue, average order value, sessions, conversion rate, ad spend, cost per click, click-through rate, ROAS, customer acquisition cost, new customers.

**Learn:** how joins between models work, ratio vs derived metrics, grouping by `metric_time`.

**Done when:** `mf query --metrics roas --group-by metric_time__month,channel__channel_name` returns the expected numbers.

### 3. Golden question set

Build this before the agent, so the agent can't be tuned to the test.

- 30–50 questions in YAML.
- Each question has a category: simple lookup, time grain, filter, ratio, multi-hop join, ambiguous definition, unanswerable, or trap.
- Each question has reference SQL against the dbt tables that produces its expected answer, plus a tolerance.

### 4. Claude agent

- Anthropic SDK with `claude-opus-5-5`, orchestrated with LangGraph.
- Tools:
  - `list_metrics`
  - `list_dimensions`
  - `query_metrics`: MetricFlow turns the request into SQL, which runs read-only on DuckDB.
  - `run_sql`: read-only raw SQL, used as the baseline.
- Three modes: semantic only, raw SQL only, hybrid.
- Things specific to Opus 5.5:
  - Thinking is always on; control depth with `output_config.effort`, which defaults to `medium`.
  - Forcing a tool call (`tool_choice` of `any` or `tool`) returns an error; use `auto` and steer from the prompt.
  - Pass `response.content` back unchanged on every turn.

### 5. Eval harness

- Run every question in every mode, 3 times each to measure variance.
- Grading:
  - Numeric answers are compared automatically against the expected answer, within its tolerance.
  - An LLM judge is used only for ambiguous or unanswerable questions.
- Record accuracy per category, tool calls, tokens, cost and latency to DuckDB or CSV.
- Classify every failure: wrong metric, wrong filter, wrong time grain, join fan-out, invented column, or didn't ask a clarifying question.

### 6. Experiments

This is where the learning happens.

- Compare accuracy with the semantic layer against raw SQL, and against hybrid.
- Then measure what moves accuracy:
  - how good the metric descriptions are
  - schema size and noise
  - effort level
  - other models (Sonnet, Haiku)
  - ambiguous metric definitions
  - prompt caching (cost and latency)

### 7. Chat UI and tracing

This comes last because the evals don't need a UI.

- A FastAPI endpoint with the OpenAI-style API that OpenWebUI expects.
- LiteLLM as the model gateway. First check that it passes through `thinking` and `output_config`; if it doesn't, call Claude directly.
- Self-hosted Langfuse for traces, with each eval run linked to its traces.
- All services added to `compose.yaml`.

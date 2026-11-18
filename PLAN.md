# Plan: semantic layer + evals for chatting with a local database

**Goal:** learn how accurately an AI model answers questions about a local database, and how much a semantic layer helps compared with letting it write raw SQL. No paid API calls: evals run on a free local model, and the app can use free online models.

## Decisions

| Area | Choice | Why |
|---|---|---|
| Semantic layer | dbt MetricFlow | Open source (Apache 2.0), basis of the Open Semantic Interchange standard, runs locally. Omni was the first preference but only runs in Omni's cloud. |
| Database | DuckDB | Single file, no server, well supported by dbt and MetricFlow. |
| Data | Synthetic marketing data | We control it, so every eval question has a known correct answer. |
| AI | LiteLLM, default `ollama_chat/qwen3:4b` (local, via Ollama on the Mac) | No API key or paid calls. qwen3:8b was dropped: about 7 GB of RAM on a 16 GB Mac. qwen3:4b uses 3.9 GB. Changing model is one setting (`CHAT_MODEL` or `--model`). |
| Runtime | Docker (`compose.yaml`) | Everything runs in containers. |
| Later | LangGraph, OpenWebUI, Langfuse | Same stack as the existing internal tool. |

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
- Step 3 (golden set) is done: 96 questions (two batches of 48 with the same category mix) in `evals/golden.yaml`. `docker compose run --rm app python scripts/check_golden.py` validates the file and prints every expected answer. Ambiguous questions expect the AI to answer with the default definition and state it (revenue = net); questions too vague for a default expect a clarifying question.
- Step 4 (agent) works with qwen3:4b: `chat/tools.py` and `chat/agent.py`, a plain LiteLLM tool loop (LangGraph moved to step 7). Questions take 40–150 s each. Start Ollama with `ollama serve` (or `brew services start ollama`) before running it. `python -m chat.tools` self-checks the tools without a key. MetricFlow only generates the SQL; the agent runs it on a read-only DuckDB connection with external access off.

- Step 5 (eval harness) is built: `evals/grade.py`, `evals/run.py`, `evals/report.py`; design in `evals/README.md`. The first full run is still to do.
- Step 7, first part: `chat/server.py` is an OpenAI-style endpoint (each mode is a "model" in OpenWebUI), and `compose.yaml` has `api` and `webui` services. Not started yet; LangGraph and Langfuse are still to do.

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

### 4. Agent

- LiteLLM with a plain tool loop (LangGraph comes in step 7). Default model `ollama_chat/qwen3:4b`.
- Tools:
  - `list_metrics`
  - `list_dimensions`
  - `query_metrics`: MetricFlow turns the request into SQL, which runs read-only on DuckDB.
  - `run_sql`: read-only raw SQL, used as the baseline.
  - `ask_clarification`: for questions too vague to answer; ends the turn.
- Three modes: semantic only, raw SQL only, hybrid.
- If a Claude model is used later (needs an API key), things specific to Opus 5.5:
  - Thinking is always on; control depth with `output_config.effort`, which defaults to `medium`.
  - Forcing a tool call (`tool_choice` of `any` or `tool`) returns an error; use `auto` and steer from the prompt.
  - Pass `response.content` back unchanged on every turn.

### 5. Eval harness

Design, grading rules, failure classes and levers: `evals/README.md`.

- Run every question in every mode, 3 times each to measure variance. Temperature 0.3.
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
  - other models: free online models, and qwen3:4b against smaller local models
  - ambiguous metric definitions
  - prompt caching (cost and latency)

### 7. Chat UI and tracing

This comes last because the evals don't need a UI.

- Wrap the step 4 agent loop in a LangGraph graph (saved chat state, clarification as a pause for the user).
- A FastAPI endpoint with the OpenAI-style API that OpenWebUI expects.
- Self-hosted Langfuse for traces, with each eval run linked to its traces.
- All services added to `compose.yaml`.

### 8. Model choice in the app

- Users pick the model from 3–4 common options, e.g. local qwen3:4b plus free online models (OpenRouter, Gemini, Groq or Cerebras). The agent already takes any LiteLLM model name, so this is a dropdown plus a fixed list of allowed names.
- Before the app is public: a per-user rate limit (free tiers have small daily caps) and a SQL query timeout.

"""Agent tools: MetricFlow semantic layer + read-only SQL on the DuckDB warehouse.

Self-check (no API key needed): python -m chat.tools
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path

import duckdb
from metricflow.engine.metricflow_engine import MetricFlowEngine, MetricFlowQueryRequest
from metricflow.protocols.sql_client import SqlEngine
from metricflow.sql.render.duckdb_renderer import DuckDbSqlPlanRenderer
from metricflow_semantics.model.dbt_manifest_parser import parse_manifest_from_dbt_generated_manifest
from metricflow_semantics.model.semantic_manifest_lookup import SemanticManifestLookup

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = os.environ.get("DUCKDB_PATH", str(ROOT / "data/warehouse.duckdb"))
MANIFEST = ROOT / "dbt/target/semantic_manifest.json"  # written by `dbt build`
MAX_ROWS = 200


class _DuckDbDialect:
    """MetricFlow only needs the dialect to generate SQL; we execute it ourselves, read-only."""

    sql_engine_type = SqlEngine.DUCKDB
    sql_plan_renderer = DuckDbSqlPlanRenderer()


@functools.cache
def _mf() -> MetricFlowEngine:
    manifest = parse_manifest_from_dbt_generated_manifest(MANIFEST.read_text())
    return MetricFlowEngine(SemanticManifestLookup(manifest), _DuckDbDialect())


def _run(sql: str) -> str:
    # read-only + no external access: no writes, no file reads, no ATTACH
    # shortcut: no query timeout, a runaway cross join hangs the call; add duckdb interrupt() if it happens
    con = duckdb.connect(DB_PATH, read_only=True, config={"enable_external_access": False})
    try:
        cur = con.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS + 1)
    finally:
        con.close()
    out = {"columns": cols, "rows": rows[:MAX_ROWS]}
    if len(rows) > MAX_ROWS:
        out["truncated"] = f"only the first {MAX_ROWS} rows are shown"
    return json.dumps(out, default=str)


def list_metrics() -> str:
    return json.dumps([
        {"name": m.name, "label": m.label, "type": m.type.value, "description": m.description}
        for m in _mf().list_metrics(include_dimensions=False)
    ])


def list_dimensions(metrics: list[str]) -> str:
    dims = [{"name": d.dunder_name, "description": d.description} for d in _mf().list_dimensions(metric_names=metrics)]
    return json.dumps(dims)


def query_metrics(metrics: list[str], group_by: list[str] | None = None, where: list[str] | None = None,
                  order_by: list[str] | None = None, limit: int | None = None) -> str:
    request = MetricFlowQueryRequest.create(
        metric_names=metrics, group_by_names=group_by or [], where_constraints=where or [],
        order_by_names=order_by or [], limit=limit,
    )
    return _run(_mf().explain(request).sql_statement.sql)


def run_sql(sql: str) -> str:
    return _run(sql)


def ask_clarification(question: str) -> str:
    return question  # handled by the agent loop: ends the turn and shows the question to the user


SCHEMAS = {
    "list_metrics": {
        "description": "List the metrics defined in the semantic layer, with their definitions.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    "list_dimensions": {
        "description": (
            "List the dimensions you can group or filter the given metrics by. "
            "metric_time is the time dimension for every metric; add a grain suffix to group by it: "
            "metric_time__day, metric_time__week, metric_time__month, metric_time__quarter, metric_time__year."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"metrics": {"type": "array", "items": {"type": "string"}}},
            "required": ["metrics"],
            "additionalProperties": False,
        },
    },
    "query_metrics": {
        "description": (
            "Query metrics through the semantic layer. It writes the SQL and joins for you. "
            "group_by and order_by take dimension names from list_dimensions (prefix order_by with '-' for descending). "
            "where takes MetricFlow filters, e.g. \"{{ Dimension('channel__channel_name') }} = 'meta_ads'\" or "
            "\"{{ TimeDimension('metric_time', 'day') }} >= '2026-03-01'\"; several filters are ANDed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "metrics": {"type": "array", "items": {"type": "string"}},
                "group_by": {"type": "array", "items": {"type": "string"}},
                "where": {"type": "array", "items": {"type": "string"}},
                "order_by": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
            "required": ["metrics"],
            "additionalProperties": False,
        },
    },
    "run_sql": {
        "description": (
            "Run one read-only DuckDB SQL query against the warehouse and get up to "
            f"{MAX_ROWS} rows back. Use information_schema to explore tables and columns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    "ask_clarification": {
        "description": (
            "Ask the user a clarifying question instead of answering, when the question is too vague to "
            "choose a metric or period and no common default meaning exists. Ends your turn."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
            "additionalProperties": False,
        },
    },
}
FUNCTIONS = {f.__name__: f for f in (list_metrics, list_dimensions, query_metrics, run_sql, ask_clarification)}


def tool_defs(names: list[str]) -> list[dict]:
    return [{"name": n, **SCHEMAS[n], "strict": True} for n in names]


if __name__ == "__main__":
    names = {m["name"] for m in json.loads(list_metrics())}
    assert {"net_revenue", "gross_revenue", "roas", "conversion_rate"} <= names, names
    assert "metric_time__day" in list_dimensions(["roas"])
    rows = json.loads(query_metrics(["net_revenue"]))["rows"]
    total_sql = json.loads(run_sql("select sum(net_revenue_eur) from marts.fct_orders"))["rows"][0][0]
    assert abs(rows[0][0] - total_sql) < 1e-6, (rows, total_sql)
    for bad in ["create table marts.x as select 1", "select * from read_csv('/etc/passwd')"]:
        try:
            run_sql(bad)
            raise AssertionError(f"not blocked: {bad}")
        except duckdb.Error:
            pass
    print("tools ok:", len(names), "metrics; net revenue", round(total_sql, 2))

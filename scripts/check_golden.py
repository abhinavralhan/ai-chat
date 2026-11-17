"""Validate evals/golden.yaml and print the expected answer of every question.

Usage: python scripts/check_golden.py
"""

from collections import Counter
from pathlib import Path

import duckdb
import yaml

ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {
    "simple_lookup", "time_grain", "filter", "ratio",
    "multi_hop_join", "ambiguous_definition", "unanswerable", "trap",
}
ANSWER_TYPES = {"number", "text", "table", "none"}
BEHAVIORS = {"answer", "state_assumption", "refuse", "clarify"}


def expected(con, q: dict):
    """Run a question's reference SQL; scalar for number/text, rows for table."""
    rows = con.execute(q["reference_sql"]).fetchall()
    if q["answer_type"] in ("number", "text"):
        assert len(rows) == 1 and len(rows[0]) == 1, f"{q['id']}: expected one value, got {rows}"
        assert rows[0][0] is not None, f"{q['id']}: answer is NULL"
        return rows[0][0]
    assert rows, f"{q['id']}: no rows"
    return rows


def main() -> None:
    questions = yaml.safe_load((ROOT / "evals/golden.yaml").read_text())["questions"]
    ids = [q["id"] for q in questions]
    assert len(ids) == len(set(ids)), "duplicate ids"

    con = duckdb.connect(str(ROOT / "data/warehouse.duckdb"), read_only=True)
    for q in questions:
        assert q["category"] in CATEGORIES, q["id"]
        assert q["answer_type"] in ANSWER_TYPES, q["id"]
        assert q["expected_behavior"] in BEHAVIORS, q["id"]
        assert ("reference_sql" in q) == (q["answer_type"] != "none"), q["id"]
        if q["answer_type"] == "none":
            print(f"{q['id']:<16} ({q['expected_behavior']}) {q['question']}")
            continue
        ans = expected(con, q)
        shown = f"{ans:,.4f}" if isinstance(ans, float) else ans
        print(f"{q['id']:<16} {q['question']}\n{'':<17}-> {shown}")

    print(f"\n{len(questions)} questions:", dict(Counter(q["category"] for q in questions)))


if __name__ == "__main__":
    main()

"""Grade saved runs and print the report. Fast: no model calls except the refusal judge, whose verdicts are cached.

Usage: python -m evals.report evals/runs/smoke.jsonl [more run files]
Writes <run>.graded.csv next to the first run file.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

import duckdb
import yaml

from chat.agent import MODEL, NUM_CTX, TEMPERATURE
from evals.grade import grade, llm_judge
from scripts.check_golden import expected

ROOT = Path(__file__).resolve().parents[1]
JUDGE_CACHE = ROOT / "evals/runs/judge_cache.json"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% confidence interval for a pass rate."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    centre, half = p + z * z / (2 * n), z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - half) / (1 + z * z / n), (centre + half) / (1 + z * z / n)


def sign_test(a: int, b: int) -> float:
    """Two-sided p-value that a and b wins out of a+b split this unevenly by chance."""
    n = a + b
    return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(min(a, b) + 1)) / 2**n)


def cached_judge(model: str):
    cache = json.loads(JUDGE_CACHE.read_text()) if JUDGE_CACHE.exists() else {}
    judge = llm_judge(model, TEMPERATURE)

    def run(question: str, answer: str) -> bool:
        key = hashlib.sha256(f"{model}\n{question}\n{answer}".encode()).hexdigest()
        if key not in cache:
            cache[key] = judge(question, answer)
            JUDGE_CACHE.write_text(json.dumps(cache, indent=0))
        return cache[key]

    return run


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("runs", nargs="+", type=Path)
    p.add_argument("--judge-model", default=MODEL)
    args = p.parse_args()

    golden = {q["id"]: q for q in yaml.safe_load((ROOT / "evals/golden.yaml").read_text())["questions"]}
    con = duckdb.connect(str(ROOT / "data/warehouse.duckdb"), read_only=True)
    truth = {i: expected(con, q) if q["answer_type"] != "none" else None for i, q in golden.items()}
    results = [json.loads(line) for path in args.runs for line in path.read_text().splitlines()]
    judge = cached_judge(args.judge_model)

    # label each result by whatever differs between them: mode, and model or temperature when mixed
    varies = [k for k in ("model", "temperature") if len({r[k] for r in results}) > 1]
    for r in results:
        r["config"] = " ".join([str(r[k]).split("/")[-1] for k in varies] + [r["mode"]])
        r["passed"], r["reason"] = grade(golden[r["id"]], r, truth[r["id"]], judge)
        r["category"] = golden[r["id"]]["category"]
    if len({r["commit"] for r in results}) > 1:
        print("warning: these answers come from different git commits\n")
    configs = sorted({r["config"] for r in results})

    print("ACCURACY (95% interval)")
    for c in configs:
        rs = [r for r in results if r["config"] == c]
        k, n = sum(r["passed"] for r in rs), len(rs)
        lo, hi = wilson(k, n)
        lat = sum(r["latency_s"] for r in rs) / n
        tok = sum(r["usage"].get("input_tokens", 0) + r["usage"].get("output_tokens", 0) for r in rs) / n
        print(f"  {c:<24} {k:>3}/{n:<3} {k / n:6.1%}  ({lo:.0%}-{hi:.0%})  avg {lat:.0f}s, {tok:,.0f} tokens")

    print("\nBY CATEGORY")
    cats = sorted({r["category"] for r in results})
    print(f"  {'':<22}" + "".join(f"{c[:16]:>18}" for c in configs))
    for cat in cats:
        cells = []
        for c in configs:
            rs = [r for r in results if r["config"] == c and r["category"] == cat]
            cells.append(f"{sum(r['passed'] for r in rs)}/{len(rs)}" if rs else "-")
        print(f"  {cat:<22}" + "".join(f"{x:>18}" for x in cells))

    print("\nPAIRED (same question and repeat; only answers where exactly one side passed count)")
    by_key = {(r["config"], r["id"], r["repeat"]): r["passed"] for r in results}
    for i, a in enumerate(configs):
        for b in configs[i + 1:]:
            pairs = [(by_key[(a, q, rep)], by_key[(b, q, rep)]) for (c, q, rep) in by_key
                     if c == a and (b, q, rep) in by_key]
            a_wins, b_wins = sum(x and not y for x, y in pairs), sum(y and not x for x, y in pairs)
            pv = sign_test(a_wins, b_wins)
            verdict = "real difference" if pv < 0.05 else "could be chance"
            print(f"  {a} vs {b}: {len(pairs)} pairs, {a} only {a_wins}, {b} only {b_wins}, p={pv:.2f} ({verdict})")

    runs_per = defaultdict(list)
    for r in results:
        runs_per[(r["config"], r["id"])].append(r["passed"])
    flaky = {k: v for k, v in runs_per.items() if len(v) > 1 and 0 < sum(v) < len(v)}
    if flaky:
        print("\nFLAKY (passed on some repeats only)")
        for (c, q), v in sorted(flaky.items()):
            print(f"  {c:<24} {q:<16} {sum(v)}/{len(v)}")

    near = [r for r in results if r["usage"].get("max_prompt_tokens", 0) > 0.9 * NUM_CTX]
    if near:
        print(f"\nCONTEXT: {len(near)} answers got within 10% of the {NUM_CTX}-token window "
              "(Ollama drops the oldest messages silently)")
        for r in near:
            print(f"  {r['config']:<24} {r['id']:<16} {r['usage']['max_prompt_tokens']:,} tokens")

    print("\nFAILURES")
    for r in sorted((r for r in results if not r["passed"]), key=lambda r: (r["id"], r["config"], r["repeat"])):
        said = (r.get("clarification") or r.get("answer") or "").replace("\n", " ")[:110]
        print(f"  {r['id']:<16} {r['config']:<12} #{r['repeat']}  {r['reason']}\n      said: {said}")

    out = args.runs[0].with_suffix(".graded.csv")
    fields = ["id", "category", "config", "model", "mode", "temperature", "repeat", "passed", "reason",
              "stop_reason", "turns", "latency_s", "commit"]
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(fields + ["tool_calls", "tool_errors", "input_tokens", "output_tokens", "max_prompt_tokens"])
        for r in results:
            u = r["usage"]
            w.writerow([r[k] for k in fields] + [len(r["tool_calls"]), sum(c.get("error", False) for c in r["tool_calls"]),
                        u.get("input_tokens"), u.get("output_tokens"), u.get("max_prompt_tokens")])
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

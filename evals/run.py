"""Ask the agent every golden question and save each answer as one line in evals/runs/<run_id>.jsonl.

Re-running the same command resumes: answers already in the file are skipped.
Usage: python -m evals.run smoke --ids simple_01 trap_05 --modes semantic sql
       python -m evals.run full_1 --repeats 3
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from chat import tracing
from chat.agent import MODEL, MODES, TEMPERATURE, ask

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "evals/runs"


def git_commit() -> str:
    """Current commit from .git directly; the container has no git binary."""
    head = (ROOT / ".git/HEAD").read_text().strip()
    if not head.startswith("ref: "):
        return head
    ref = head[5:]
    loose = ROOT / ".git" / ref
    if loose.exists():
        return loose.read_text().strip()
    packed = (ROOT / ".git/packed-refs").read_text().splitlines()
    return next(line.split()[0] for line in packed if line.endswith(" " + ref))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("run_id")
    p.add_argument("--modes", nargs="+", choices=MODES, default=list(MODES))
    p.add_argument("--ids", nargs="+", help="only these question ids")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--model", default=MODEL)
    p.add_argument("--temperature", type=float, default=TEMPERATURE)
    args = p.parse_args()

    questions = yaml.safe_load((ROOT / "evals/golden.yaml").read_text())["questions"]
    if args.ids:
        unknown = set(args.ids) - {q["id"] for q in questions}
        assert not unknown, f"unknown ids: {unknown}"
        questions = [q for q in questions if q["id"] in args.ids]

    RUNS.mkdir(exist_ok=True)
    path = RUNS / f"{args.run_id}.jsonl"
    done = set()
    if path.exists():
        for line in path.read_text().splitlines():
            r = json.loads(line)
            done.add((r["id"], r["mode"], r["repeat"]))

    todo = [(rep, q, m) for rep in range(1, args.repeats + 1) for q in questions for m in args.modes
            if (q["id"], m, rep) not in done]  # modes inner, so an interrupted run still has complete pairs
    print(f"{path.name}: {len(done)} saved, {len(todo)} to go")
    commit, errors = git_commit(), 0
    with path.open("a") as f:
        for i, (rep, q, mode) in enumerate(todo, 1):
            try:
                with tracing.session(args.run_id, mode, q["id"]):  # Langfuse session = eval run
                    result = ask(q["question"], mode, model=args.model, temperature=args.temperature)
            except Exception as e:  # model server down or similar: don't save, so a rerun retries it
                errors += 1
                print(f"[{i}/{len(todo)}] {q['id']} {mode} ERROR {type(e).__name__}: {str(e)[:200]}")
                if errors >= 3:
                    raise SystemExit("3 errors in a row, stopping. Is the model server running?")
                continue
            errors = 0
            f.write(json.dumps({"id": q["id"], "repeat": rep, "run_id": args.run_id, "commit": commit,
                                **result}, default=str) + "\n")
            f.flush()
            print(f"[{i}/{len(todo)}] {q['id']} {mode} #{rep}: {result['stop_reason']}, "
                  f"{len(result['tool_calls'])} calls, {result['latency_s']}s")


if __name__ == "__main__":
    main()

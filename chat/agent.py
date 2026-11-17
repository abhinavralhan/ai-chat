"""Claude agent that answers questions about the marketing warehouse.

Modes: semantic (MetricFlow only), sql (raw SQL only), hybrid (both).
Usage: python -m chat.agent "What was revenue in March?" --mode semantic
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter

import anthropic

from chat.tools import FUNCTIONS, tool_defs

MODEL = "claude-opus-5-5"
MAX_TURNS = 15

BASE_PROMPT = """You answer questions about the marketing data of an online running-shoe shop (January to June 2026). Use the tools to get every number; never guess or estimate one.

- If the question has a common default meaning, answer with that meaning and say in one sentence which definition you used.
- If it is too vague to choose a metric or period and there is no common default, call ask_clarification instead of answering.
- If the data cannot answer it, say so plainly instead of giving a number.
- Lead your final answer with the number(s), then one short line on the definition or assumptions. Money is in EUR."""

MODES = {
    "semantic": (
        ["list_metrics", "list_dimensions", "query_metrics", "ask_clarification"],
        "Use the semantic layer: start with list_metrics, check dimensions with list_dimensions, then query_metrics.",
    ),
    "sql": (
        ["run_sql", "ask_clarification"],
        "Query the DuckDB warehouse with run_sql. The tables are in the raw and marts schemas; explore them with information_schema first.",
    ),
    "hybrid": (
        ["list_metrics", "list_dimensions", "query_metrics", "run_sql", "ask_clarification"],
        "Prefer the semantic layer (list_metrics, list_dimensions, query_metrics). Use run_sql only when no metric or dimension fits.",
    ),
}

client = anthropic.Anthropic()


def ask(question: str, mode: str = "semantic", effort: str = "medium", model: str = MODEL) -> dict:
    """Answer one question. Returns the answer plus everything the eval harness records."""
    tool_names, mode_prompt = MODES[mode]
    messages: list[dict] = [{"role": "user", "content": question}]
    calls: list[dict] = []
    usage: Counter = Counter()
    clarification = None
    start = time.monotonic()

    for turn in range(1, MAX_TURNS + 1):
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            system=f"{BASE_PROMPT}\n\n{mode_prompt}",
            tools=tool_defs(tool_names),
            messages=messages,
            output_config={"effort": effort},  # Opus 5.5: thinking is always on, effort sets its depth
            cache_control={"type": "ephemeral"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # on a refusal the API retries on a fallback model; response.model says who answered
        )
        for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            usage[k] += getattr(response.usage, k) or 0
        messages.append({"role": "assistant", "content": response.content})  # unchanged, thinking blocks included
        stop_reason = response.stop_reason
        if stop_reason != "tool_use":
            break

        results = []
        for block in (b for b in response.content if b.type == "tool_use"):
            call = {"name": block.name, "input": block.input}
            calls.append(call)
            if block.name == "ask_clarification":
                clarification = block.input["question"]
                continue
            try:
                output, call["error"] = FUNCTIONS[block.name](**block.input), False
            except Exception as e:  # tool errors go back to Claude so it can fix its query
                output, call["error"] = f"{type(e).__name__}: {e}"[:2000], True
            results.append({"type": "tool_result", "tool_use_id": block.id, "content": output, "is_error": call["error"]})
        if clarification is not None:
            stop_reason = "clarification"
            break
        messages.append({"role": "user", "content": results})
    else:
        stop_reason = "max_turns"

    return {
        "question": question,
        "mode": mode,
        "effort": effort,
        "model": response.model,
        "answer": "".join(b.text for b in response.content if b.type == "text").strip(),
        "clarification": clarification,
        "stop_reason": stop_reason,
        "turns": turn,
        "tool_calls": calls,
        "usage": dict(usage),
        "latency_s": round(time.monotonic() - start, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question")
    parser.add_argument("--mode", choices=MODES, default="semantic")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="medium")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()

    result = ask(args.question, args.mode, args.effort)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(result["clarification"] or result["answer"])
    print(f"\n[{result['stop_reason']}, {result['turns']} turns, {len(result['tool_calls'])} tool calls, "
          f"{result['latency_s']}s, tokens {result['usage']}]")


if __name__ == "__main__":
    main()

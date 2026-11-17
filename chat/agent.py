"""Agent that answers questions about the marketing warehouse, on any model LiteLLM supports.

Modes: semantic (MetricFlow only), sql (raw SQL only), hybrid (both).
Model: --model or CHAT_MODEL, e.g. ollama_chat/qwen3:4b, openrouter/<model>:free, anthropic/claude-opus-5-5
Usage: python -m chat.agent "What was revenue in March?" --mode semantic
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter

import litellm

from chat.tools import FUNCTIONS, tool_defs

MODEL = os.environ.get("CHAT_MODEL", "ollama_chat/qwen3:4b")
MAX_TURNS = 15
TEMPERATURE = 0.3  # low for stable evals; Qwen loops at 0 in thinking mode
NUM_CTX = 8192

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


def _model_args(model: str, effort: str, temperature: float) -> dict:
    if model.startswith("ollama"):
        return {"num_ctx": NUM_CTX, "temperature": temperature}  # Ollama's default context is small and truncates silently
    if model.startswith("anthropic/"):
        return {"reasoning_effort": effort}  # Claude thinking models don't take a temperature
    return {"temperature": temperature}


def ask(question: str, mode: str = "semantic", effort: str = "medium", model: str = MODEL,
        temperature: float = TEMPERATURE) -> dict:
    """Answer one question. Returns the answer plus everything the eval harness records."""
    tool_names, mode_prompt = MODES[mode]
    tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                               "parameters": t["input_schema"]}} for t in tool_defs(tool_names)]
    messages: list[dict] = [
        {"role": "system", "content": f"{BASE_PROMPT}\n\n{mode_prompt}"},
        {"role": "user", "content": question},
    ]
    calls: list[dict] = []
    usage: Counter = Counter()
    cost = 0.0
    clarification = None
    start = time.monotonic()

    for turn in range(1, MAX_TURNS + 1):
        response = litellm.completion(model=model, messages=messages, tools=tools,
                                    **_model_args(model, effort, temperature))
        usage["input_tokens"] += response.usage.prompt_tokens or 0
        usage["output_tokens"] += response.usage.completion_tokens or 0
        usage["max_prompt_tokens"] = max(usage["max_prompt_tokens"], response.usage.prompt_tokens or 0)
        try:
            cost += litellm.completion_cost(response)
        except Exception:  # free/local models have no price entry
            pass
        msg = response.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))  # unchanged, so reasoning blocks survive
        stop_reason = response.choices[0].finish_reason
        if not msg.tool_calls:
            break

        for tc in msg.tool_calls:
            call = {"name": tc.function.name, "input": tc.function.arguments}
            calls.append(call)
            try:
                args = json.loads(tc.function.arguments or "{}")
                call["input"] = args
                if tc.function.name == "ask_clarification":
                    clarification = args["question"]
                    output, call["error"] = "", False
                else:
                    output, call["error"] = FUNCTIONS[tc.function.name](**args), False
            except Exception as e:  # bad arguments or a failing query go back to the model to fix
                output, call["error"] = f"{type(e).__name__}: {e}"[:2000], True
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": output})
        if clarification is not None:
            stop_reason = "clarification"
            break
    else:
        stop_reason = "max_turns"

    return {
        "question": question,
        "mode": mode,
        "model": model,
        "temperature": temperature,
        "answer": (msg.content or "").strip(),
        "clarification": clarification,
        "stop_reason": stop_reason,
        "turns": turn,
        "tool_calls": calls,
        "usage": dict(usage),
        "cost_usd": round(cost, 6),
        "latency_s": round(time.monotonic() - start, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("question")
    parser.add_argument("--mode", choices=MODES, default="semantic")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default="medium",
                        help="only used by Claude models")
    parser.add_argument("--json", action="store_true", help="print the full result as JSON")
    args = parser.parse_args()

    result = ask(args.question, args.mode, args.effort, args.model)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return
    print(result["clarification"] or result["answer"])
    print(f"\n[{result['model']}, {result['stop_reason']}, {result['turns']} turns, "
          f"{len(result['tool_calls'])} tool calls, {result['latency_s']}s, tokens {result['usage']}]")


if __name__ == "__main__":
    main()

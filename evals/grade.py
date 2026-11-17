"""Grade one saved agent answer against a golden question. Pure code, except the refusal judge.

Self-check (no model needed): python -m evals.grade
"""

from __future__ import annotations

import datetime as dt
import re

DEFAULT_TOLERANCE = 0.01
MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_NUM = re.compile(r"(?<![\w.])(-?\d[\d,]*(?:\.\d+)?)\s*(%|k\b|m\b|million\b|thousand\b)?", re.I)
_NOT_AMOUNTS = [  # dates and period names, removed before numbers are read
    re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?(?:[ t]\d{2}:\d{2}(?::\d{2})?)?\b", re.I),
    re.compile(r"\b[qh][1-4]\b", re.I),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:{MONTHS})\b|\b(?:{MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b(?!,\d)", re.I),
    re.compile(r"\b202[5-7]\b"),  # shortcut: drops a count that is exactly 2025-2027; no golden answer is
]
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}


def numbers(text: str) -> list[tuple[float, float]]:
    """Every amount in the text as (value, half of its last written digit), e.g. '1.0%' -> (0.01, 0.0005)."""
    for pattern in _NOT_AMOUNTS:
        text = pattern.sub(" ", text)
    out = []
    for raw, unit in _NUM.findall(text):
        digits = raw.replace(",", "")
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        scale = 0.01 if unit == "%" else _SCALE.get(unit.lower(), 1) if unit else 1
        out.append((float(digits) * scale, 0.5 * 10**-decimals * scale))
    return out


def number_matches(expected: float, text: str, tolerance: float | None) -> bool:
    """Any amount in the text equals expected within the relative tolerance, or within the rounding it was written with.

    Exact match is required when the tolerance is 0 or the expected value is 0.
    """
    tol = DEFAULT_TOLERANCE if tolerance is None else tolerance
    for value, rounding in numbers(text):
        if tol == 0 or expected == 0:
            if value == expected:
                return True
        elif abs(value - expected) <= max(tol * abs(expected), rounding):
            return True
    return False


def _norm(s: str) -> str:
    return re.sub(r"[\s_]+", " ", s.lower()).strip()


def text_matches(expected: str, text: str) -> bool:
    """Case- and underscore-insensitive substring; ISO dates also match when written out ('16 March 2026')."""
    hay = _norm(text)
    options = {_norm(expected)}
    try:
        d = dt.date.fromisoformat(expected)
        m = d.strftime("%B").lower()
        options |= {f"{d.day} {m} {d.year}", f"{m} {d.day}, {d.year}", f"{m} {d.day} {d.year}",
                    f"{d.day:02d}.{d.month:02d}.{d.year}", f"{d.day}.{d.month}.{d.year}"}
    except ValueError:
        pass
    return any(o in hay for o in options)


def table_matches(expected_rows: list, text: str, tolerance: float | None) -> tuple[bool, str]:
    """Every number in every expected row appears in the text. Row labels are not checked."""
    missing = [v for row in expected_rows for v in row[1:]
               if isinstance(v, (int, float)) and not number_matches(v, text, tolerance)]
    return (not missing, f"missing {len(missing)} of the expected values, e.g. {missing[0]:,.2f}" if missing else "")


def grade(q: dict, result: dict, expected, judge=None) -> tuple[bool, str]:
    """Return (passed, reason). `expected` comes from the question's reference SQL; judge(question, answer) -> bool."""
    behavior, answer = q["expected_behavior"], result.get("answer") or ""
    clarified = result.get("clarification") is not None

    if behavior == "clarify":
        return (clarified, "asked a clarifying question" if clarified else "answered instead of asking")
    if clarified:
        return False, "asked a clarifying question instead of answering"
    if result.get("stop_reason") == "max_turns":
        return False, "hit the turn limit"
    if not answer:
        return False, "empty answer"
    if behavior == "refuse":
        if judge is None:
            return False, "needs the judge"
        ok = judge(q["question"], answer)
        return ok, "declined" if ok else "gave an answer the data can't support"

    kind, tol = q["answer_type"], q.get("tolerance")
    if kind == "number":
        ok, why = number_matches(expected, answer, tol), f"expected {expected:,.4f}"
    elif kind == "text":
        ok, why = text_matches(str(expected), answer), f"expected '{expected}'"
    else:
        ok, why = table_matches(expected, answer, tol)
    if not ok:
        return False, why
    if behavior == "state_assumption" and not any(w.lower() in answer.lower() for w in q["must_mention"]):
        return False, f"right value but didn't state the definition (none of {q['must_mention']})"
    return True, "correct"


JUDGE_PROMPT = """A user asked a data assistant a question that the data cannot answer.

Question: {question}
Assistant's answer: {answer}

Did the assistant clearly say the data can't answer it, without presenting a made-up or substitute number as the answer? Reply with only YES or NO. /no_think"""


def llm_judge(model: str, temperature: float):
    """Refusal judge on the given model. Its verdicts are checked against hand labels before they're trusted."""
    import litellm

    def judge(question: str, answer: str) -> bool:
        r = litellm.completion(model=model, temperature=temperature,
                               messages=[{"role": "user", "content": JUDGE_PROMPT.format(question=question, answer=answer)}])
        reply = re.sub(r"<think>.*?</think>", "", r.choices[0].message.content or "", flags=re.S)
        return reply.strip().upper().startswith("YES")

    return judge


if __name__ == "__main__":
    # numbers: formats, percent, k/M, dates and years ignored
    assert number_matches(525424.71, "Revenue was €525,424.71 (net, completed orders).", None)
    assert number_matches(525424.71, "About €525k in net revenue.", None)
    assert number_matches(525424.71, "About €0.53M", None)
    assert not number_matches(525424.71, "Gross revenue was €596,901.", None)
    assert number_matches(0.0102, "TikTok converts at 1.02%.", None)
    assert number_matches(0.0102, "TikTok converts at 1.0%.", None)  # within its written rounding
    assert number_matches(0.0102, "The conversion rate is 0.0102.", None)
    assert not number_matches(0.0142, "about 1.0%", None)
    assert number_matches(130000, "In March 2026, revenue was €130k.", None)
    assert not number_matches(2026, "In March 2026 there were no refunds.", None)
    assert not number_matches(16, "The week of 2026-03-16 was busiest.", None)
    assert not number_matches(30, "Spend on 30 June was €1,200.", None)
    assert number_matches(2.3983, "ROAS is 2.40x overall.", None)
    # exact questions: no rounding allowance
    assert number_matches(1279, "1,279 orders were placed in June 2026.", 0)
    assert not number_matches(1279, "about 1.3k orders", 0)
    assert not number_matches(1279, "1,280 orders", 0)
    assert number_matches(0, "We spent €0 on newsletter ads.", None)
    assert not number_matches(0, "We spent €0.50 on newsletter ads.", None)

    # text
    assert text_matches("gs_brand_always_on", "The campaign GS_Brand_Always_On brought the most.")
    assert text_matches("google_search", "Google Search acquired the most customers.")
    assert text_matches("2026-03-16", "The week starting 16 March 2026 had the most sessions.")
    assert text_matches("2026-03-16", "Week of March 16, 2026.")
    assert not text_matches("2026-03-16", "The week starting 9 March 2026.")
    assert text_matches("Tuesday", "Tuesdays are busiest on average.")

    # table
    rows = [("Q1", 3995), ("Q2", 3703), ("Q3", 1)]
    assert table_matches(rows, "Q1: 3,995 orders, Q2: 3,703, Q3: 1 (one order on 1 July).", None)[0]
    assert not table_matches(rows[:2] + [("Q3", 7)], "Q1: 3,995 orders, Q2: 3,703.", None)[0]

    # grade(): behaviours
    num_q = {"question": "x", "expected_behavior": "answer", "answer_type": "number"}
    assert grade(num_q, {"answer": "€525,425", "stop_reason": "stop"}, 525424.71) == (True, "correct")
    assert grade(num_q, {"answer": "", "clarification": "Which period?"}, 525424.71)[0] is False
    assert grade(num_q, {"answer": "", "stop_reason": "max_turns"}, 1.0) == (False, "hit the turn limit")
    sa_q = {**num_q, "expected_behavior": "state_assumption", "must_mention": ["net", "completed"]}
    assert grade(sa_q, {"answer": "€525,425 net revenue (completed orders)."}, 525424.71)[0]
    assert grade(sa_q, {"answer": "Revenue was €525,425."}, 525424.71)[0] is False
    cl_q = {"question": "How are we doing?", "expected_behavior": "clarify", "answer_type": "none"}
    assert grade(cl_q, {"answer": "", "clarification": "Which metric?"}, None)[0]
    assert grade(cl_q, {"answer": "Revenue is €525k."}, None)[0] is False
    rf_q = {"question": "What is our profit margin?", "expected_behavior": "refuse", "answer_type": "none"}
    assert grade(rf_q, {"answer": "No cost data, so I can't."}, None, judge=lambda q, a: True)[0]
    assert grade(rf_q, {"answer": "Margin is 40%."}, None, judge=lambda q, a: False)[0] is False
    print("grader ok")

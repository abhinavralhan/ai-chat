"""LangGraph wrapper around the agent: chat memory lives on our side, keyed by thread id.

agent -> END, or agent -> human -> agent when the agent asks a clarifying question.
The human node pauses the graph (interrupt); the person's next message resumes it. That reply can
answer the question or rewrite the original request; either way the agent re-runs with the full thread.
State is checkpointed to SQLite (CHAT_STATE_DB), so threads survive restarts.

Self-check (no model needed): python -m chat.graph
"""

from __future__ import annotations

import operator
import os
import sqlite3
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from chat import agent

STATE_DB = os.environ.get("CHAT_STATE_DB", "data/chat_state.sqlite")


class State(TypedDict):
    messages: Annotated[list[dict], operator.add]  # user/assistant turns only, as the agent's history takes them
    mode: str
    clarification: str | None


def run_agent(state: State) -> dict:
    *history, last = state["messages"]
    result = agent.ask(last["content"], state["mode"], history=history)
    text = result["clarification"] or result["answer"] or "Sorry, I couldn't find an answer."
    return {"messages": [{"role": "assistant", "content": text}], "clarification": result["clarification"]}


def human(state: State) -> dict:
    reply = interrupt(state["clarification"])  # pauses here until resumed with Command(resume=<reply>)
    return {"messages": [{"role": "user", "content": reply}], "clarification": None}


def build(checkpointer=None):
    g = StateGraph(State)
    g.add_node("agent", run_agent)
    g.add_node("human", human)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", lambda s: "human" if s["clarification"] else END, ["human", END])
    g.add_edge("human", "agent")
    if checkpointer is None:
        os.makedirs(os.path.dirname(STATE_DB) or ".", exist_ok=True)
        checkpointer = SqliteSaver(sqlite3.connect(STATE_DB, check_same_thread=False))  # saver locks per call
    return g.compile(checkpointer=checkpointer)


def send(graph, thread_id: str, text: str, mode: str) -> dict:
    """Send one user message to a thread. Resumes the thread if it is waiting on a clarification.

    Returns {"text": reply to show, "waiting": True if the reply is a clarifying question}.
    """
    config = {"configurable": {"thread_id": thread_id}}
    if graph.get_state(config).next:
        out = graph.invoke(Command(resume=text), config)
    else:
        out = graph.invoke({"messages": [{"role": "user", "content": text}], "mode": mode, "clarification": None}, config)
    if out.get("__interrupt__"):
        return {"text": out["__interrupt__"][0].value, "waiting": True}
    return {"text": out["messages"][-1]["content"], "waiting": False}


if __name__ == "__main__":
    from unittest import mock

    from langgraph.checkpoint.memory import InMemorySaver

    replies = iter([{"answer": "", "clarification": "Which metric?"},
                    {"answer": "€525,425 net revenue.", "clarification": None},
                    {"answer": "€56k ad spend in March.", "clarification": None}])
    with mock.patch.object(agent, "ask", side_effect=lambda *a, **k: next(replies)) as spy:
        g = build(InMemorySaver())
        assert send(g, "t1", "How are we doing?", "sql") == {"text": "Which metric?", "waiting": True}
        assert send(g, "t1", "revenue", "sql") == {"text": "€525,425 net revenue.", "waiting": False}
        assert spy.call_args.args == ("revenue", "sql")
        assert [m["content"] for m in spy.call_args.kwargs["history"]] == ["How are we doing?", "Which metric?"]
        # a new question on the same thread carries the whole conversation from our store
        assert send(g, "t1", "and spend in March?", "semantic")["text"] == "€56k ad spend in March."
        assert spy.call_args.args == ("and spend in March?", "semantic") and len(spy.call_args.kwargs["history"]) == 4
        assert not g.get_state({"configurable": {"thread_id": "t2"}}).values  # threads are separate
    print("graph ok")

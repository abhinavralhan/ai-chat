"""OpenAI-style chat endpoint for OpenWebUI. Each mode shows up as a model in OpenWebUI's dropdown.

Run: uvicorn chat.server:app --host 0.0.0.0 --port 8000
Self-check (no model needed): python -m chat.server
Chat memory is ours (chat/graph.py), keyed by OpenWebUI's chat id header; only the last user message is used.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

from fastapi import FastAPI, Header, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from chat import graph as chat_graph
from chat.agent import MODES
from chat.tracing import session

PREFIX = "marketing-"
app = FastAPI(title="ai-chat")
GRAPH = None  # built on first request so importing the module (self-check, tests) opens no database


def _graph():
    global GRAPH
    if GRAPH is None:
        GRAPH = chat_graph.build()
    return GRAPH


def _text(content) -> str:
    """Message content is a string, or a list of parts when the user attaches files."""
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if p.get("type") == "text")


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [{"id": PREFIX + m, "object": "model", "owned_by": "ai-chat"} for m in MODES]}


@app.post("/v1/chat/completions")
async def chat(body: dict, x_openwebui_chat_id: str | None = Header(None)):
    mode = str(body.get("model", "")).removeprefix(PREFIX)
    if mode not in MODES:
        raise HTTPException(404, f"unknown model {body.get('model')!r}; use one of {[PREFIX + m for m in MODES]}")
    turns = [{"role": m["role"], "content": _text(m.get("content"))}
             for m in body.get("messages", []) if m.get("role") in ("user", "assistant")]
    if not turns or turns[-1]["role"] != "user":
        raise HTTPException(400, "the last message must be from the user")

    # shortcut: without the header, a chat is identified by its first message; two chats opening with the same words share memory
    thread = x_openwebui_chat_id or "first:" + hashlib.sha256(turns[0]["content"].encode()).hexdigest()[:16]
    with session(thread, mode):
        reply = await run_in_threadpool(chat_graph.send, _graph(), thread, turns[-1]["content"], mode)
    text = reply["text"]
    cid, created, model = f"chatcmpl-{uuid.uuid4().hex}", int(time.time()), body["model"]

    if not body.get("stream"):
        return {"id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}

    def events():  # shortcut: the answer arrives as one chunk after the whole tool loop; stream tokens if waits feel long
        for delta, finish in (({"role": "assistant", "content": text}, None), ({}, "stop")):
            chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


if __name__ == "__main__":
    from unittest import mock

    from fastapi.testclient import TestClient

    client = TestClient(app)
    assert [m["id"] for m in client.get("/v1/models").json()["data"]] == [PREFIX + m for m in MODES]
    assert client.post("/v1/chat/completions", json={"model": "gpt-4", "messages": []}).status_code == 404

    fake = {"text": "€525,425 net revenue.", "waiting": False}
    GRAPH = object()
    msgs = [{"role": "system", "content": "ignored"}, {"role": "user", "content": "How are we doing?"},
            {"role": "assistant", "content": "Which metric?"}, {"role": "user", "content": [{"type": "text", "text": "revenue"}]}]
    with mock.patch.object(chat_graph, "send", return_value=fake) as spy:
        r = client.post("/v1/chat/completions", json={"model": PREFIX + "sql", "messages": msgs},
                        headers={"X-OpenWebUI-Chat-Id": "c1"}).json()
        assert r["choices"][0]["message"]["content"] == fake["text"]
        assert spy.call_args.args[1:] == ("c1", "revenue", "sql")
        s = client.post("/v1/chat/completions", json={"model": PREFIX + "sql", "messages": msgs, "stream": True}).text
        assert fake["text"] in s and s.endswith("data: [DONE]\n\n")
        assert spy.call_args.args[1].startswith("first:")  # no header: thread from the first message
    print("server ok")

"""Langfuse tracing, a no-op unless LANGFUSE_PUBLIC_KEY is set (so evals without Langfuse run quietly)."""

from __future__ import annotations

import contextlib
import os

ENABLED = bool(os.environ.get("LANGFUSE_PUBLIC_KEY"))


class _Noop:
    def update(self, **_) -> None:
        pass


def observation(name: str, **kwargs):
    """Context manager for one span/generation/tool; yields an object with .update(...)."""
    if not ENABLED:
        return contextlib.nullcontext(_Noop())
    from langfuse import get_client
    return get_client().start_as_current_observation(name=name, **kwargs)


def session(session_id: str, *tags: str):
    """Group every trace started inside under one Langfuse session (a chat thread or an eval run)."""
    if not ENABLED:
        return contextlib.nullcontext()
    from langfuse import propagate_attributes
    return propagate_attributes(session_id=session_id, tags=list(tags))

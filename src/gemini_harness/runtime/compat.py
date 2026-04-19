"""LangGraph compatibility surface — the ONLY module that imports from langgraph.

Per ADR 0005 and gemini_integration.md §8-§9, all other modules in gemini_harness
must import LangGraph primitives from here (``from ..compat import ...``). If the
upstream LangGraph API drifts, only this file needs to change.

Exposed surface (locked in architect resolution 2026-04-19):
- Graph primitives: ``StateGraph``, ``START``, ``END``
- Control flow: ``Command``, ``Send``
- Checkpointing: ``SqliteSaver`` with ``from_conn_string``
- Reducer helpers: ``add`` (list concat), ``append_unique``, ``merge_inboxes``,
  ``merge_dicts``
- Version comparison: ``Version`` (from ``packaging``)

Excluded from v1 surface: ``interrupt()``, ``RetryPolicy``, streaming primitives.
"""
from __future__ import annotations

from operator import add as _list_add
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send
from packaging.version import Version

try:
    from langgraph.checkpoint.sqlite import SqliteSaver
except ImportError as exc:  # pragma: no cover - surfaces setup errors early
    raise ImportError(
        "langgraph-checkpoint-sqlite is required. "
        "Install with: pip install langgraph-checkpoint-sqlite"
    ) from exc

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
except ImportError:  # pragma: no cover — older langgraph-checkpoint-sqlite
    AsyncSqliteSaver = None  # type: ignore[assignment]


add = _list_add


def append_unique(lhs: list[dict] | None, rhs: list[dict] | None) -> list[dict]:
    """Reducer: append items from rhs whose ``id`` is not already in lhs.

    Idempotent. Used for ``registry`` so replayed Worker writes don't duplicate
    agents. Items without an ``id`` key are compared by identity (always added).
    """
    lhs = list(lhs or [])
    rhs = list(rhs or [])
    seen_ids = {item["id"] for item in lhs if isinstance(item, dict) and "id" in item}
    out = list(lhs)
    for item in rhs:
        if isinstance(item, dict) and "id" in item:
            if item["id"] in seen_ids:
                continue
            seen_ids.add(item["id"])
        out.append(item)
    return out


def merge_inboxes(
    lhs: dict[str, list] | None, rhs: dict[str, list] | None
) -> dict[str, list]:
    """Reducer: merge two inbox dicts by appending per-key lists.

    An rhs value of ``[]`` for a key overwrites that key to empty — this is how
    Worker drains its own inbox after processing.
    """
    lhs = dict(lhs or {})
    rhs = dict(rhs or {})
    out = dict(lhs)
    for key, messages in rhs.items():
        if messages == []:
            out[key] = []
        else:
            out[key] = list(out.get(key, [])) + list(messages)
    return out


def merge_dicts(lhs: dict[str, Any] | None, rhs: dict[str, Any] | None) -> dict[str, Any]:
    """Reducer: shallow merge; rhs wins on key collision."""
    out = dict(lhs or {})
    out.update(rhs or {})
    return out


__all__ = [
    "StateGraph",
    "START",
    "END",
    "Command",
    "Send",
    "SqliteSaver",
    "AsyncSqliteSaver",
    "Version",
    "add",
    "append_unique",
    "merge_inboxes",
    "merge_dicts",
]

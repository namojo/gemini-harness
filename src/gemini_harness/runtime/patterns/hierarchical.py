"""Hierarchical pattern.

Contract (ADR 0003):
- Start with the root agent (``routing_config.root_id``).
- The root may emit ``create_agents`` with a ``group`` tag identifying children.
  Until each newly created child is completed, dispatch the next non-completed
  child.
- When all descendants are completed, re-run root once (final synthesis), then
  END.
- ``max_depth`` defaults to 2 and is enforced by Worker when creating agents
  (not by router) via ``created_by`` chains.
"""
from __future__ import annotations

from ..state import HarnessState, find_agent


def _children(state: HarnessState, parent_id: str) -> list[dict]:
    return [a for a in state.get("registry", []) if a.get("created_by") == parent_id]


def _complete_count(state: HarnessState, agent_id: str) -> int:
    return sum(
        1
        for e in state.get("history") or []
        if e.get("kind") == "worker_complete" and e.get("agent") == agent_id
    )


def route(state: HarnessState) -> str | None:
    routing = (state.get("workflow") or {}).get("routing_config") or {}
    root_id = routing.get("root_id")
    if not root_id:
        return None

    root = find_agent(state.get("registry", []), root_id)
    if root is None:
        return None

    if _complete_count(state, root_id) == 0:
        return root_id

    children = _children(state, root_id)
    for child in children:
        child_id = child.get("id")
        if _complete_count(state, child_id) == 0:
            return child_id

    all_children_done = all(
        _complete_count(state, c.get("id")) > 0 for c in children
    )
    if all_children_done and _complete_count(state, root_id) < 2:
        return root_id

    return None

"""Supervisor pattern.

Contract (ADR 0003):
- Run supervisor first; while ``task_queue`` is empty or has no ready work for
  the current iteration, the supervisor is dispatched so it can populate/refresh
  the queue.
- Once tasks exist, dispatch the first ``pending`` task's ``assigned_to`` agent,
  or (if unassigned) the first idle non-supervisor agent.
- When all tasks are ``completed``, return None (END).

``supervisor_id`` is required in ``routing_config``.
"""
from __future__ import annotations

from ..state import HarnessState


def _completed_agents(state: HarnessState) -> set[str]:
    return {
        e.get("agent")
        for e in state.get("history") or []
        if e.get("kind") == "worker_complete"
    }


def _pick_idle_worker(state: HarnessState, supervisor_id: str) -> str | None:
    completed = _completed_agents(state)
    for a in state.get("registry", []):
        if a.get("id") == supervisor_id:
            continue
        if a.get("id") in completed:
            continue
        return a.get("id")
    return None


def route(state: HarnessState) -> str | None:
    routing = (state.get("workflow") or {}).get("routing_config") or {}
    supervisor_id = routing.get("supervisor_id")
    if not supervisor_id:
        return None

    queue = state.get("task_queue") or []

    if not queue:
        return supervisor_id

    pending = [t for t in queue if t.get("status") == "pending"]
    in_progress = [t for t in queue if t.get("status") == "in_progress"]

    if not pending and not in_progress:
        return None

    for task in pending:
        assignee = task.get("assigned_to")
        if assignee:
            return assignee

    return _pick_idle_worker(state, supervisor_id) or supervisor_id

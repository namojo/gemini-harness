"""Pipeline pattern: scan registry in order, return first non-completed id.

"Completed" is determined by the presence of a ``worker_complete`` history
event for that agent (registry entries' ``status`` field is write-once at
creation because ``append_unique`` is id-keyed).
"""
from __future__ import annotations

from ..state import HarnessState


def _completed_agents(state: HarnessState) -> set[str]:
    out: set[str] = set()
    for event in state.get("history") or []:
        if event.get("kind") == "worker_complete":
            agent = event.get("agent")
            if agent:
                out.add(agent)
    return out


def route(state: HarnessState) -> str | None:
    completed = _completed_agents(state)
    for agent in state.get("registry", []):
        if agent.get("id") not in completed:
            return agent.get("id")
    return None

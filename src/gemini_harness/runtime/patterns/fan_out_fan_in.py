"""Fan-out/Fan-in pattern.

Contract (ADR 0003):
- While workers are not-yet-completed, return ``[Send("worker", sub_state) ...]``
  for each pending worker to run in parallel.
- Once all workers complete, return the ``integrator_id``.
- When integrator completes, return None (END).

``integrator_id`` is required in ``routing_config``.
"""
from __future__ import annotations

from ..compat import Send
from ..state import HarnessState, find_agent


def _completed_agents(state: HarnessState) -> set[str]:
    out: set[str] = set()
    for event in state.get("history") or []:
        if event.get("kind") == "worker_complete":
            agent = event.get("agent")
            if agent:
                out.add(agent)
    return out


def route(state: HarnessState) -> str | None | list[Send]:
    routing = (state.get("workflow") or {}).get("routing_config") or {}
    integrator_id = routing.get("integrator_id")
    registry = state.get("registry", [])

    completed = _completed_agents(state)
    workers = [a for a in registry if a.get("id") != integrator_id]
    integrator = find_agent(registry, integrator_id) if integrator_id else None

    pending_workers = [a for a in workers if a.get("id") not in completed]
    if pending_workers:
        sends: list[Send] = []
        for a in pending_workers:
            sends.append(Send("worker", {**state, "current_target": a["id"]}))
        return sends

    if integrator and integrator_id not in completed:
        return integrator_id

    return None

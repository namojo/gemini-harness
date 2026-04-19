"""Manager node: the sole writer of ``current_target``, ``retry_count``, and
``tool_iterations``.

Manager responsibility (ADR 0001, 0003):
1. Terminate if ``retry_count >= retry_limit`` for producer_reviewer retries,
   or if routing returns None.
2. Handle tool-loop continuation: if ``pending_tool_calls`` is non-empty on
   arrival, goto ``tool_executor``. Increment ``tool_iterations`` before
   dispatching a worker that has just returned from a tool round.
3. Resolve the active sub-pattern (composite via ``phase_map``) and delegate to
   ``patterns.PATTERN_ROUTES``.
4. Return ``Command(goto=..., update={...})``.
"""
from __future__ import annotations

from typing import Any

from .compat import END, Command, Send
from .patterns import PATTERN_ROUTES
from .state import HarnessState


def _active_pattern(state: HarnessState) -> str | None:
    workflow = state.get("workflow") or {}
    pattern = workflow.get("pattern")
    if not isinstance(pattern, str):
        return None
    if "+" not in pattern:
        return pattern
    phase = state.get("phase")
    phase_map = (workflow.get("routing_config") or {}).get("phase_map") or {}
    if phase and phase in phase_map:
        return phase_map[phase]
    sub_patterns = pattern.split("+")
    return sub_patterns[0] if sub_patterns else None


def _max_tool_iterations(state: HarnessState) -> int:
    routing = (state.get("workflow") or {}).get("routing_config") or {}
    tool_cfg = routing.get("tool_executor") or {}
    return int(tool_cfg.get("max_tool_iterations", 5))


def _route_for_pattern(state: HarnessState) -> str | None | list[Send]:
    pattern = _active_pattern(state)
    if pattern is None:
        return None
    routing_fn = PATTERN_ROUTES.get(pattern)
    if routing_fn is None:
        return None
    return routing_fn(state)


def _is_producer_reviewer_retry(state: HarnessState, prev_target: str | None) -> bool:
    routing = (state.get("workflow") or {}).get("routing_config") or {}
    pattern = _active_pattern(state)
    if pattern != "producer_reviewer":
        return False
    producer_id = routing.get("producer_id")
    if not producer_id or prev_target != producer_id:
        return False
    history = state.get("history") or []
    reviewer_id = routing.get("reviewer_id")
    for event in reversed(history):
        if event.get("kind") != "worker_complete":
            continue
        return event.get("agent") == reviewer_id
    return False


def _just_finished_tool_executor(state: HarnessState) -> bool:
    history = state.get("history") or []
    if not history:
        return False
    return history[-1].get("kind") == "tool_executor_complete"


def _stuck_on_create_agents(state: HarnessState, *, threshold: int = 3) -> str | None:
    """Detect consecutive worker_complete events for the same agent that produced
    `create_agent_*` errors and no actual registry additions. Returns the stuck
    agent id, or None. Once threshold is reached the Manager should stop routing
    back and terminate with an escalate error — otherwise the graph loops
    indefinitely retrying the same malformed spec.
    """
    history = state.get("history") or []
    streak = 0
    culprit: str | None = None
    for event in reversed(history):
        if event.get("node") != "worker" or event.get("kind") != "worker_complete":
            continue
        err_count = int(event.get("create_agent_errors", 0) or 0)
        added = int(event.get("agents_added", 0) or 0)
        agent = event.get("agent")
        if err_count > 0 and added == 0 and agent:
            if culprit is None:
                culprit = agent
            if agent == culprit:
                streak += 1
                if streak >= threshold:
                    return culprit
                continue
        break
    return None


def manager_node(state: HarnessState) -> Command:
    if state.get("pending_tool_calls"):
        return Command(goto="tool_executor")

    stuck_agent = _stuck_on_create_agents(state)
    if stuck_agent is not None:
        return Command(
            goto=END,
            update={
                "errors": [
                    {
                        "kind": "create_agent_loop_aborted",
                        "agent": stuck_agent,
                        "detail": (
                            f"{stuck_agent} produced create_agent errors on 3 "
                            "consecutive turns without adding any agents. "
                            "Aborting to prevent an infinite retry loop — check "
                            "the agent's system prompt and linter_spec."
                        ),
                    }
                ]
            },
        )

    tool_iterations = state.get("tool_iterations", 0)
    max_tool_iters = _max_tool_iterations(state)

    if _just_finished_tool_executor(state):
        new_iters = int(tool_iterations) + 1
        if new_iters > max_tool_iters:
            return Command(
                goto=END,
                update={
                    "errors": [
                        {
                            "kind": "tool_iter_exhausted",
                            "iterations": new_iters,
                            "max": max_tool_iters,
                        }
                    ]
                },
            )
        return Command(
            goto="worker",
            update={
                "tool_iterations": new_iters,
                "current_target": state.get("current_target"),
            },
        )

    routed = _route_for_pattern(state)

    if routed is None:
        return Command(goto=END)

    if isinstance(routed, list):
        return Command(goto=routed)

    update: dict[str, Any] = {"current_target": routed}
    if _is_producer_reviewer_retry(state, routed):
        update["retry_count"] = state.get("retry_count", 0) + 1

    return Command(goto="worker", update=update)

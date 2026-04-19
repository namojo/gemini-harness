"""Tool executor node (gemini_integration.md §7a).

Contract:
- Reads ``state.pending_tool_calls``.
- For each call, dispatches via the injected ``ToolExecutor`` callable.
- Respects ``routing_config.tool_executor`` (allowed_tools whitelist, per-call
  timeout, max_tool_iterations — the iteration cap is enforced by Manager).
- Writes results to ``tool_results`` (merge_dicts reducer) and clears
  ``pending_tool_calls``.
- Manager routes back to Worker next.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import ToolExecResult, ToolExecutor
from .state import HarnessState


@dataclass
class ToolExecutorDeps:
    executor: ToolExecutor
    repo_root: str | Path = "."
    now: Callable[[], str] = lambda: datetime.now(UTC).isoformat()


def _allowed(
    tool_name: str, allowed_tools: list[str] | None, agent_tools: list[str] | None
) -> bool:
    pool = list(allowed_tools or []) or list(agent_tools or [])
    if not pool:
        return True  # no whitelist configured → permit
    return tool_name in pool


def _agent_tools(state: HarnessState, agent_id: str | None) -> list[str]:
    if not agent_id:
        return []
    for a in state.get("registry", []):
        if a.get("id") == agent_id:
            return list(a.get("tools", []) or [])
    return []


def make_tool_executor_node(deps: ToolExecutorDeps):
    def tool_executor_node(state: HarnessState) -> dict[str, Any]:
        pending = list(state.get("pending_tool_calls") or [])
        if not pending:
            return {}

        routing = (state.get("workflow") or {}).get("routing_config") or {}
        tool_cfg = routing.get("tool_executor") or {}
        allowed_tools = tool_cfg.get("allowed_tools")
        timeout_s = float(tool_cfg.get("tool_timeout_s", 30))

        results: dict[str, Any] = {}
        errors: list[dict[str, Any]] = []
        run_id = state.get("run_id", "unknown")
        now = deps.now()

        for call in pending:
            call_id = call.get("id")
            name = call.get("name")
            args = dict(call.get("args") or {})
            caller = call.get("caller_agent")
            if not call_id or not name:
                errors.append(
                    {"kind": "tool_call_malformed", "call_id": call_id, "name": name}
                )
                continue
            agent_tools = _agent_tools(state, caller)
            if not _allowed(name, allowed_tools, agent_tools):
                results[call_id] = {
                    "is_error": True,
                    "text": f"tool {name!r} not allowed for agent {caller!r}",
                }
                errors.append(
                    {
                        "kind": "tool_not_allowed",
                        "tool": name,
                        "agent": caller,
                    }
                )
                continue
            try:
                result: ToolExecResult = deps.executor(
                    name,
                    args,
                    timeout_s=timeout_s,
                    node="tool_executor",
                    run_id=run_id,
                )
                results[call_id] = {
                    "is_error": bool(result.is_error),
                    "text": result.text,
                    "structured": result.structured,
                }
            except Exception as exc:  # noqa: BLE001 — surface to state.errors
                results[call_id] = {"is_error": True, "text": str(exc)}
                errors.append(
                    {
                        "kind": "tool_exec_failed",
                        "tool": name,
                        "detail": str(exc),
                    }
                )

        update: dict[str, Any] = {
            "tool_results": results,
            "pending_tool_calls": [],
            "history": [
                {
                    "ts": now,
                    "agent": pending[0].get("caller_agent", ""),
                    "node": "tool_executor",
                    "kind": "tool_executor_complete",
                    "summary": f"executed {len(pending)} tool call(s)",
                }
            ],
        }
        if errors:
            update["errors"] = errors
        return update

    return tool_executor_node


__all__ = ["ToolExecutorDeps", "make_tool_executor_node"]

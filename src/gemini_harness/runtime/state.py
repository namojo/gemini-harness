"""HarnessState TypedDict and supporting types.

Reducers are imported from ``.compat`` so they can be swapped alongside the rest
of the LangGraph surface without touching this file.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal, TypedDict

from .compat import add, append_unique, merge_dicts, merge_inboxes

AgentStatus = Literal["idle", "working", "completed", "failed"]
MessageKind = Literal["info", "request", "result"]


class AgentMetadata(TypedDict, total=False):
    id: str
    name: str
    role: str
    system_prompt_path: str
    skills: list[str]
    tools: list[str]
    group: str
    status: AgentStatus
    created_at: str
    created_by: str
    temperature: float


class Message(TypedDict, total=False):
    from_id: str
    to: str
    content: Any
    kind: MessageKind
    ts: str


class ToolCall(TypedDict, total=False):
    id: str
    name: str
    args: dict[str, Any]
    caller_agent: str


class Event(TypedDict, total=False):
    ts: str
    agent: str
    node: str
    kind: str
    summary: str
    detail: dict[str, Any]


class Task(TypedDict, total=False):
    id: str
    description: str
    assigned_to: str | None
    status: Literal["pending", "in_progress", "completed", "failed"]


class HarnessState(TypedDict, total=False):
    # Workflow snapshot (set at init from workflow.json, then read-only).
    workflow: dict[str, Any]
    run_id: str

    # Agent fabric.
    registry: Annotated[list[AgentMetadata], append_unique]
    inbox: Annotated[dict[str, list[Message]], merge_inboxes]
    current_target: str | None
    task_queue: list[Task]

    # Event log & artifacts.
    history: Annotated[list[Event], add]
    artifacts: Annotated[dict[str, str], merge_dicts]

    # Composite-pattern phase selector.
    phase: str | None

    # Producer/Reviewer + generic retry bookkeeping (Manager is sole writer).
    retry_count: int
    retry_limit: int
    test_passed: bool

    # Error log (append-only).
    errors: Annotated[list[dict[str, Any]], add]

    # Tool-calling loop state (ADR clarification in gemini_integration.md §7a).
    pending_tool_calls: list[ToolCall]
    tool_results: Annotated[dict[str, Any], merge_dicts]
    tool_iterations: int


def initial_state(workflow: dict[str, Any], run_id: str) -> HarnessState:
    """Build the initial HarnessState from a validated workflow dict.

    The workflow is assumed to have been validated against ``workflow.v1.json``
    already; this function just shapes the registry snapshot into state.
    """
    initial_registry: list[AgentMetadata] = list(workflow.get("initial_registry", []))
    routing = workflow.get("routing_config", {}) or {}
    retry_limit = routing.get("retry_limit", workflow.get("retry_limit", 3))
    return {
        "workflow": workflow,
        "run_id": run_id,
        "registry": initial_registry,
        "inbox": {},
        "current_target": None,
        "task_queue": [],
        "history": [],
        "artifacts": {},
        "phase": None,
        "retry_count": 0,
        "retry_limit": retry_limit,
        "test_passed": False,
        "errors": [],
        "pending_tool_calls": [],
        "tool_results": {},
        "tool_iterations": 0,
    }


def find_agent(registry: list[AgentMetadata], agent_id: str) -> AgentMetadata | None:
    for a in registry:
        if a.get("id") == agent_id:
            return a
    return None


__all__ = [
    "AgentMetadata",
    "AgentStatus",
    "Event",
    "HarnessState",
    "Message",
    "MessageKind",
    "Task",
    "ToolCall",
    "find_agent",
    "initial_state",
]

"""Build the compiled harness graph.

Topology (build-time fixed per ADR 0001 + gemini_integration.md §7a):

    START → manager → {worker | tool_executor | END}
    worker → manager
    tool_executor → manager

Tool-executor is always wired but only reached when Worker emits
``pending_tool_calls``. Workflows without tool-calling simply never populate
that field.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from ._audit import run_audit
from ._build import BuildError, run_build
from ._evolve import EvolveError, run_evolve
from ._run import RunError, run_harness
from ._verify import run_verify
from .compat import START, SqliteSaver, StateGraph
from .manager import manager_node
from .state import HarnessState, initial_state
from .tool_executor import ToolExecutorDeps, make_tool_executor_node
from .worker import WorkerDeps, make_worker_node


def build_harness_graph(
    *,
    worker_deps: WorkerDeps,
    tool_executor_deps: ToolExecutorDeps | None = None,
    checkpointer: Any = None,
):
    """Compile a harness graph. ``checkpointer`` is optional — callers that
    want durable resume pass a ``SqliteSaver`` context-manager-managed instance.
    """
    graph = StateGraph(HarnessState)
    graph.add_node("manager", manager_node)
    graph.add_node("worker", make_worker_node(worker_deps))
    if tool_executor_deps is not None:
        graph.add_node("tool_executor", make_tool_executor_node(tool_executor_deps))

    graph.add_edge(START, "manager")
    graph.add_edge("worker", "manager")
    if tool_executor_deps is not None:
        graph.add_edge("tool_executor", "manager")

    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()


def open_sqlite_checkpointer(db_path: str | Path):
    """Return the context-manager returned by ``SqliteSaver.from_conn_string``.

    Callers must use it as a context manager:
        with open_sqlite_checkpointer("_workspace/ckpt.db") as cp:
            app = build_harness_graph(..., checkpointer=cp)
            app.invoke(initial_state(wf, run_id), {"configurable": {"thread_id": run_id}})
    """
    return SqliteSaver.from_conn_string(str(db_path))


__all__ = [
    "BuildError",
    "EvolveError",
    "RunError",
    "build_harness_graph",
    "initial_state",
    "open_sqlite_checkpointer",
    "run_audit",
    "run_build",
    "run_evolve",
    "run_harness",
    "run_verify",
]

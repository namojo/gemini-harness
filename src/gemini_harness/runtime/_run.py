"""``run_harness`` — execute a generated workflow.json via the LangGraph graph.

Loads workflow.json from disk, wires real GeminiClient + meta.linter, optionally
wires a ToolExecutor (via MCP adapter or local callable), compiles the StateGraph
with a SqliteSaver checkpointer keyed by run_id, and invokes with an initial
state seeded from user_input.

Returns the final state artifacts plus metrics. Writes `.gemini/context.md`
incrementally via streaming.
"""
from __future__ import annotations


def _resolve_model() -> str:
    from ..config import get_model
    return get_model()


import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._audit import _load_workflow
from .compat import AsyncSqliteSaver, SqliteSaver, StateGraph, START
from .manager import manager_node
from .state import HarnessState, initial_state
from .tool_executor import ToolExecutorDeps, make_tool_executor_node
from .worker import WorkerDeps, make_aworker_node, make_worker_node


def _build_graph_local(worker_deps, tool_executor_deps, checkpointer, *, async_worker: bool = False):
    """Mirror of harness_runtime.build_harness_graph, placed here to avoid
    circular imports between _run and harness_runtime.

    When ``async_worker`` is True, the worker node is an async wrapper that
    offloads the sync Gemini call to a thread — this unlocks true wall-clock
    parallelism for ``Send``-based fan-out when driven by ``astream``/``ainvoke``.
    """
    graph = StateGraph(HarnessState)
    graph.add_node("manager", manager_node)
    worker_fn = make_aworker_node(worker_deps) if async_worker else make_worker_node(worker_deps)
    graph.add_node("worker", worker_fn)
    if tool_executor_deps is not None:
        graph.add_node("tool_executor", make_tool_executor_node(tool_executor_deps))
    graph.add_edge(START, "manager")
    graph.add_edge("worker", "manager")
    if tool_executor_deps is not None:
        graph.add_edge("tool_executor", "manager")
    return graph.compile(checkpointer=checkpointer) if checkpointer else graph.compile()


def _open_sqlite_cm(db_path):
    return SqliteSaver.from_conn_string(str(db_path))


class RunError(RuntimeError):
    """run_harness failed."""


def _load_dotenv(project_path: Path) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = project_path / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _make_gemini_callable(model: str, run_id: str):
    """Wrap GeminiClient.call into the Protocol-shaped ``GeminiClient`` callable."""
    from ..integrations.gemini_client import GeminiClient

    client = GeminiClient()

    def _call(
        prompt,
        *,
        system=None,
        context=(),
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools=None,
        tool_choice: str = "auto",
        model: str = model,  # type: ignore[assignment]
        node: str = "worker",
        run_id: str = run_id,  # type: ignore[assignment]
        timeout_s: float = 60.0,
    ):
        return client.call(
            prompt=prompt,
            system=system,
            context=context,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            node=node,
            run_id=run_id,
            timeout_s=timeout_s,
        )

    return _call


def _make_tool_executor(te_cfg: dict, run_id: str):
    """Build a ToolExecutor callable backed by mcp_adapter + cli_bridge.

    Dispatch rules (per tool name prefix):
      - ``mcp:{server}/{tool}`` — call_mcp_tool(server_cmd, tool, args)
      - ``cli:{skill}`` — invoke_cli_skill(skill, args_as_list)
      - otherwise → ToolExecResult(is_error=True, "unknown tool transport")

    ``te_cfg`` is ``routing_config.tool_executor`` from workflow.json:
      { allowed_tools: [...], tool_timeout_s: 30, max_tool_iterations: 5, mcp_servers: {...} }

    ``mcp_servers`` (optional) maps server short name → command list, e.g.
      {"harness": ["python", "-m", "gemini_harness.mcp_server"]}
    """
    import asyncio

    from ..integrations.cli_bridge import invoke_cli_skill
    from ..integrations.mcp_adapter import McpServerSpec, McpToolResult, call_mcp_tool
    from .contracts import ToolExecResult

    allowed: list[str] = list(te_cfg.get("allowed_tools") or [])
    default_timeout = float(te_cfg.get("tool_timeout_s", 30))
    mcp_servers_cfg: dict[str, list[str]] = dict(te_cfg.get("mcp_servers") or {})
    # Lazily-populated: user-discovered servers merged with explicit config.
    # Config wins on collision so users can override with a custom command.
    _discovered_servers: dict[str, list[str]] | None = None

    from .builtin_tools import builtins_by_name

    def _resolve_mcp_server(name: str) -> list[str] | None:
        nonlocal _discovered_servers
        if name in mcp_servers_cfg:
            return mcp_servers_cfg[name]
        if _discovered_servers is None:
            try:
                from .tool_discovery import discover_mcp_servers
                _discovered_servers = {
                    k: v.command for k, v in discover_mcp_servers(".").items()
                }
            except Exception:
                _discovered_servers = {}
        return _discovered_servers.get(name)

    def _normalize_call_name(raw: str) -> str:
        """Map Gemini-emitted function names back to transport-prefixed form.

        Gemini SDK rejects ':' and '/' in function declarations, so Worker
        exposes ``mcp:github/list_issues`` as ``mcp__github__list_issues``.
        Accept both when dispatching.
        """
        if raw.startswith("mcp__"):
            parts = raw.split("__", 2)
            if len(parts) == 3:
                return f"mcp:{parts[1]}/{parts[2]}"
        return raw

    def _execute(call_name, call_args, *, timeout_s=None, node="tool_executor", run_id=run_id):
        timeout = float(timeout_s if timeout_s is not None else default_timeout)
        call_name = _normalize_call_name(call_name)
        if allowed and call_name not in allowed:
            return ToolExecResult(
                is_error=True,
                text=f"tool not in allowed_tools: {call_name}",
                structured={"call_name": call_name},
            )
        try:
            # Built-in sandboxed helpers (read_file / list_files / glob_files).
            # Accept both "builtin:NAME" and bare NAME when NAME is known.
            builtins = builtins_by_name()
            bare_name = call_name[8:] if call_name.startswith("builtin:") else call_name
            if bare_name in builtins:
                tool = builtins[bare_name]
                try:
                    result_dict = tool.invoke(Path("."), **(call_args or {}))
                except Exception as exc:  # noqa: BLE001
                    return ToolExecResult(
                        is_error=True,
                        text=f"builtin {tool.name} crashed: {type(exc).__name__}: {exc}",
                    )
                is_err = not result_dict.get("ok", True)
                return ToolExecResult(
                    is_error=is_err,
                    text=None if is_err else str(result_dict.get("content") or result_dict.get("entries") or result_dict.get("matches") or ""),
                    structured=result_dict,
                )

            if call_name.startswith("mcp:"):
                rest = call_name[4:]
                if "/" not in rest:
                    return ToolExecResult(
                        is_error=True,
                        text=f"invalid mcp tool name: {call_name} (expected mcp:<server>/<tool>)",
                    )
                server_key, tool = rest.split("/", 1)
                server_cmd = _resolve_mcp_server(server_key)
                if not server_cmd:
                    return ToolExecResult(
                        is_error=True,
                        text=(
                            f"mcp server {server_key!r} not found. Register it in "
                            "~/.gemini/settings.json mcpServers, or pass it explicitly "
                            "via routing_config.tool_executor.mcp_servers."
                        ),
                    )
                spec = McpServerSpec(
                    name=server_key, transport="stdio", command=list(server_cmd)
                )
                try:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    result: McpToolResult = loop.run_until_complete(
                        call_mcp_tool(
                            server=spec,
                            tool=tool,
                            args=call_args or {},
                            timeout=timeout,
                            node=node,
                            run_id=run_id,
                        )
                    )
                    loop.close()
                except Exception as exc:  # noqa: BLE001
                    return ToolExecResult(
                        is_error=True,
                        text=f"mcp call failed: {type(exc).__name__}: {exc}",
                    )
                return ToolExecResult(
                    is_error=result.is_error,
                    text=result.text,
                    structured=result.structured,
                    raw_content=result.raw_content,
                )

            if call_name.startswith("cli:"):
                skill = call_name[4:]
                # call_args dict → CLI arg list; simple flag-style for v1
                args_list: list[str] = []
                for k, v in (call_args or {}).items():
                    args_list.extend([f"--{k}", str(v)])
                try:
                    out = invoke_cli_skill(skill=skill, args=args_list, timeout=int(timeout))
                except Exception as exc:  # noqa: BLE001
                    return ToolExecResult(
                        is_error=True,
                        text=f"cli skill failed: {type(exc).__name__}: {exc}",
                    )
                return ToolExecResult(is_error=False, text=out)

            return ToolExecResult(
                is_error=True,
                text=f"unknown tool transport: {call_name} (expected mcp:* or cli:*)",
            )
        except Exception as exc:  # noqa: BLE001  safety net
            return ToolExecResult(
                is_error=True,
                text=f"tool_executor unexpected error: {type(exc).__name__}: {exc}",
            )

    return _execute


class _ModuleLinterAdapter:
    """Adapt `gemini_harness.meta.linter` module to the MetaLinter Protocol."""

    def __init__(self) -> None:
        from .. import meta

        self._m = meta

    def lint_agent(self, frontmatter, body, agent_meta=None):
        return self._m.lint_agent(frontmatter, body, agent_meta)

    def lint_skill(self, frontmatter, body, entry_path, read_root):
        return self._m.lint_skill(frontmatter, body, entry_path, read_root)

    def lint_workflow(self, workflow):
        return self._m.lint_workflow(workflow)


_PATH_LIKE_RE = re.compile(
    r"(?:[A-Za-z]:)?[\w\-./][\w\-./~]*\.[\w]{1,8}"
)


def _extract_referenced_files(user_input: str, project_root: Path) -> list[tuple[str, str]]:
    """Best-effort: find project-relative file paths mentioned in ``user_input``
    and return ``[(path, truncated_content), ...]`` for those that exist.

    Only files under ``project_root`` and ≤ 64 KiB each are loaded. Result is
    capped at 5 files and 128 KiB aggregate so we don't balloon the first
    inbox message.
    """
    loaded: list[tuple[str, str]] = []
    seen: set[str] = set()
    aggregate = 0
    for m in _PATH_LIKE_RE.finditer(user_input or ""):
        candidate = m.group(0).strip(" .,;:()[]{}\"'`")
        if candidate in seen:
            continue
        seen.add(candidate)
        # Resolve relative to project root; reject paths that escape.
        try:
            target = (project_root / candidate).resolve()
            target.relative_to(project_root)
        except (ValueError, OSError):
            continue
        if not target.is_file():
            continue
        try:
            data = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(data) > 64 * 1024:
            data = data[: 64 * 1024] + "\n\n... [truncated at 64KiB]"
        aggregate += len(data)
        if aggregate > 128 * 1024:
            break
        loaded.append((candidate, data))
        if len(loaded) >= 5:
            break
    return loaded


def _seed_user_input(state: dict, user_input: str, *, project_root: Path | None = None) -> dict:
    """Put ``user_input`` into the entry agent's inbox as the first message.

    If ``project_root`` is provided, also attach any files mentioned in
    ``user_input`` (existing + under the root) as ``artifacts`` in the seed
    state so every agent can see them. This is a pragmatic workaround for
    v0.1.x which does not yet plumb Gemini function-calling — agents can't
    browse the filesystem, but they can read pre-loaded content.
    """
    registry = state.get("registry", [])
    if not registry:
        return state
    entry_id = registry[0]["id"]

    attached: list[tuple[str, str]] = []
    if project_root is not None:
        attached = _extract_referenced_files(user_input, project_root)

    message_parts = [f"User request: {user_input}"]
    if attached:
        message_parts.append("")
        message_parts.append("Referenced files (pre-loaded; read from this inbox, do NOT ask for them again):")
        for path, _ in attached:
            message_parts.append(f"- {path}")
    message = {
        "from_id": "user",
        "content": "\n".join(message_parts),
        "kind": "request",
    }
    inbox = dict(state.get("inbox") or {})
    inbox[entry_id] = list(inbox.get(entry_id, [])) + [message]

    new_state = dict(state)
    new_state["inbox"] = inbox
    if attached:
        # Stash file contents as artifacts so subsequent agents in the graph
        # can also read them (the inbox message only goes to the entry agent).
        artifacts = dict(new_state.get("artifacts") or {})
        for path, content in attached:
            artifacts[f"input/{path}"] = content
        new_state["artifacts"] = artifacts
    return new_state


def _append_context_md(project_path: Path, run_id: str, chunk: dict) -> None:
    ctx = project_path / ".gemini" / "context.md"
    ctx.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ctx.open("a", encoding="utf-8") as f:
        for node_name, update in chunk.items():
            f.write(f"\n## [{ts}] run={run_id} node={node_name}\n")
            # Keep context.md readable — only summarize interesting keys.
            if isinstance(update, dict):
                summary = {
                    k: v
                    for k, v in update.items()
                    if k in ("current_target", "retry_count", "errors", "history")
                }
                if "history" in summary and isinstance(summary["history"], list):
                    summary["history"] = [
                        h.get("kind") if isinstance(h, dict) else str(h)
                        for h in summary["history"][-5:]
                    ]
                if summary:
                    f.write(
                        "```json\n"
                        + json.dumps(summary, ensure_ascii=False, default=str)
                        + "\n```\n"
                    )
                else:
                    f.write("(no tracked fields in update)\n")
            else:
                f.write(f"(update: {type(update).__name__})\n")


def run_harness(
    *,
    project_path: str,
    user_input: str,
    run_id: str | None = None,
    resume: bool = False,
    step_limit: int | None = None,
    gemini_callable: Any | None = None,
    linter: Any | None = None,
    progress_callback: Any | None = None,
) -> dict:
    """Execute the generated harness graph against user_input. Returns summary.

    ``gemini_callable`` and ``linter`` are overridable for tests. Production
    callers leave them None; the real ``GeminiClient`` and ``meta.linter`` are
    wired automatically.

    ``progress_callback`` (optional, sync signature ``(progress, total, message)``)
    is invoked after each streaming chunk. MCP handlers wire it to
    ``session.send_progress_notification`` so Gemini CLI's HUD shows which
    agent is currently active.
    """
    root = Path(project_path).resolve()
    _load_dotenv(root)

    workflow, drift = _load_workflow(root)
    if workflow is None:
        raise RunError(
            f"No workflow.json at {root}. Run harness.build first."
        )
    fatal_drift = [d for d in drift if d["kind"] == "schema_violation"]
    if fatal_drift:
        raise RunError(
            f"workflow.json fails schema lint: {fatal_drift[0]['detail']}"
        )

    run_id = run_id or "run-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model = _resolve_model()

    if gemini_callable is None:
        gemini_callable = _make_gemini_callable(model, run_id)
    if linter is None:
        linter = _ModuleLinterAdapter()
    worker_deps = WorkerDeps(
        gemini=gemini_callable,
        linter=linter,
        repo_root=root,
    )

    rc = workflow.get("routing_config", {}) or {}
    te_cfg = rc.get("tool_executor") or {}
    # Always wire a tool_executor — even when the workflow doesn't explicitly
    # request one — so agents with `file-manager` in their tools list get the
    # built-in sandboxed fallbacks and so ``mcp:<server>/<tool>`` labels can be
    # resolved against the user's Gemini CLI settings. ``allowed_tools`` /
    # ``mcp_servers`` from workflow routing_config still take precedence.
    tool_executor_deps = ToolExecutorDeps(
        executor=_make_tool_executor(te_cfg, run_id),
        repo_root=root,
    )

    checkpoint_path = root / "_workspace" / "checkpoints" / f"{run_id}.db"
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

    start = time.monotonic()
    chunks_seen = 0
    errors: list[str] = []
    final_state: dict = {}

    # Detect fan-out/parallel-capable patterns — use async worker so multiple
    # Send dispatches execute truly concurrently (wall-clock parallel).
    pattern = str(workflow.get("pattern", "") or "")
    needs_parallel = (
        "fan_out_fan_in" in pattern
        or "supervisor" in pattern
        or "+" in pattern  # composite patterns may contain parallel phases
    )
    if needs_parallel and AsyncSqliteSaver is None:
        # Installed langgraph-checkpoint-sqlite lacks async variant — fall back.
        needs_parallel = False

    seed = initial_state(workflow, run_id=run_id)
    seed = _seed_user_input(seed, user_input, project_root=root)

    config = {
        "configurable": {"thread_id": run_id},
        "recursion_limit": step_limit if step_limit is not None else 50,
    }

    def _emit_progress(seen: int, chunk: dict) -> None:
        if progress_callback is None:
            return
        # Build a short label from the chunk: node name + current_target if present.
        node = next(iter(chunk.keys()), "step")
        update = chunk.get(node) or {}
        current = update.get("current_target") if isinstance(update, dict) else None
        msg = f"{node}" + (f" → {current}" if current else "")
        total = float(step_limit) if step_limit else None
        try:
            progress_callback(float(seen), total, msg)
        except Exception:  # best-effort — never block the run on reporting failures
            pass

    if progress_callback is not None:
        try:
            progress_callback(0.0, float(step_limit) if step_limit else None,
                              f"run start (pattern={pattern or '?'})")
        except Exception:
            pass

    if needs_parallel:
        # Async path: AsyncSqliteSaver + astream + async worker.
        import asyncio

        async def _run_async() -> dict:
            seen = 0
            final_values: dict = {}
            errs: list[str] = []
            try:
                async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
                    app = _build_graph_local(
                        worker_deps=worker_deps,
                        tool_executor_deps=tool_executor_deps,
                        checkpointer=checkpointer,
                        async_worker=True,
                    )
                    async for chunk in app.astream(seed, config, stream_mode="updates"):
                        seen += 1
                        _append_context_md(root, run_id, chunk)
                        _emit_progress(seen, chunk)
                        if step_limit is not None and seen >= step_limit:
                            break
                    final_state_snap = await app.aget_state(config)
                    final_values = final_state_snap.values or {}
            except Exception as exc:  # noqa: BLE001
                errs.append(f"{type(exc).__name__}: {exc}")
            return {"seen": seen, "final": final_values, "errors": errs}

        driven = asyncio.run(_run_async())
        chunks_seen = driven["seen"]
        final_state = driven["final"]
        errors.extend(driven["errors"])
    else:
        with _open_sqlite_cm(str(checkpoint_path)) as checkpointer:
            app = _build_graph_local(
                worker_deps=worker_deps,
                tool_executor_deps=tool_executor_deps,
                checkpointer=checkpointer,
                async_worker=False,
            )
            try:
                for chunk in app.stream(seed, config, stream_mode="updates"):
                    chunks_seen += 1
                    _append_context_md(root, run_id, chunk)
                    _emit_progress(chunks_seen, chunk)
                    if step_limit is not None and chunks_seen >= step_limit:
                        break
                final_state = app.get_state(config).values or {}
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

    wall = int((time.monotonic() - start) * 1000)
    final_registry = final_state.get("registry", [])
    history = final_state.get("history") or []

    # Build an agent_timeline field that Gemini CLI can consume via write_todos.
    # Each agent gets a status: completed (at least one worker_complete), blocked
    # (errors tagged with this agent), or idle (no events seen).
    worker_completes = {
        e.get("agent")
        for e in history
        if isinstance(e, dict) and e.get("kind") == "worker_complete"
    }
    blocked_agents: set[str] = set()
    for err in errors:
        if isinstance(err, str):
            continue
        owner = (err.get("agent") if isinstance(err, dict) else None) if err else None
        if owner:
            blocked_agents.add(owner)

    agent_timeline = []
    for agent in final_registry:
        aid = agent.get("id", "")
        if aid in blocked_agents:
            status = "blocked"
        elif aid in worker_completes:
            status = "completed"
        else:
            status = "idle"
        agent_timeline.append(
            {
                "id": aid,
                "role": (agent.get("role") or "")[:80],
                "status": status,
            }
        )

    summary = {
        "run_id": run_id,
        "project_path": str(root),
        "steps": chunks_seen,
        "wall_clock_ms": wall,
        "errors": errors,
        "final_registry": final_registry,
        "agent_timeline": agent_timeline,  # HUD-friendly snapshot
        "artifacts": list((final_state.get("artifacts") or {}).keys()),
        "history_tail": history[-10:],
        "checkpoint_path": str(checkpoint_path.resolve()),
        "context_md_path": str((root / ".gemini" / "context.md").resolve()),
    }
    return summary


__all__ = ["run_harness", "RunError"]
"""Worker node: single dispatcher for any agent in the registry.

Processing order within one Worker call (ADR 0004 + gemini_integration.md §7a):

  1. Resolve current agent from ``state.current_target``.
  2. Compose prompt (system_prompt + inbox + tool_results + context).
  3. Call Gemini (injected via ``GeminiClient`` protocol).
  4. If tool_calls returned → set ``pending_tool_calls``, return (Manager routes
     to tool_executor). DO NOT process create_agents in the same turn.
  5. Otherwise parse structured response and run:
     (a) meta_linter.validate on each create_agents entry
     (b) write SYSTEM_PROMPT.md under sandbox, then append to registry
     (c) merge send_messages into inbox
     (d) persist artifacts under sandbox
     (e) drain self inbox
     (f) append history event + mark agent status

Worker is constructed via ``make_worker_node(deps)`` — the dependencies carry
the ``GeminiClient``, ``MetaLinter``, and ``repo_root``. This lets tests swap
all three with mocks without monkey-patching.

Composite-pattern phase responsibility (ADR 0003 addendum):
  When ``workflow.pattern`` contains ``"+"`` (e.g. ``"fan_out_fan_in+producer_reviewer"``),
  Manager reads ``state.phase`` + ``routing_config.phase_map`` to pick the active
  sub-pattern. Setting ``state.phase`` is the **Worker's** responsibility — emit
  a ``status_update.phase`` field in the structured response when the active
  phase should advance. Manager does not auto-advance phase.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .builtin_tools import select_builtins_for_agent
from .contracts import GeminiClient, GeminiResponseLike, MetaLinter, ToolCallDecl
from .sandbox import SandboxViolation, resolve_safe
from .state import AgentMetadata, HarnessState, Message, ToolCall, find_agent
from .tool_discovery import discover_mcp_servers

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", flags=re.DOTALL)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a markdown file with YAML frontmatter into (frontmatter, body).

    Returns an empty dict and the full text if no frontmatter is present.
    Coerces YAML-native ``version`` and ``created_at`` values to strings so
    they satisfy the meta linter's schema checks.
    """
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return {}, text or ""
    fm = yaml.safe_load(match.group(1)) or {}
    if not isinstance(fm, dict):
        fm = {}
    if "version" in fm and not isinstance(fm["version"], str):
        fm["version"] = str(fm["version"])
    created_at = fm.get("created_at")
    if created_at is not None and hasattr(created_at, "isoformat"):
        fm["created_at"] = created_at.isoformat().replace("+00:00", "Z")
    return fm, match.group(2)


def _failures_to_payload(failures: Any) -> list[dict[str, Any]]:
    """Convert a linter's ``failures`` list into JSON-safe dicts for state."""
    out: list[dict[str, Any]] = []
    for f in failures or []:
        out.append(
            {
                "check_name": getattr(f, "check_name", "unknown"),
                "severity": getattr(f, "severity", "error"),
                "message": getattr(f, "message", str(f)),
                "field_path": getattr(f, "field_path", None),
            }
        )
    return out


@dataclass
class WorkerDeps:
    gemini: GeminiClient
    linter: MetaLinter
    repo_root: str | Path = "."
    now: Callable[[], str] = lambda: datetime.now(UTC).isoformat()


def _read_system_prompt(repo_root: Path, rel_path: str) -> str:
    target = resolve_safe(repo_root, rel_path)
    if not target.exists():
        raise FileNotFoundError(f"system_prompt not found: {rel_path}")
    return target.read_text(encoding="utf-8")


def _relevant_recent_errors(
    all_errors: list[Any],
    *,
    agent_id: str,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Pick the most recent errors the current agent can act on.

    Two bucket rule:
      1. Action-specific errors (``create_agent_*``, ``create_skill_*``,
         ``artifact_*``, ``tool_iter_exhausted``) are ALWAYS relevant — the
         ``id`` on these errors refers to the thing the agent tried to
         create, not the agent itself, so filtering by ``agent_id`` would
         hide exactly the feedback that lets the LLM self-correct.
      2. Agent-scoped errors (``system_prompt_error`` etc.) are only
         relevant when their ``agent`` field matches ``agent_id``.
    """
    ACTIONABLE_PREFIXES = ("create_agent", "create_skill", "artifact_")
    relevant: list[dict[str, Any]] = []
    for err in reversed(all_errors or []):
        if not isinstance(err, dict):
            relevant.append({"kind": "note", "detail": str(err)[:500]})
        else:
            kind = err.get("kind", "")
            if (
                kind.startswith(ACTIONABLE_PREFIXES)
                or kind == "tool_iter_exhausted"
            ):
                relevant.append(err)
            else:
                owner = err.get("agent")
                if owner in (None, agent_id):
                    relevant.append(err)
        if len(relevant) >= limit:
            break
    return list(reversed(relevant))


def _compose_prompt(
    agent: AgentMetadata,
    inbox_messages: list[Message],
    tool_results: dict[str, Any],
    workflow_summary: str,
    *,
    recent_errors: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = []
    lines.append("## role")
    lines.append(agent.get("role", ""))
    lines.append("")
    if workflow_summary:
        lines.append("## workflow")
        lines.append(workflow_summary)
        lines.append("")
    if inbox_messages:
        lines.append("## inbox")
        for msg in inbox_messages:
            lines.append(
                f"- from={msg.get('from_id', '?')} kind={msg.get('kind', 'info')}: "
                f"{msg.get('content', '')}"
            )
        lines.append("")
    if tool_results:
        lines.append("## tool_results")
        for call_id, result in tool_results.items():
            lines.append(f"- id={call_id}: {json.dumps(result, default=str)[:2000]}")
        lines.append("")
    if recent_errors:
        lines.append("## previous_errors")
        lines.append(
            "Your previous attempts produced these errors. DO NOT repeat the same "
            "mistakes; adjust your next response to fix them:"
        )
        for err in recent_errors:
            lines.append(f"- {json.dumps(err, ensure_ascii=False, default=str)[:1500]}")
        lines.append("")
    lines.append("## instruction")
    lines.append(
        "Respond with a JSON object. Fields: "
        "text (str, human summary), create_agents (list, optional), "
        "create_skills (list, optional), send_messages (list, optional), "
        "artifacts (list[{path, content}], optional), status_update (str, optional), "
        "test_passed (bool, optional), event_summary (str)."
    )
    lines.append("")
    lines.append("## create_agents requirements (strict)")
    lines.append(
        "If you emit `create_agents`, each entry MUST include: "
        "`id` (lowercase slug, ^[a-z][a-z0-9-]*$), "
        "`name`, `role` (>=10 chars), "
        "`system_prompt_path` (must be under `.agents/{id}/SYSTEM_PROMPT.md`), "
        "`system_prompt_body` (full Markdown body, WITHOUT frontmatter, >=400 chars, "
        "containing sections: `## 핵심 역할`, `## 작업 원칙`, `## 입력/출력 프로토콜`, "
        "`## 에러 핸들링`, `## 자가 검증`). "
        "We add YAML frontmatter mechanically — you must not include it. "
        "Entries missing `system_prompt_body` will be REJECTED — do not omit it."
    )
    return "\n".join(lines)


def _parse_structured_response(resp: GeminiResponseLike) -> dict[str, Any]:
    text = (resp.text or "").strip()
    if not text:
        return {"event_summary": "empty response"}

    parsed: dict[str, Any]
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            parsed = {"text": text, "event_summary": "non-dict response"}
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                if not isinstance(parsed, dict):
                    parsed = {"text": text, "event_summary": "non-dict response"}
            except json.JSONDecodeError:
                parsed = {"text": text, "event_summary": "unparsed text"}
        else:
            parsed = {"text": text, "event_summary": "unparsed text"}

    parsed.setdefault("event_summary", parsed.get("text", "")[:200] or "no summary")
    return parsed


def _atomic_write(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    If the file already exists with identical content, skip (checkpoint replay
    safety — ADR 0004).
    """
    if path.exists():
        existing = path.read_bytes()
        new = content.encode("utf-8")
        if hashlib.sha256(existing).digest() == hashlib.sha256(new).digest():
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=str(path.parent), delete=False
    ) as tmp:
        tmp.write(content)
        tmp_name = tmp.name
    os.replace(tmp_name, str(path))


def _materialize_agent_meta(
    entry: dict[str, Any], created_by: str, now: str
) -> AgentMetadata:
    agent: AgentMetadata = {
        "id": entry["id"],
        "name": entry.get("name", entry["id"]),
        "role": entry.get("role", ""),
        "system_prompt_path": entry["system_prompt_path"],
        "skills": list(entry.get("skills", []) or []),
        "tools": list(entry.get("tools", []) or []),
        "status": "idle",
        "created_at": entry.get("created_at") or now,
        "created_by": created_by,
    }
    if "group" in entry:
        agent["group"] = entry["group"]
    if "temperature" in entry:
        agent["temperature"] = float(entry["temperature"])
    return agent


def _tool_calls_to_state(
    tool_calls: list[ToolCallDecl], caller_agent: str
) -> list[ToolCall]:
    out: list[ToolCall] = []
    for tc in tool_calls:
        out.append(
            {
                "id": tc.id,
                "name": tc.name,
                "args": dict(tc.args or {}),
                "caller_agent": caller_agent,
            }
        )
    return out


def _build_tool_declarations(
    *,
    agent_tools: list[str],
    repo_root: Path,
) -> list[Any]:
    """Turn ``agent.tools`` labels into Gemini function declarations.

    Resolves in priority order (see ``builtin_tools.py`` docstring):
      1. ``mcp:<server>/<tool>`` — if ``<server>`` is in the user's
         Gemini CLI settings, expose the specific tool name as a Gemini
         function-call. tool_executor routes via mcp_adapter.
      2. ``file-manager`` / ``read-file`` / ``list-files`` / ``glob`` —
         activate our sandboxed Python fallback (builtin:*).

    Labels we do NOT currently auto-wire (agents should rely on
    pre-collection or send_messages instead):
      - ``google-search``
      - ``write_todos`` (client-only anyway)
    """
    from .contracts import ToolDecl

    decls: list[Any] = []
    seen: set[str] = set()

    # Built-in fallbacks (file ops).
    for tool in select_builtins_for_agent(agent_tools):
        if tool.name in seen:
            continue
        seen.add(tool.name)
        decls.append(
            ToolDecl(
                name=tool.name,
                description=tool.description,
                parameters_json_schema=tool.parameters_json_schema,
            )
        )

    # External MCP proxy. We only surface mcp:<server>/<tool> labels whose
    # server is actually discoverable — otherwise the tool_call would fail.
    mcp_labels = [t for t in agent_tools if isinstance(t, str) and t.startswith("mcp:")]
    if mcp_labels:
        try:
            discovered = discover_mcp_servers(repo_root)
        except Exception:
            discovered = {}
        for label in mcp_labels:
            rest = label[4:]
            if "/" not in rest:
                continue
            server_name, tool_name = rest.split("/", 1)
            if server_name not in discovered:
                continue
            decl_name = f"mcp__{server_name}__{tool_name}"
            if decl_name in seen:
                continue
            seen.add(decl_name)
            decls.append(
                ToolDecl(
                    name=decl_name,
                    description=(
                        f"Proxy call to the user's ``{server_name}`` MCP server "
                        f"(tool ``{tool_name}``). Arguments and return shape are "
                        "defined by that server — pass whatever dict the remote "
                        "tool expects."
                    ),
                    parameters_json_schema={
                        "type": "object",
                        "additionalProperties": True,
                    },
                )
            )
    return decls


def make_worker_node(deps: WorkerDeps):
    """Return a worker_node bound to the provided dependencies."""
    repo_root = Path(deps.repo_root).resolve()

    def worker_node(state: HarnessState) -> dict[str, Any]:
        agent_id = state.get("current_target")
        if not agent_id:
            return {
                "errors": [{"kind": "worker_no_target"}],
            }
        agent = find_agent(state.get("registry", []), agent_id)
        if agent is None:
            return {
                "errors": [{"kind": "worker_missing_agent", "id": agent_id}],
            }

        try:
            system_prompt = _read_system_prompt(
                repo_root, agent.get("system_prompt_path", "")
            )
        except (SandboxViolation, FileNotFoundError) as exc:
            return {
                "errors": [
                    {
                        "kind": "system_prompt_error",
                        "agent": agent_id,
                        "detail": str(exc),
                    }
                ]
            }

        inbox_messages: list[Message] = list(
            (state.get("inbox") or {}).get(agent_id, [])
        )
        workflow = state.get("workflow") or {}
        workflow_summary = (
            f"pattern={workflow.get('pattern')} phase={state.get('phase')}"
        )
        prompt = _compose_prompt(
            agent=agent,
            inbox_messages=inbox_messages,
            tool_results=state.get("tool_results") or {},
            workflow_summary=workflow_summary,
            recent_errors=_relevant_recent_errors(
                state.get("errors") or [], agent_id=agent_id
            ),
        )

        temperature = float(agent.get("temperature", 0.7))
        # Build tool declarations from agent.tools: built-in file-manager
        # helpers + any external MCP servers discovered in project/user
        # Gemini CLI settings. Absent agent.tools ⇒ no function-calling
        # ⇒ agent replies with plain JSON text only.
        declarations = _build_tool_declarations(
            agent_tools=list(agent.get("tools") or []),
            repo_root=repo_root,
        )
        response = deps.gemini(
            prompt=prompt,
            system=system_prompt,
            temperature=temperature,
            node="worker",
            run_id=state.get("run_id", "unknown"),
            tools=declarations or None,
        )

        # Tool-call branch: stash calls, return — Manager will route to executor.
        if response.tool_calls:
            pending = _tool_calls_to_state(list(response.tool_calls), agent_id)
            return {
                "pending_tool_calls": pending,
                "inbox": {agent_id: []},
                "history": [
                    {
                        "ts": deps.now(),
                        "agent": agent_id,
                        "node": "worker",
                        "kind": "worker_tool_call",
                        "summary": f"requested {len(pending)} tool call(s)",
                    }
                ],
            }

        parsed = _parse_structured_response(response)

        update: dict[str, Any] = {
            "inbox": {agent_id: []},
        }

        new_registry: list[AgentMetadata] = []
        errors: list[dict[str, Any]] = []
        now = deps.now()

        existing_ids = {a.get("id") for a in state.get("registry", [])}

        for entry in parsed.get("create_agents", []) or []:
            if not isinstance(entry, dict) or "id" not in entry:
                errors.append({"kind": "create_agent_malformed", "detail": entry})
                continue
            if entry["id"] in existing_ids or entry["id"] in {
                a["id"] for a in new_registry
            }:
                errors.append({"kind": "create_agent_duplicate", "id": entry["id"]})
                continue
            # Sandbox guard on the declared path BEFORE parsing / writing.
            try:
                target = resolve_safe(repo_root, entry.get("system_prompt_path", ""))
            except SandboxViolation as exc:
                errors.append(
                    {
                        "kind": "create_agent_sandbox_violation",
                        "id": entry["id"],
                        "detail": str(exc),
                    }
                )
                continue
            raw_body = entry.get("system_prompt_body")
            if not isinstance(raw_body, str) or len(raw_body.strip()) < 200:
                # Do NOT fabricate a minimal body — that silently produced
                # agents that always failed the linter and put the graph in
                # an infinite retry loop. Surface a concrete error so the
                # next prompt can feed it back to the LLM.
                errors.append(
                    {
                        "kind": "create_agent_missing_body",
                        "id": entry["id"],
                        "detail": (
                            "system_prompt_body is missing or too short "
                            "(need >= 200 chars). Provide the full Markdown "
                            "body with sections '## 핵심 역할', '## 작업 원칙', "
                            "'## 입력/출력 프로토콜', '## 에러 핸들링', '## 자가 검증'."
                        ),
                    }
                )
                continue
            # If the model accidentally included YAML frontmatter, strip it —
            # we add our own mechanically with the resolved model/version.
            if raw_body.lstrip().startswith("---"):
                _, raw_body = _split_frontmatter(raw_body)
            # Build the canonical on-disk text with our frontmatter.
            from ..config import get_model as _get_model
            _model = entry.get("model") or _get_model()
            _tools = entry.get("tools") or []
            _tools_str = "[" + ", ".join(_tools) + "]" if _tools else "[]"
            canonical_text = (
                "---\n"
                f"name: {entry['id']}\n"
                'version: "1.0"\n'
                f"model: {_model}\n"
                f"tools: {_tools_str}\n"
                "---\n\n"
                f"{raw_body.rstrip()}\n"
            )
            # Re-parse the canonical version so the linter sees the real
            # frontmatter we'll persist.
            frontmatter, body = _split_frontmatter(canonical_text)
            raw_body = canonical_text
            lint = deps.linter.lint_agent(frontmatter, body, entry)
            if not getattr(lint, "passed", False):
                errors.append(
                    {
                        "kind": "create_agent_lint_failed",
                        "id": entry["id"],
                        "failures": _failures_to_payload(
                            getattr(lint, "failures", [])
                        ),
                    }
                )
                continue
            try:
                _atomic_write(target, raw_body)
            except OSError as exc:
                errors.append(
                    {
                        "kind": "create_agent_write_failed",
                        "id": entry["id"],
                        "detail": str(exc),
                    }
                )
                continue
            new_registry.append(_materialize_agent_meta(entry, agent_id, now))

        if new_registry:
            update["registry"] = new_registry

        # send_messages → inbox (merge_inboxes reducer handles concat).
        inbox_additions: dict[str, list[Message]] = {}
        for msg in parsed.get("send_messages", []) or []:
            if not isinstance(msg, dict):
                continue
            to = msg.get("to")
            if not to:
                continue
            inbox_additions.setdefault(to, []).append(
                {
                    "from_id": agent_id,
                    "to": to,
                    "content": msg.get("content", ""),
                    "kind": msg.get("kind", "info"),
                    "ts": now,
                }
            )
        if inbox_additions:
            merged = {agent_id: []}
            merged.update(inbox_additions)
            update["inbox"] = merged

        # artifacts → write to disk under sandbox, store path->content (small) or
        # path->pointer (large). v1 stores content as a marker string.
        artifact_writes: dict[str, str] = {}
        for art in parsed.get("artifacts", []) or []:
            if not isinstance(art, dict):
                continue
            path = art.get("path")
            content = art.get("content", "")
            if not path or not isinstance(content, str):
                continue
            try:
                target = resolve_safe(repo_root, path)
                _atomic_write(target, content)
                if len(content) <= 1024:
                    artifact_writes[path] = content
                else:
                    artifact_writes[path] = f"<{len(content)} bytes>"
            except (SandboxViolation, OSError) as exc:
                errors.append(
                    {
                        "kind": "artifact_write_failed",
                        "path": path,
                        "detail": str(exc),
                    }
                )
        if artifact_writes:
            update["artifacts"] = artifact_writes

        # Note: registry entries are reduced by ``append_unique`` (id-keyed), so
        # in-place status edits via the reducer are not possible. Agent
        # completion is signalled via the ``worker_complete`` history event,
        # which the routing functions consult.

        if "test_passed" in parsed and isinstance(parsed["test_passed"], bool):
            update["test_passed"] = parsed["test_passed"]

        if "phase" in parsed and isinstance(parsed["phase"], str):
            update["phase"] = parsed["phase"]

        # history event.
        update.setdefault("history", []).append(
            {
                "ts": now,
                "agent": agent_id,
                "node": "worker",
                "kind": "worker_complete",
                "summary": str(parsed.get("event_summary", ""))[:500],
                "create_agent_errors": sum(
                    1 for e in errors if e.get("kind", "").startswith("create_agent")
                ),
                "agents_added": len(new_registry),
            }
        )

        if errors:
            update["errors"] = errors

        return update

    return worker_node


def make_aworker_node(deps: WorkerDeps):
    """Async variant of ``make_worker_node``.

    Identical semantics to the sync worker, but the (sync) ``deps.gemini``
    call is offloaded to a thread via ``asyncio.to_thread`` so that
    LangGraph's ``astream`` / ``ainvoke`` runtime can execute multiple
    ``Send``-dispatched workers truly in parallel (wall-clock concurrency).

    Use this with ``arun_harness`` / ``.astream(...)``. Fan-out/fan-in
    patterns that would otherwise serialize in sync mode become parallel.
    """
    import asyncio

    # Reuse the sync worker logic by composing it. Only the Gemini call is the
    # blocking point, but the entire worker body runs outside an event loop
    # so we run the whole thing in a thread — keeps the two implementations
    # in lockstep without code duplication.
    sync_worker = make_worker_node(deps)

    async def aworker_node(state: HarnessState) -> dict[str, Any]:
        return await asyncio.to_thread(sync_worker, state)

    return aworker_node


__all__ = ["WorkerDeps", "make_worker_node", "make_aworker_node"]

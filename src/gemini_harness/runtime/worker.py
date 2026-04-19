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

from .contracts import GeminiClient, GeminiResponseLike, MetaLinter, ToolCallDecl
from .sandbox import SandboxViolation, resolve_safe
from .state import AgentMetadata, HarnessState, Message, ToolCall, find_agent

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


def _compose_prompt(
    agent: AgentMetadata,
    inbox_messages: list[Message],
    tool_results: dict[str, Any],
    workflow_summary: str,
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
    lines.append("## instruction")
    lines.append(
        "Respond with a JSON object. Fields: "
        "text (str, human summary), create_agents (list, optional), "
        "create_skills (list, optional), send_messages (list, optional), "
        "artifacts (list[{path, content}], optional), status_update (str, optional), "
        "test_passed (bool, optional), event_summary (str)."
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
        )

        temperature = float(agent.get("temperature", 0.7))
        response = deps.gemini(
            prompt=prompt,
            system=system_prompt,
            temperature=temperature,
            node="worker",
            run_id=state.get("run_id", "unknown"),
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
            if not isinstance(raw_body, str) or not raw_body.strip():
                raw_body = (
                    f"---\nname: {entry['id']}\n"
                    f"version: 1.0\n"
                    f"model: {entry.get('model', 'gemini-3.1-pro-preview')}\n"
                    f"tools: []\n---\n\n"
                    f"# {entry.get('name', entry['id'])}\n\n"
                    f"## 핵심 역할\n\n{entry.get('role', '')}\n\n"
                    f"## 자가 검증\n\nTODO\n"
                )
            frontmatter, body = _split_frontmatter(raw_body)
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
            }
        )

        if errors:
            update["errors"] = errors

        return update

    return worker_node


__all__ = ["WorkerDeps", "make_worker_node"]

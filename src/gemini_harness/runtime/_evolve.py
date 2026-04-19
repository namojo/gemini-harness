"""``run_evolve`` — Phase 7 incremental harness adjustment (feedback-driven).

Unlike ``run_build`` which regenerates everything, evolve makes **targeted**
modifications. The architect Gemini call gets the existing harness state plus
the user's feedback, and proposes a minimal diff — change one agent's role,
add a new agent, tune routing_config, etc.

The diff is applied (or surfaced as dry-run) and CLAUDE.md gets a new row in
its change-log table.
"""
from __future__ import annotations


def _resolve_model() -> str:
    from ..config import get_model
    return get_model()


import difflib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..meta import lint_agent, lint_skill, lint_workflow
from ._audit import _load_workflow, _split_frontmatter


MAX_EVOLVE_RETRIES = 2


class EvolveError(RuntimeError):
    """run_evolve failed."""


EVOLVE_SYSTEM_PROMPT = """You are the evolution agent for Gemini-Harness. Given an existing harness (workflow.json + agent bodies) and user feedback, propose the MINIMAL set of changes.

Return JSON only (no fences, no prose) with this shape:

{
  "summary": "<Korean, 1-2 sentences describing the change>",
  "changes": [
    {
      "kind": "agent_update" | "agent_add" | "skill_update" | "skill_add" | "routing_config" | "workflow_field",
      "target": "<agent_id | skill_name | routing_config.key | workflow.field>",
      "old_excerpt": "<short snippet of the current value being replaced, for audit>",
      "new_content": "<the new value — full SYSTEM_PROMPT.md body for agent_update/add, full SKILL.md body for skill_update/add, JSON value otherwise>",
      "rationale": "<Korean, 1 sentence why>"
    }
  ]
}

Rules:
- Be SURGICAL. If feedback says "reviewer too lenient", only change the reviewer's SYSTEM_PROMPT.md body. Don't touch other agents.
- agent_update / skill_update: new_content is full body WITHOUT frontmatter (we add it mechanically)
- agent_add: produce the full agent entry (id, name, role, system_prompt_body, skills, tools) as object in new_content
- skill_add: full skill entry (name, description, runtime, entry, body)
- routing_config: new_content is the JSON value to set (string/number/object)
- workflow_field: target is dotted path like "retry_limit" or "pattern"; new_content is the new JSON value
- If scope hints are provided, stay within them unless you have a strong reason (note in rationale)
- If the feedback is unclear or dangerous, return {"error": "<reason>"}

Input format:
---
<workflow.json snapshot>
---
<sample agent bodies>
---
<user feedback>
---
<optional scope hints>
"""


def _read_agent_body(project_path: Path, agent_id: str) -> str | None:
    path = project_path / ".agents" / agent_id / "SYSTEM_PROMPT.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    _, body = _split_frontmatter(text)
    return body


def _read_skill_body(project_path: Path, name: str) -> str | None:
    path = project_path / ".agents" / "skills" / name / "SKILL.md"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    _, body = _split_frontmatter(text)
    return body


def _compose_context(project_path: Path, workflow: dict) -> str:
    """Serialize the existing harness for the architect prompt."""
    parts = ["--- workflow.json ---", json.dumps(workflow, ensure_ascii=False, indent=2)]
    parts.append("\n--- agent bodies (first 800 chars each) ---")
    for agent in workflow.get("initial_registry", []):
        aid = agent.get("id", "")
        body = _read_agent_body(project_path, aid) or "(missing)"
        parts.append(f"\n# agent: {aid}\n{body[:800]}")
    parts.append("\n--- end ---")
    return "\n".join(parts)


def _validate_scope(scope: list[dict] | None, workflow: dict) -> list[str]:
    if not scope:
        return []
    errors: list[str] = []
    agent_ids = {a["id"] for a in workflow.get("initial_registry", [])}
    for entry in scope:
        kind = entry.get("kind")
        target = entry.get("id", "")
        if kind == "agent" and target and target not in agent_ids:
            errors.append(f"scope references unknown agent: {target!r}")
    return errors


def _unified_diff(old: str, new: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"{path} (before)",
            tofile=f"{path} (after)",
            lineterm="",
        )
    )


def _wrap_system_prompt(agent_id: str, tools: list[str], body: str) -> str:
    model = _resolve_model()
    tools_str = "[" + ", ".join(tools) + "]" if tools else "[]"
    return (
        "---\n"
        f"name: {agent_id}\n"
        'version: "1.0"\n'
        f"model: {model}\n"
        f"tools: {tools_str}\n"
        "---\n\n" + body.rstrip() + "\n"
    )


def _wrap_skill(name: str, description: str, runtime: str, entry: str, body: str) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        'version: "1.0"\n'
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        f"runtime: {runtime}\n"
        f"entry: {entry}\n"
        "---\n\n" + body.rstrip() + "\n"
    )


def _dotted_set(obj: dict, path: str, value: Any) -> None:
    keys = path.split(".")
    d = obj
    for k in keys[:-1]:
        if k not in d or not isinstance(d[k], dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value


def _apply_changes(
    design_changes: list[dict],
    project_path: Path,
    workflow: dict,
    dry_run: bool,
) -> tuple[list[dict], list[str]]:
    """Apply or preview each change. Returns (change_records, errors)."""
    changes: list[dict] = []
    errors: list[str] = []

    for ch in design_changes:
        kind = ch.get("kind")
        target = ch.get("target", "")
        new_content = ch.get("new_content")

        if kind == "agent_update":
            agent = next(
                (a for a in workflow["initial_registry"] if a["id"] == target),
                None,
            )
            if agent is None:
                errors.append(f"agent_update: unknown agent {target!r}")
                continue
            sp_path = project_path / ".agents" / target / "SYSTEM_PROMPT.md"
            old_text = sp_path.read_text(encoding="utf-8") if sp_path.exists() else ""
            # Lint before writing
            fm, _body = _split_frontmatter(old_text) if old_text else ({}, "")
            result = lint_agent(
                {
                    "name": target,
                    "version": "1.0",
                    "model": _resolve_model(),
                    "tools": list(agent.get("tools", []) or []),
                },
                new_content,
                agent,
            )
            lint_errs = [f for f in result.failures if f.severity == "error"]
            if lint_errs:
                errors.extend(
                    f"agent_update[{target}].{f.check_name}: {f.message}"
                    for f in lint_errs
                )
                continue
            new_text = _wrap_system_prompt(target, list(agent.get("tools", []) or []), new_content)
            diff = _unified_diff(old_text, new_text, str(sp_path))
            rec = {"path": str(sp_path.resolve()), "operation": "modified", "diff": diff[:10000]}
            if not dry_run:
                sp_path.parent.mkdir(parents=True, exist_ok=True)
                sp_path.write_text(new_text, encoding="utf-8")
            changes.append(rec)

        elif kind == "agent_add":
            if not isinstance(new_content, dict):
                errors.append("agent_add.new_content must be object")
                continue
            aid = new_content.get("id", "")
            if not re.match(r"^[a-z][a-z0-9-]*$", aid):
                errors.append(f"agent_add: invalid id {aid!r}")
                continue
            sp_path = project_path / ".agents" / aid / "SYSTEM_PROMPT.md"
            if sp_path.exists():
                errors.append(f"agent_add: {aid} already exists")
                continue
            body = new_content.get("system_prompt_body", "")
            # Add to registry (in-memory) so linter sees it
            new_agent_meta = {
                "id": aid,
                "name": new_content.get("name", aid),
                "role": new_content.get("role", ""),
                "system_prompt_path": f".agents/{aid}/SYSTEM_PROMPT.md",
                "skills": list(new_content.get("skills", []) or []),
                "tools": list(new_content.get("tools", []) or []),
            }
            result = lint_agent(
                {
                    "name": aid,
                    "version": "1.0",
                    "model": _resolve_model(),
                    "tools": new_agent_meta["tools"],
                },
                body,
                new_agent_meta,
            )
            lint_errs = [f for f in result.failures if f.severity == "error"]
            if lint_errs:
                errors.extend(
                    f"agent_add[{aid}].{f.check_name}: {f.message}" for f in lint_errs
                )
                continue
            workflow["initial_registry"].append(new_agent_meta)
            new_text = _wrap_system_prompt(aid, new_agent_meta["tools"], body)
            diff = _unified_diff("", new_text, str(sp_path))
            rec = {"path": str(sp_path.resolve()), "operation": "created", "diff": diff[:10000]}
            if not dry_run:
                sp_path.parent.mkdir(parents=True, exist_ok=True)
                sp_path.write_text(new_text, encoding="utf-8")
            changes.append(rec)

        elif kind == "skill_update":
            sk_path = project_path / ".agents" / "skills" / target / "SKILL.md"
            if not sk_path.exists():
                errors.append(f"skill_update: unknown skill {target!r}")
                continue
            old_text = sk_path.read_text(encoding="utf-8")
            fm, _ = _split_frontmatter(old_text)
            new_text = _wrap_skill(
                target,
                fm.get("description", ""),
                fm.get("runtime", "python"),
                fm.get("entry", "scripts/main.py"),
                new_content or "",
            )
            diff = _unified_diff(old_text, new_text, str(sk_path))
            rec = {"path": str(sk_path.resolve()), "operation": "modified", "diff": diff[:10000]}
            if not dry_run:
                sk_path.write_text(new_text, encoding="utf-8")
            changes.append(rec)

        elif kind == "routing_config":
            key = target
            old_value = workflow.get("routing_config", {}).get(key)
            workflow.setdefault("routing_config", {})[key] = new_content
            diff = _unified_diff(
                json.dumps({key: old_value}, ensure_ascii=False, indent=2),
                json.dumps({key: new_content}, ensure_ascii=False, indent=2),
                "routing_config." + key,
            )
            rec = {
                "path": f"{(project_path / 'workflow.json').resolve()}#routing_config.{key}",
                "operation": "modified",
                "diff": diff,
            }
            changes.append(rec)

        elif kind == "workflow_field":
            old_snap = json.dumps(workflow, ensure_ascii=False, indent=2, default=str)
            _dotted_set(workflow, target, new_content)
            new_snap = json.dumps(workflow, ensure_ascii=False, indent=2, default=str)
            diff = _unified_diff(old_snap, new_snap, str(project_path / "workflow.json"))
            rec = {
                "path": f"{(project_path / 'workflow.json').resolve()}#{target}",
                "operation": "modified",
                "diff": diff[:10000],
            }
            changes.append(rec)

        else:
            errors.append(f"unknown change kind: {kind!r}")

    return changes, errors


def _write_workflow(project_path: Path, workflow: dict) -> str:
    wf_path = project_path / "workflow.json"
    # Re-validate before committing
    result = lint_workflow(workflow)
    fatal = [f for f in result.failures if f.severity == "error"]
    if fatal:
        raise EvolveError(
            "workflow.json would fail linter after evolve: "
            + "; ".join(f"{f.check_name}: {f.message}" for f in fatal[:3])
        )
    wf_path.write_text(
        json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return str(wf_path.resolve())


def _append_context_event(project_path: Path, feedback: str, summary: str) -> bool:
    ctx = project_path / ".gemini" / "context.md"
    ctx.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ctx.open("a", encoding="utf-8") as f:
        f.write(
            f"\n## [{ts}] event=evolve\n"
            f"- feedback: {feedback}\n"
            f"- summary: {summary}\n"
        )
    return True


def _append_claude_md_history(project_path: Path, summary: str, feedback: str) -> None:
    md = project_path / "CLAUDE.md"
    if not md.exists():
        return
    text = md.read_text(encoding="utf-8")
    # Find the 변경 이력 table and append a row. Best-effort: find last table row.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_row = f"| {today} | {summary} | harness.evolve | {feedback[:80]}{'…' if len(feedback) > 80 else ''} |\n"
    if "| 날짜 |" in text and "| 변경 내용 |" in text:
        # Find end of the change-log table (blank line after a pipe line)
        lines = text.splitlines(keepends=True)
        # Find last row line in the table and insert after it
        last_pipe = -1
        for i, line in enumerate(lines):
            if line.lstrip().startswith("|") and "|" in line[1:]:
                last_pipe = i
        if last_pipe >= 0:
            lines.insert(last_pipe + 1, new_row)
            md.write_text("".join(lines), encoding="utf-8")


def _default_gemini_client():
    from ..integrations.gemini_client import GeminiClient

    return GeminiClient()


def _extract_json(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1 :]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start, end = s.find("{"), s.rfind("}")
    return s[start : end + 1] if start >= 0 and end > start else s


def run_evolve(
    *,
    project_path: str,
    feedback: str,
    scope: list[dict] | None = None,
    dry_run: bool = False,
    gemini_client: Any | None = None,
) -> dict:
    """Apply feedback-driven changes to an existing harness.

    Returns ``{applied, changes, context_log_appended, metrics}``.
    """
    root = Path(project_path).resolve()
    try:
        from dotenv import load_dotenv

        env = root / ".env"
        if env.exists():
            load_dotenv(env, override=False)
    except ImportError:
        pass

    if len(feedback) < 10:
        raise EvolveError("feedback must be at least 10 characters")

    workflow, drift = _load_workflow(root)
    if workflow is None:
        raise EvolveError("HARNESS_NOT_INITIALIZED: no workflow.json at project_path")

    scope_errors = _validate_scope(scope, workflow)
    if scope_errors:
        raise EvolveError("INVALID_INPUT: " + "; ".join(scope_errors))

    client = gemini_client or _default_gemini_client()
    model = _resolve_model()

    user_prompt_parts = [
        _compose_context(root, workflow),
        "\n--- user feedback ---",
        feedback,
    ]
    if scope:
        user_prompt_parts.append("\n--- scope hints ---")
        user_prompt_parts.append(json.dumps(scope, ensure_ascii=False))
    user_prompt = "\n".join(user_prompt_parts)

    start = time.monotonic()
    metrics = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "wall_clock_ms": 0}
    last_errors: list[str] = []
    design: dict | None = None
    last_text = ""

    prompt = user_prompt
    for attempt in range(MAX_EVOLVE_RETRIES + 1):
        resp = client.call(
            prompt=prompt,
            system=EVOLVE_SYSTEM_PROMPT,
            context="",
            temperature=0.3,
            model=model,
        )
        metrics["calls"] += 1
        usage = getattr(resp, "usage", None)
        if usage is not None:
            metrics["input_tokens"] += int(getattr(usage, "prompt_token_count", 0) or 0)
            metrics["output_tokens"] += int(getattr(usage, "candidates_token_count", 0) or 0)
        text = (getattr(resp, "text", "") or "").strip()
        last_text = text
        if not text:
            last_errors = ["empty response"]
            prompt = f"Previous attempt returned empty. Try again:\n{user_prompt}"
            continue
        try:
            candidate = json.loads(_extract_json(text))
        except json.JSONDecodeError as exc:
            last_errors = [f"JSON parse: {exc}"]
            prompt = f"Previous attempt had parse error ({exc}). Return valid JSON only:\n{user_prompt}"
            continue

        if "error" in candidate:
            raise EvolveError(f"architect refused: {candidate['error']}")
        changes_proposed = candidate.get("changes", [])
        if not changes_proposed:
            last_errors = ["no changes proposed"]
            prompt = (
                "You proposed zero changes but feedback is present. "
                f"Re-examine and emit at least one change:\n{user_prompt}"
            )
            continue
        design = candidate
        break

    if design is None:
        metrics["wall_clock_ms"] = int((time.monotonic() - start) * 1000)
        raise EvolveError(
            f"evolve failed after {MAX_EVOLVE_RETRIES + 1} attempts. last errors: "
            + "; ".join(last_errors[:3])
        )

    summary = design.get("summary", "evolve applied")
    changes, apply_errors = _apply_changes(
        design["changes"], root, workflow, dry_run=dry_run
    )

    context_appended = False
    if not dry_run and not apply_errors:
        # Persist workflow changes (routing_config/workflow_field edits mutated it in-place)
        _write_workflow(root, workflow)
        _append_claude_md_history(root, summary, feedback)
        context_appended = _append_context_event(root, feedback, summary)

    metrics["wall_clock_ms"] = int((time.monotonic() - start) * 1000)

    if apply_errors:
        return {
            "applied": False,
            "changes": changes,
            "context_log_appended": False,
            "metrics": metrics,
            "errors": apply_errors,
            "summary": summary,
        }

    return {
        "applied": not dry_run,
        "changes": changes,
        "context_log_appended": context_appended,
        "metrics": metrics,
        "summary": summary,
    }


__all__ = ["run_evolve", "EvolveError"]
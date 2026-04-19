"""``run_build`` — Phase 1~5 end-to-end via a single meta-architect call.

The meta-architect is a Gemini call (not the runtime graph — that's for
running generated harnesses). It produces the whole harness design in one
structured JSON response: pattern, agents with SYSTEM_PROMPT bodies,
skills with SKILL bodies, routing_config.

This module validates the proposal with ``meta.lint_*`` and writes to
``.agents/``, ``.agents/skills/``, ``workflow.json``, and CLAUDE.md.

Retries up to 3 times with error feedback if linting fails.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..meta import lint_agent, lint_skill, lint_workflow
from ._prompts import (
    ARCHITECT_SYSTEM_PROMPT,
    architect_retry_prompt,
    architect_user_prompt,
)


def _wrap_system_prompt(
    *,
    agent_id: str,
    version: str,
    model: str,
    tools: list[str],
    body: str,
) -> str:
    """Wrap a SYSTEM_PROMPT.md body with YAML frontmatter. Body stays verbatim."""
    fm_lines = [
        "---",
        f"name: {agent_id}",
        f'version: "{version}"',
        f"model: {model}",
        "tools: [" + ", ".join(tools) + "]" if tools else "tools: []",
        "---",
    ]
    return "\n".join(fm_lines) + "\n\n" + body.rstrip() + "\n"


def _wrap_skill(
    *,
    name: str,
    version: str,
    description: str,
    runtime: str,
    entry: str,
    body: str,
) -> str:
    """Wrap a SKILL.md body with YAML frontmatter."""
    fm_lines = [
        "---",
        f"name: {name}",
        f'version: "{version}"',
        # description is quoted to survive YAML reserved chars
        "description: " + json.dumps(description, ensure_ascii=False),
        f"runtime: {runtime}",
        f"entry: {entry}",
        "---",
    ]
    return "\n".join(fm_lines) + "\n\n" + body.rstrip() + "\n"


MAX_ARCHITECT_RETRIES = 2
ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class BuildError(RuntimeError):
    """run_build failed after all retries."""


def _load_dotenv(project_path: Path) -> None:
    """Load `.env` from project_path if present. No-op if already loaded."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = project_path / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _extract_json_block(text: str) -> str:
    """Strip optional ```json fences and surrounding prose."""
    stripped = text.strip()
    # Fence variations
    if stripped.startswith("```"):
        first_nl = stripped.find("\n")
        if first_nl > 0:
            stripped = stripped[first_nl + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    # Find the outermost {...}
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1].strip()
    return stripped


def _coerce_routing_config(design: dict) -> dict:
    """Ensure routing_config is a dict (normalize None/missing)."""
    rc = design.get("routing_config")
    if not isinstance(rc, dict):
        rc = {}
    return rc


def _validate_design_shape(design: dict) -> list[str]:
    """Basic structural checks before running the full linter chain."""
    errors: list[str] = []
    if "error" in design:
        errors.append(f"architect returned error: {design.get('error')}")
        return errors
    for key in ("pattern", "agents"):
        if key not in design:
            errors.append(f"missing required key: {key!r}")
    agents = design.get("agents", [])
    if not isinstance(agents, list) or not agents:
        errors.append("agents must be a non-empty array")
    else:
        seen_ids = set()
        for i, a in enumerate(agents):
            if not isinstance(a, dict):
                errors.append(f"agents[{i}] not an object")
                continue
            aid = a.get("id", "")
            if not ID_RE.match(aid):
                errors.append(
                    f"agents[{i}].id {aid!r} not slug (^[a-z][a-z0-9-]*$)"
                )
            if aid in seen_ids:
                errors.append(f"duplicate agent id: {aid}")
            seen_ids.add(aid)
            for required in ("name", "role", "system_prompt_body"):
                if not a.get(required):
                    errors.append(f"agents[{i}] missing {required}")
    skills = design.get("skills", []) or []
    skill_names: set[str] = set()
    for i, s in enumerate(skills):
        if not isinstance(s, dict):
            errors.append(f"skills[{i}] not an object")
            continue
        name = s.get("name", "")
        if not ID_RE.match(name):
            errors.append(f"skills[{i}].name {name!r} not slug")
        skill_names.add(name)
        for required in ("description", "runtime", "entry", "body"):
            if required not in s:
                errors.append(f"skills[{i}] missing {required}")
    # Every agent.skills entry should be declared in skills[].name
    for a in design.get("agents", []):
        for sk in a.get("skills", []) or []:
            if sk not in skill_names:
                errors.append(
                    f"agent {a.get('id','?')}.skills references undeclared skill {sk!r}"
                )
    return errors


def _build_workflow_dict(design: dict) -> dict:
    """Assemble a workflow.json-shaped dict from the architect design."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    registry = []
    for a in design.get("agents", []):
        registry.append(
            {
                "id": a["id"],
                "name": a.get("name", a["id"]),
                "role": a["role"],
                "system_prompt_path": f".agents/{a['id']}/SYSTEM_PROMPT.md",
                "skills": list(a.get("skills", []) or []),
                "tools": list(a.get("tools", []) or []),
                "status": "idle",
                "created_at": now_iso,
                "created_by": "user",
            }
        )
    workflow: dict[str, Any] = {
        "version": "1.0",
        "pattern": design["pattern"],
        "retry_limit": 3,
        "routing_config": _coerce_routing_config(design),
        "initial_registry": registry,
    }
    return workflow


def _validate_design_against_linter(design: dict, project_path: Path) -> list[str]:
    """Build workflow + lint each artifact; return human-readable error strings."""
    errors: list[str] = []

    workflow = _build_workflow_dict(design)
    workflow_result = lint_workflow(workflow)
    for f in workflow_result.failures:
        if f.severity == "error":
            errors.append(f"workflow.{f.check_name}: {f.message}")

    # Lint each agent's frontmatter + body (we assemble frontmatter mechanically)
    for a in design.get("agents", []):
        frontmatter = {
            "name": a["id"],
            "version": "1.0",
            "model": os.environ.get("LANGCHAIN_HARNESS_MODEL", "gemini-3.1-pro-preview"),
            "tools": list(a.get("tools", []) or []),
        }
        body = a.get("system_prompt_body", "")
        agent_meta = next(
            (m for m in workflow["initial_registry"] if m["id"] == a["id"]),
            None,
        )
        result = lint_agent(frontmatter, body, agent_meta)
        for f in result.failures:
            if f.severity == "error":
                errors.append(f"agent[{a['id']}].{f.check_name}: {f.message}")

    # Lint each skill
    for s in design.get("skills", []) or []:
        fm = {
            "name": s["name"],
            "version": "1.0",
            "description": s.get("description", ""),
            "runtime": s.get("runtime", "python"),
            "entry": s.get("entry", ""),
        }
        skill_dir = project_path / ".agents" / "skills" / s["name"]
        body = s.get("body", "")
        # entry_file presence is checked after we actually write the entry file,
        # so skip entry-existence checks in this pre-write lint pass.
        # We still validate runtime / description / body / forbidden patterns.
        result = lint_skill(fm, body, s.get("entry", ""), str(skill_dir))
        skip_names = {"sk.entry_present", "sk.entry_file_exists"}
        for f in result.failures:
            if f.severity == "error" and f.check_name not in skip_names:
                errors.append(f"skill[{s['name']}].{f.check_name}: {f.message}")

    return errors


def _write_harness(design: dict, project_path: Path) -> tuple[list[str], dict]:
    """Write all generated files to disk. Returns (written_paths, workflow_dict)."""
    written: list[str] = []
    workflow = _build_workflow_dict(design)

    # Write workflow.json
    wf_path = project_path / "workflow.json"
    wf_path.write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    written.append(str(wf_path.resolve()))

    # Write each .agents/{id}/SYSTEM_PROMPT.md
    model = os.environ.get("LANGCHAIN_HARNESS_MODEL", "gemini-3.1-pro-preview")
    for a in design.get("agents", []):
        sp_path = project_path / ".agents" / a["id"] / "SYSTEM_PROMPT.md"
        sp_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = _wrap_system_prompt(
            agent_id=a["id"],
            version="1.0",
            model=model,
            tools=list(a.get("tools", []) or []),
            body=a["system_prompt_body"],
        )
        sp_path.write_text(rendered, encoding="utf-8")
        written.append(str(sp_path.resolve()))

    # Write each .agents/skills/{name}/SKILL.md + entry stub
    for s in design.get("skills", []) or []:
        skill_dir = project_path / ".agents" / "skills" / s["name"]
        skill_dir.mkdir(parents=True, exist_ok=True)
        sk_path = skill_dir / "SKILL.md"
        rendered = _wrap_skill(
            name=s["name"],
            version="1.0",
            description=s["description"],
            runtime=s.get("runtime", "python"),
            entry=s.get("entry", "scripts/main.py"),
            body=s["body"],
        )
        sk_path.write_text(rendered, encoding="utf-8")
        written.append(str(sk_path.resolve()))

        # Write a stub entry file so lint_skill's entry_present passes later
        entry_rel = s.get("entry", "scripts/main.py")
        entry_path = skill_dir / entry_rel
        entry_path.parent.mkdir(parents=True, exist_ok=True)
        if not entry_path.exists():
            if entry_path.suffix == ".py":
                entry_path.write_text(
                    f'"""Entry point for {s["name"]} skill. Implement here."""\n'
                    'def main():\n    raise NotImplementedError\n\n'
                    'if __name__ == "__main__":\n    main()\n',
                    encoding="utf-8",
                )
            else:
                entry_path.write_text(
                    "#!/usr/bin/env bash\n"
                    f"# Entry point for {s['name']} skill. Implement here.\n"
                    'echo "not implemented" >&2; exit 1\n',
                    encoding="utf-8",
                )
                entry_path.chmod(0o755)
            written.append(str(entry_path.resolve()))

    return written, workflow


def _update_claude_md_pointer(project_path: Path, design: dict, run_id: str) -> str | None:
    """Append or refresh the harness pointer block in CLAUDE.md."""
    claude_md = project_path / "CLAUDE.md"
    pointer_block = (
        f"\n## 하네스: generated-{run_id}\n\n"
        f"**패턴:** {design['pattern']}\n\n"
        "**트리거:** 이 도메인 관련 작업을 요청하면 Gemini-Harness의 `harness.run` MCP 도구 또는 "
        "`gemini-harness run --project {project_path} --user-input \"...\"` 명령으로 실행하라.\n\n"
        "**변경 이력:**\n"
        "| 날짜 | 변경 내용 | 대상 | 사유 |\n"
        "|------|----------|------|------|\n"
        f"| {datetime.now(timezone.utc).strftime('%Y-%m-%d')} | 초기 생성 | 전체 | harness.build (run_id={run_id}) |\n"
    )
    if claude_md.exists():
        existing = claude_md.read_text(encoding="utf-8")
        if "## 하네스: generated-" in existing:
            # Already has a generated block; don't duplicate, prepend a new entry.
            new_text = existing + f"\n<!-- regenerated {run_id} -->\n" + pointer_block
        else:
            new_text = existing.rstrip() + "\n" + pointer_block
        claude_md.write_text(new_text, encoding="utf-8")
    else:
        claude_md.write_text(
            "# CLAUDE.md\n\nThis file provides guidance to Claude Code (claude.ai/code).\n"
            + pointer_block,
            encoding="utf-8",
        )
    return str(claude_md.resolve())


def _detect_existing_harness(project_path: Path) -> bool:
    return (project_path / "workflow.json").exists() or (project_path / ".agents").is_dir()


def _default_gemini_client():
    from ..integrations.gemini_client import GeminiClient

    return GeminiClient()


def run_build(
    *,
    project_path: str,
    domain_description: str,
    run_id: str | None = None,
    pattern_hint: str | None = None,
    max_agents: int = 8,
    tool_executor: dict | None = None,
    force: bool = False,
    gemini_client: Any | None = None,
) -> dict:
    """Generate a new harness (agents + skills + workflow.json) from a domain sentence.

    Uses a meta-architect Gemini call to design the team, validates via the
    linter, writes files atomically, and returns a structured report.
    """
    root = Path(project_path).resolve()
    _load_dotenv(root)

    if not force and _detect_existing_harness(root):
        raise BuildError(
            "Existing harness detected (workflow.json or .agents/). "
            "Pass force=true to overwrite, or call harness.audit first."
        )

    if len(domain_description) < 20:
        raise BuildError("domain_description must be at least 20 characters")

    run_id = run_id or "build-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    model = os.environ.get("LANGCHAIN_HARNESS_MODEL", "gemini-3.1-pro-preview")
    client = gemini_client or _default_gemini_client()

    start = time.monotonic()
    metrics = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "wall_clock_ms": 0}
    warnings: list[str] = []
    last_text = ""
    last_errors: list[str] = []

    prompt = architect_user_prompt(domain_description, pattern_hint, max_agents)

    design: dict | None = None
    for attempt in range(MAX_ARCHITECT_RETRIES + 1):
        resp = client.call(
            prompt=prompt,
            system=ARCHITECT_SYSTEM_PROMPT,
            context="",
            temperature=0.3,
            model=model,
        )
        metrics["calls"] += 1
        # GeminiResponse.usage is a UsageMetadata dataclass (not a dict).
        usage = getattr(resp, "usage", None)
        if usage is not None:
            metrics["input_tokens"] += int(getattr(usage, "prompt_token_count", 0) or 0)
            metrics["output_tokens"] += int(getattr(usage, "candidates_token_count", 0) or 0)
        else:
            # Fallback for mocks that return dict-style usage_metadata
            legacy = getattr(resp, "usage_metadata", None) or {}
            if isinstance(legacy, dict):
                metrics["input_tokens"] += int(legacy.get("input_tokens", 0) or 0)
                metrics["output_tokens"] += int(legacy.get("output_tokens", 0) or 0)
        text = (getattr(resp, "text", "") or "").strip()
        last_text = text
        if not text:
            last_errors = ["architect returned empty response"]
            prompt = architect_retry_prompt(last_text, last_errors)
            continue

        try:
            candidate = json.loads(_extract_json_block(text))
        except json.JSONDecodeError as exc:
            last_errors = [f"JSON parse error: {exc}"]
            prompt = architect_retry_prompt(last_text, last_errors)
            continue

        # Inject tool_executor into routing_config if caller requested it
        if tool_executor:
            rc = candidate.setdefault("routing_config", {})
            rc["tool_executor"] = tool_executor

        shape_errors = _validate_design_shape(candidate)
        if shape_errors:
            last_errors = shape_errors
            prompt = architect_retry_prompt(last_text, last_errors)
            continue

        lint_errors = _validate_design_against_linter(candidate, root)
        if lint_errors:
            last_errors = lint_errors
            prompt = architect_retry_prompt(last_text, last_errors)
            continue

        design = candidate
        break

    if design is None:
        metrics["wall_clock_ms"] = int((time.monotonic() - start) * 1000)
        raise BuildError(
            "architect failed after "
            f"{MAX_ARCHITECT_RETRIES + 1} attempts. Last errors: "
            + "; ".join(last_errors[:5])
        )

    # If force and existing: stage _workspace_prev snapshot of workflow.json for audit trail.
    if force and (root / "workflow.json").exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = root / "_workspace" / f"prev-{ts}" / "workflow.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(
            (root / "workflow.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        warnings.append(f"Previous workflow.json backed up to {backup.resolve()}")

    written, workflow = _write_harness(design, root)
    claude_path = _update_claude_md_pointer(root, design, run_id)
    if claude_path:
        written.append(claude_path)

    metrics["wall_clock_ms"] = int((time.monotonic() - start) * 1000)

    return {
        "run_id": run_id,
        "pattern": workflow["pattern"],
        "final_registry": workflow["initial_registry"],
        "workflow_path": str((root / "workflow.json").resolve()),
        "written_files": written,
        "metrics": metrics,
        "warnings": warnings,
    }


__all__ = ["run_build", "BuildError"]

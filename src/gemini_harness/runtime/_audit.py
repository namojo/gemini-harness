"""``run_audit`` — Phase 0 현황 감사 (read-only).

Scans an existing project for a previously generated harness and reports
drift between `workflow.json` (registry snapshot) and filesystem state.

Output shape per `_workspace/guide/mcp_tools.md` §1.3.

This module is pure Python — no Gemini API calls — so it works in any
environment without `GEMINI_API_KEY`.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ..meta import lint_agent, lint_skill, lint_workflow


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Parse YAML frontmatter. Returns (frontmatter_dict, body)."""
    import yaml  # pyyaml is a runtime dep

    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    raw = text[3:end].strip("\n")
    body = text[end + 4 :].lstrip("\n")
    try:
        fm = yaml.safe_load(raw) or {}
    except yaml.YAMLError:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    # YAML silently coerces unquoted "1.0" to float; schema expects str.
    # Stringify known-string fields. Preserve at least major.minor for versions.
    v = fm.get("version")
    if isinstance(v, float):
        text = repr(v)
        fm["version"] = text if "." in text else f"{v:.1f}"
    elif isinstance(v, int):
        fm["version"] = f"{v}.0"
    for key in ("model", "name"):
        v = fm.get(key)
        if isinstance(v, (int, float)):
            fm[key] = str(v)
    return fm, body


def _load_workflow(project_path: Path) -> tuple[dict | None, list[dict]]:
    """Load workflow.json if present. Returns (data, drift_entries)."""
    path = project_path / "workflow.json"
    drift: list[dict] = []
    if not path.exists():
        return None, drift
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        drift.append(
            {
                "kind": "schema_violation",
                "subject": "workflow.json",
                "detail": f"JSON parse error: {exc}",
            }
        )
        return None, drift

    result = lint_workflow(data)
    for failure in result.failures:
        if failure.severity == "error":
            drift.append(
                {
                    "kind": "schema_violation",
                    "subject": "workflow.json",
                    "detail": f"{failure.check_name}: {failure.message}",
                }
            )
    return data, drift


def _scan_agents(
    project_path: Path,
    expected_ids: dict[str, dict],
    scanned: list[str],
) -> list[dict]:
    """Scan .agents/{name}/SYSTEM_PROMPT.md. Return drift entries."""
    drift: list[dict] = []
    agents_root = project_path / ".agents"
    scanned.append(str(agents_root))

    filesystem_ids: set[str] = set()
    if agents_root.is_dir():
        for child in sorted(agents_root.iterdir()):
            if not child.is_dir() or child.name == "skills":
                continue
            agent_id = child.name
            filesystem_ids.add(agent_id)
            sp_path = child / "SYSTEM_PROMPT.md"
            if not sp_path.exists():
                drift.append(
                    {
                        "kind": "missing_prompt_file",
                        "subject": agent_id,
                        "detail": f"{sp_path} not found",
                    }
                )
                continue
            try:
                text = sp_path.read_text(encoding="utf-8")
            except OSError as exc:
                drift.append(
                    {
                        "kind": "stale_metadata",
                        "subject": agent_id,
                        "detail": f"read error: {exc}",
                    }
                )
                continue
            fm, body = _split_frontmatter(text)
            meta_info = expected_ids.get(agent_id)
            result = lint_agent(fm, body, meta_info)
            for failure in result.failures:
                if failure.severity == "error":
                    drift.append(
                        {
                            "kind": "schema_violation",
                            "subject": agent_id,
                            "detail": f"{failure.check_name}: {failure.message}",
                        }
                    )

    # Registry says X exists but no file
    for registry_id in expected_ids:
        if registry_id not in filesystem_ids:
            drift.append(
                {
                    "kind": "missing_prompt_file",
                    "subject": registry_id,
                    "detail": f".agents/{registry_id}/SYSTEM_PROMPT.md absent",
                }
            )
    # File exists but not in registry
    if expected_ids:
        for fs_id in filesystem_ids:
            if fs_id not in expected_ids:
                drift.append(
                    {
                        "kind": "orphan_prompt_file",
                        "subject": fs_id,
                        "detail": f".agents/{fs_id}/ not referenced by workflow.json",
                    }
                )

    return drift


def _scan_skills(project_path: Path, scanned: list[str]) -> list[dict]:
    """Scan .agents/skills/{name}/SKILL.md. Return drift entries."""
    drift: list[dict] = []
    skills_root = project_path / ".agents" / "skills"
    scanned.append(str(skills_root))
    if not skills_root.is_dir():
        return drift

    for child in sorted(skills_root.iterdir()):
        if not child.is_dir():
            continue
        sk_path = child / "SKILL.md"
        if not sk_path.exists():
            drift.append(
                {
                    "kind": "missing_skill_dir",
                    "subject": child.name,
                    "detail": f"{sk_path} missing",
                }
            )
            continue
        try:
            text = sk_path.read_text(encoding="utf-8")
        except OSError as exc:
            drift.append(
                {
                    "kind": "stale_metadata",
                    "subject": child.name,
                    "detail": f"read error: {exc}",
                }
            )
            continue
        fm, body = _split_frontmatter(text)
        entry = fm.get("entry", "")
        result = lint_skill(fm, body, entry, str(child))
        for failure in result.failures:
            if failure.severity == "error":
                drift.append(
                    {
                        "kind": "schema_violation",
                        "subject": child.name,
                        "detail": f"{failure.check_name}: {failure.message}",
                    }
                )
    return drift


def _history_digest(project_path: Path, scanned: list[str]) -> dict | None:
    """Parse `.gemini/context.md` and return last-N event kinds grouped."""
    ctx = project_path / ".gemini" / "context.md"
    scanned.append(str(ctx))
    if not ctx.exists():
        return {"present": False, "events_by_kind": {}}
    try:
        lines = ctx.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"present": True, "events_by_kind": {}, "read_error": True}
    kinds: Counter[str] = Counter()
    # Lines look like "## [ts] run=... node=...  event=<kind>" or similar.
    for line in lines[-500:]:
        lower = line.lower()
        for key in (
            "worker_complete",
            "agent-created",
            "tool_executor_complete",
            "error",
            "escalate",
        ):
            if key in lower:
                kinds[key] += 1
    return {
        "present": True,
        "tail_lines": min(len(lines), 500),
        "events_by_kind": dict(kinds),
    }


def run_audit(
    *,
    project_path: str,
    include_skills: bool = True,
    include_history: bool = False,
) -> dict:
    """Audit a project for harness artifacts. Pure read-only."""
    root = Path(project_path).resolve()
    scanned: list[str] = []

    workflow_data, workflow_drift = _load_workflow(root)
    registry: list[dict] = []
    expected_ids: dict[str, dict] = {}
    pattern: str | None = None
    version: str | None = None

    if workflow_data is not None:
        pattern = workflow_data.get("pattern")
        version = workflow_data.get("version")
        registry = list(workflow_data.get("initial_registry", []))
        for a in registry:
            if isinstance(a, dict) and isinstance(a.get("id"), str):
                expected_ids[a["id"]] = a

    drift: list[dict] = list(workflow_drift)
    drift.extend(_scan_agents(root, expected_ids, scanned))
    if include_skills:
        drift.extend(_scan_skills(root, scanned))

    history_digest = _history_digest(root, scanned) if include_history else None

    has_harness = workflow_data is not None or (root / ".agents").is_dir()

    return {
        "has_harness": has_harness,
        "workflow_version": version,
        "pattern": pattern,
        "registry_snapshot": registry,
        "drift": drift,
        "scanned_paths": scanned,
        "history_digest": history_digest,
    }


__all__ = ["run_audit"]

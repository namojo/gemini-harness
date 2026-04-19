"""``run_verify`` — Phase 6 검증 (structure, triggers, dry-run, A/B).

Pure Python for the "schema", "triggers", and "dry_run" checks. Only
"self_critique_ab" needs the Gemini API and is deferred.

Output shape per `_workspace/guide/mcp_tools.md` §3.3.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ._audit import run_audit


VALID_PATTERNS = {
    "pipeline",
    "fan_out_fan_in",
    "expert_pool",
    "producer_reviewer",
    "supervisor",
    "hierarchical",
}

ALLOWED_CHECKS = ("schema", "triggers", "dry_run", "self_critique_ab")


def _check_schema(project_path: Path, report_dir: Path) -> dict:
    """Run audit and convert schema_violation drift into a check result."""
    audit = run_audit(project_path=str(project_path), include_skills=True)
    violations = [d for d in audit["drift"] if d["kind"] == "schema_violation"]
    missing = [d for d in audit["drift"] if d["kind"] == "missing_prompt_file"]
    orphans = [d for d in audit["drift"] if d["kind"] == "orphan_prompt_file"]
    passed = not audit["drift"]
    detail_parts: list[str] = []
    if not audit["has_harness"]:
        passed = False
        detail_parts.append("no harness found (.agents/ or workflow.json missing)")
    if violations:
        detail_parts.append(f"{len(violations)} schema violations")
    if missing:
        detail_parts.append(f"{len(missing)} missing SYSTEM_PROMPT.md files")
    if orphans:
        detail_parts.append(f"{len(orphans)} orphan agent directories")
    if not detail_parts:
        detail_parts.append("all schemas valid")

    report = report_dir / "schema.json"
    report.write_text(
        json.dumps({"audit": audit, "passed": passed}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return {
        "check": "schema",
        "passed": passed,
        "detail": "; ".join(detail_parts),
        "report_path": str(report.resolve()),
    }


_KO = "하네스"
_EN = "harness"
_JA = "ハーネス"


def _check_triggers(project_path: Path, report_dir: Path) -> dict:
    """Verify CLAUDE.md has a harness pointer block, plus gemini-extension.json
    (or GEMINI.md) exists and declares the harness MCP server.

    Gemini CLI uses natural-language directives in ``contextFileName`` (GEMINI.md)
    — there is no locale/trigger field in the extension manifest schema.
    Trigger phrases belong in GEMINI.md prose.
    """
    claude_md = project_path / "CLAUDE.md"
    ext_json = project_path / "gemini-extension.json"
    gemini_md = project_path / "GEMINI.md"

    issues: list[str] = []
    evidence: dict[str, Any] = {}

    if not claude_md.exists():
        issues.append("CLAUDE.md missing")
    else:
        text = claude_md.read_text(encoding="utf-8")
        has_pointer_block = bool(re.search(r"^##\s*하네스[:：]", text, re.MULTILINE)) or (
            "**트리거" in text or "Trigger:" in text
        )
        if not has_pointer_block:
            issues.append("CLAUDE.md lacks harness pointer block (## 하네스: ... / 트리거)")
        evidence["claude_md_chars"] = len(text)
        evidence["has_pointer_block"] = has_pointer_block

    if ext_json.exists():
        try:
            data = json.loads(ext_json.read_text(encoding="utf-8"))
            name = data.get("name")
            if not name:
                issues.append("gemini-extension.json missing 'name'")
            mcp_servers = data.get("mcpServers", {}) or {}
            if not mcp_servers:
                issues.append("gemini-extension.json declares no mcpServers")
            evidence["extension_name"] = name
            evidence["mcp_server_keys"] = sorted(mcp_servers.keys())
            ctx_file = data.get("contextFileName")
            if ctx_file:
                ctx_path = project_path / ctx_file
                if not ctx_path.exists():
                    issues.append(
                        f"contextFileName={ctx_file!r} declared but file missing"
                    )
                evidence["context_file"] = ctx_file
        except json.JSONDecodeError as exc:
            issues.append(f"gemini-extension.json parse error: {exc}")
    else:
        issues.append("gemini-extension.json missing at project root")

    if gemini_md.exists():
        gmd = gemini_md.read_text(encoding="utf-8")
        # Content-based trigger hint check — Gemini CLI derives triggers
        # from GEMINI.md prose, so we look for ko/en/ja phrase presence.
        trigger_hits = {
            "ko": "하네스" in gmd,
            "en": "harness" in gmd.lower(),
            "ja": "ハーネス" in gmd,
        }
        missing = [loc for loc, ok in trigger_hits.items() if not ok]
        if missing:
            issues.append(
                f"GEMINI.md missing trigger phrases for locales: {missing}"
            )
        evidence["gemini_md_trigger_locales"] = [
            loc for loc, ok in trigger_hits.items() if ok
        ]

    passed = not issues
    report = report_dir / "triggers.json"
    report.write_text(
        json.dumps({"issues": issues, "evidence": evidence}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    detail = "all triggers present" if passed else "; ".join(issues)
    return {
        "check": "triggers",
        "passed": passed,
        "detail": detail,
        "report_path": str(report.resolve()),
    }


def _check_dry_run(
    project_path: Path,
    dry_run_input: str,
    report_dir: Path,
) -> dict:
    """Dry-run: verify workflow.json is runnable in structure without invoking LLM.

    - workflow.json parses and lints clean
    - pattern is supported (single or composite)
    - each registry agent has a resolvable SYSTEM_PROMPT.md path
    - Manager._route() does not raise for initial state
    - dry_run_input is non-empty
    """
    issues: list[str] = []
    evidence: dict[str, Any] = {}

    if not dry_run_input or not dry_run_input.strip():
        issues.append("dry_run_input is empty")

    workflow_path = project_path / "workflow.json"
    if not workflow_path.exists():
        issues.append("workflow.json missing")
        report = report_dir / "dry_run.json"
        report.write_text(
            json.dumps({"issues": issues, "evidence": evidence}, indent=2),
            encoding="utf-8",
        )
        return {
            "check": "dry_run",
            "passed": False,
            "detail": "; ".join(issues),
            "report_path": str(report.resolve()),
        }

    try:
        workflow = json.loads(workflow_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _dry_run_result(
            report_dir,
            passed=False,
            issues=[f"workflow.json parse error: {exc}"],
            evidence={},
        )

    pattern = workflow.get("pattern", "")
    sub_patterns = [p.strip() for p in pattern.split("+") if p.strip()]
    for sp in sub_patterns:
        if sp not in VALID_PATTERNS:
            issues.append(f"unsupported sub-pattern: {sp!r}")

    registry = workflow.get("initial_registry", [])
    evidence["pattern"] = pattern
    evidence["agent_count"] = len(registry)

    if len(registry) == 0:
        issues.append("initial_registry is empty")

    for agent in registry:
        if not isinstance(agent, dict):
            issues.append(f"registry item not a dict: {agent!r}")
            continue
        sp_path = agent.get("system_prompt_path", "")
        if not sp_path:
            issues.append(f"agent {agent.get('id','?')} missing system_prompt_path")
            continue
        resolved = project_path / sp_path
        if not resolved.exists():
            issues.append(
                f"agent {agent.get('id','?')} system_prompt_path not found: {sp_path}"
            )

    # Manager route smoke (no Gemini call)
    try:
        from .manager import manager_node
        from .state import initial_state

        state = initial_state(workflow, run_id="dryrun-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
        # Seed a synthetic user input to the entry agent inbox if possible.
        if registry:
            entry_id = registry[0].get("id", "")
            if entry_id:
                state["inbox"] = {entry_id: []}  # type: ignore[typeddict-unknown-key]
        cmd = manager_node(state)
        # cmd may be: Command (has .goto), list[Send] (fan-out), or other.
        if isinstance(cmd, list):
            evidence["manager_first_goto"] = f"fan_out({len(cmd)} sends)"
        else:
            evidence["manager_first_goto"] = str(getattr(cmd, "goto", None) or "<unknown>")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"manager_node raised during dry-run: {type(exc).__name__}: {exc}")

    passed = not issues
    return _dry_run_result(report_dir, passed=passed, issues=issues, evidence=evidence)


def _dry_run_result(
    report_dir: Path,
    *,
    passed: bool,
    issues: list[str],
    evidence: dict,
) -> dict:
    report = report_dir / "dry_run.json"
    report.write_text(
        json.dumps({"issues": issues, "evidence": evidence, "passed": passed}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    detail = "dry-run structural checks passed" if passed else "; ".join(issues)
    return {
        "check": "dry_run",
        "passed": passed,
        "detail": detail,
        "report_path": str(report.resolve()),
    }


def _check_self_critique_ab(
    ab_baseline_run_id: str | None,
    report_dir: Path,
) -> dict:
    """Deferred: needs GEMINI_API_KEY + end-to-end harness.run. Record that."""
    report = report_dir / "self_critique_ab.json"
    import os

    has_key = bool(os.environ.get("GEMINI_API_KEY"))
    payload = {
        "deferred": True,
        "reason": "A/B self-critique requires GEMINI_API_KEY + harness.run impl",
        "has_api_key": has_key,
        "baseline_run_id": ab_baseline_run_id,
    }
    report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "check": "self_critique_ab",
        "passed": False,
        "detail": "deferred: requires GEMINI_API_KEY and harness.run implementation",
        "report_path": str(report.resolve()),
    }


def run_verify(
    *,
    project_path: str,
    checks: list[str] | None = None,
    dry_run_input: str | None = None,
    ab_baseline_run_id: str | None = None,
) -> dict:
    """Verify a project's harness. Returns structured pass/fail per check."""
    root = Path(project_path).resolve()
    requested = list(checks) if checks else ["schema", "triggers", "dry_run"]

    # Validate check names
    for c in requested:
        if c not in ALLOWED_CHECKS:
            raise ValueError(f"unknown check: {c!r}. allowed: {ALLOWED_CHECKS}")

    if "dry_run" in requested and not dry_run_input:
        raise ValueError("dry_run_input required when 'dry_run' in checks")
    if "self_critique_ab" in requested and not ab_baseline_run_id:
        raise ValueError("ab_baseline_run_id required when 'self_critique_ab' in checks")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_dir = root / "_workspace" / "qa" / f"verify-{run_id}"
    report_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for check in requested:
        if check == "schema":
            results.append(_check_schema(root, report_dir))
        elif check == "triggers":
            results.append(_check_triggers(root, report_dir))
        elif check == "dry_run":
            results.append(_check_dry_run(root, dry_run_input or "", report_dir))
        elif check == "self_critique_ab":
            results.append(_check_self_critique_ab(ab_baseline_run_id, report_dir))

    passed = all(r["passed"] for r in results)
    summary = {
        "run_id": run_id,
        "project_path": str(root),
        "passed": passed,
        "results": results,
    }
    summary_path = report_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return {
        "passed": passed,
        "results": results,
        "summary_path": str(summary_path.resolve()),
    }


__all__ = ["run_verify", "ALLOWED_CHECKS"]

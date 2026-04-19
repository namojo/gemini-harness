"""Unit tests for run_audit (Phase 0)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gemini_harness.runtime.harness_runtime import run_audit


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


SYSTEM_PROMPT_GOOD = """---
name: researcher
version: 1.0
model: gemini-3.1-pro-preview
tools: [google-search]
---

# Researcher

## 핵심 역할
공식 채널 조사 전담 에이전트.

## 작업 원칙
1. 1차 출처 우선.
2. 인용·출처를 명시.

## 자가 검증
체크 1, 체크 2.
""" + ("본문 padding. " * 50)


def _valid_workflow(agent_ids: list[str]) -> dict:
    return {
        "version": "1.0",
        "pattern": "fan_out_fan_in",
        "retry_limit": 3,
        "routing_config": {"integrator_id": agent_ids[-1]},
        "initial_registry": [
            {
                "id": aid,
                "name": aid,
                "role": "research agent",
                "system_prompt_path": f".agents/{aid}/SYSTEM_PROMPT.md",
                "skills": [],
                "tools": ["google-search"],
            }
            for aid in agent_ids
        ],
    }


def test_empty_project_has_no_harness(tmp_path: Path):
    result = run_audit(project_path=str(tmp_path))
    assert result["has_harness"] is False
    assert result["workflow_version"] is None
    assert result["pattern"] is None
    assert result["registry_snapshot"] == []


def test_well_formed_harness_no_drift(tmp_path: Path):
    ids = ["researcher-a", "researcher-b", "integrator"]
    _write(tmp_path / "workflow.json", json.dumps(_valid_workflow(ids)))
    for aid in ids:
        _write(tmp_path / ".agents" / aid / "SYSTEM_PROMPT.md", SYSTEM_PROMPT_GOOD)

    result = run_audit(project_path=str(tmp_path))
    assert result["has_harness"] is True
    assert result["workflow_version"] == "1.0"
    assert result["pattern"] == "fan_out_fan_in"
    assert [a["id"] for a in result["registry_snapshot"]] == ids
    assert result["drift"] == []


def test_missing_prompt_file_detected(tmp_path: Path):
    ids = ["alpha", "beta"]
    _write(tmp_path / "workflow.json", json.dumps(_valid_workflow(ids)))
    # Only write alpha's prompt; beta is missing
    _write(tmp_path / ".agents" / "alpha" / "SYSTEM_PROMPT.md", SYSTEM_PROMPT_GOOD)

    result = run_audit(project_path=str(tmp_path))
    kinds = {(d["kind"], d["subject"]) for d in result["drift"]}
    assert ("missing_prompt_file", "beta") in kinds


def test_orphan_prompt_file_detected(tmp_path: Path):
    ids = ["alpha"]
    _write(tmp_path / "workflow.json", json.dumps(_valid_workflow(ids)))
    _write(tmp_path / ".agents" / "alpha" / "SYSTEM_PROMPT.md", SYSTEM_PROMPT_GOOD)
    # Orphan: a directory not in registry
    _write(tmp_path / ".agents" / "ghost" / "SYSTEM_PROMPT.md", SYSTEM_PROMPT_GOOD)

    result = run_audit(project_path=str(tmp_path))
    kinds = {(d["kind"], d["subject"]) for d in result["drift"]}
    assert ("orphan_prompt_file", "ghost") in kinds


def test_schema_violation_reported_for_invalid_workflow(tmp_path: Path):
    bad = {"version": "1.0", "pattern": "not_a_real_pattern", "initial_registry": []}
    _write(tmp_path / "workflow.json", json.dumps(bad))
    result = run_audit(project_path=str(tmp_path))
    assert any(d["kind"] == "schema_violation" for d in result["drift"])


def test_include_history_reads_context_md(tmp_path: Path):
    ids = ["alpha"]
    _write(tmp_path / "workflow.json", json.dumps(_valid_workflow(ids)))
    _write(tmp_path / ".agents" / "alpha" / "SYSTEM_PROMPT.md", SYSTEM_PROMPT_GOOD)
    _write(
        tmp_path / ".gemini" / "context.md",
        "\n".join(
            [
                "## [2026-04-19T10:00:00] run=x node=worker event=worker_complete",
                "## [2026-04-19T10:01:00] run=x node=worker event=agent-created",
                "## [2026-04-19T10:02:00] run=x node=tool_executor event=tool_executor_complete",
            ]
        ),
    )
    result = run_audit(project_path=str(tmp_path), include_history=True)
    assert result["history_digest"] is not None
    digest = result["history_digest"]
    assert digest["present"] is True
    assert digest["events_by_kind"].get("worker_complete", 0) >= 1
    assert digest["events_by_kind"].get("agent-created", 0) >= 1


def test_include_history_false_returns_none(tmp_path: Path):
    result = run_audit(project_path=str(tmp_path), include_history=False)
    assert result["history_digest"] is None


def test_scanned_paths_includes_agents_root(tmp_path: Path):
    result = run_audit(project_path=str(tmp_path))
    assert any(".agents" in p for p in result["scanned_paths"])


def test_malformed_workflow_json_records_drift(tmp_path: Path):
    _write(tmp_path / "workflow.json", "{ this is not json")
    result = run_audit(project_path=str(tmp_path))
    kinds = {d["kind"] for d in result["drift"]}
    assert "schema_violation" in kinds


def test_include_skills_scans_skill_dirs(tmp_path: Path):
    ids = ["alpha"]
    _write(tmp_path / "workflow.json", json.dumps(_valid_workflow(ids)))
    _write(tmp_path / ".agents" / "alpha" / "SYSTEM_PROMPT.md", SYSTEM_PROMPT_GOOD)
    # Create a skill dir missing SKILL.md
    (tmp_path / ".agents" / "skills" / "stub").mkdir(parents=True)
    result = run_audit(project_path=str(tmp_path), include_skills=True)
    kinds = {(d["kind"], d["subject"]) for d in result["drift"]}
    assert ("missing_skill_dir", "stub") in kinds

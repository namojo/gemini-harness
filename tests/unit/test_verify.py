"""Unit tests for run_verify (Phase 6)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gemini_harness.runtime.harness_runtime import run_verify


SYSTEM_PROMPT_GOOD = """---
name: alpha
version: 1.0
model: gemini-3.1-pro-preview
tools: [google-search]
---

# Alpha

## 핵심 역할
테스트 에이전트.

## 작업 원칙
1. 검증.
2. 재현 가능성.

## 자가 검증
결과 shape 확인.
""" + ("본문 padding. " * 60)


CLAUDE_MD_GOOD = """# CLAUDE.md

## 하네스: test-domain

**트리거:** build a harness / 하네스 구성해줘 / ハーネスを構成して
"""

MANIFEST_GOOD = {
    "name": "gemini-harness",
    "version": "0.1.0",
    "contextFileName": "GEMINI.md",
    "mcpServers": {
        "harness": {"command": "gemini-harness-mcp"},
    },
}

GEMINI_MD_GOOD = (
    "# Gemini-Harness\n\n"
    "Team-architecture factory (하네스, ハーネス).\n\n"
    "Use harness.* MCP tools on user requests in ko/en/ja.\n"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _valid_workflow() -> dict:
    return {
        "version": "1.0",
        "pattern": "fan_out_fan_in",
        "retry_limit": 3,
        "routing_config": {"integrator_id": "integrator"},
        "initial_registry": [
            {
                "id": "alpha",
                "name": "alpha",
                "role": "test agent",
                "system_prompt_path": ".agents/alpha/SYSTEM_PROMPT.md",
                "skills": [],
                "tools": ["google-search"],
            },
            {
                "id": "integrator",
                "name": "integrator",
                "role": "integrator agent",
                "system_prompt_path": ".agents/integrator/SYSTEM_PROMPT.md",
                "skills": [],
                "tools": [],
            },
        ],
    }


def _setup_healthy_project(root: Path) -> None:
    _write(root / "workflow.json", json.dumps(_valid_workflow()))
    _write(root / ".agents" / "alpha" / "SYSTEM_PROMPT.md", SYSTEM_PROMPT_GOOD)
    _write(
        root / ".agents" / "integrator" / "SYSTEM_PROMPT.md",
        SYSTEM_PROMPT_GOOD.replace("name: alpha", "name: integrator"),
    )
    _write(root / "CLAUDE.md", CLAUDE_MD_GOOD)
    _write(root / "gemini-extension.json", json.dumps(MANIFEST_GOOD))
    _write(root / "GEMINI.md", GEMINI_MD_GOOD)


def test_healthy_project_all_checks_pass(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    result = run_verify(
        project_path=str(tmp_path),
        checks=["schema", "triggers", "dry_run"],
        dry_run_input="복잡한 Next.js 프로젝트 아키텍처 짜줘",
    )
    assert result["passed"] is True
    assert len(result["results"]) == 3
    for r in result["results"]:
        assert r["passed"] is True, f"{r['check']} failed: {r['detail']}"


def test_schema_check_fails_on_bad_workflow(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    # Break the workflow
    _write(
        tmp_path / "workflow.json",
        json.dumps({"version": "1.0", "pattern": "bogus", "initial_registry": []}),
    )
    result = run_verify(project_path=str(tmp_path), checks=["schema"])
    assert result["passed"] is False
    assert result["results"][0]["passed"] is False


def test_triggers_check_fails_without_claude_md(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    (tmp_path / "CLAUDE.md").unlink()
    result = run_verify(project_path=str(tmp_path), checks=["triggers"])
    assert result["passed"] is False


def test_triggers_check_fails_when_gemini_md_missing_ja_phrase(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    # GEMINI.md without Japanese phrase
    _write(
        tmp_path / "GEMINI.md",
        "# Harness\n\n하네스 한국어와 english but no ja locale phrase.\n",
    )
    result = run_verify(project_path=str(tmp_path), checks=["triggers"])
    assert result["passed"] is False
    assert "ja" in result["results"][0]["detail"]


def test_dry_run_requires_input(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    with pytest.raises(ValueError, match="dry_run_input required"):
        run_verify(project_path=str(tmp_path), checks=["dry_run"])


def test_dry_run_fails_for_missing_system_prompt(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    (tmp_path / ".agents" / "integrator" / "SYSTEM_PROMPT.md").unlink()
    result = run_verify(
        project_path=str(tmp_path),
        checks=["dry_run"],
        dry_run_input="x",
    )
    assert result["passed"] is False
    assert "system_prompt_path" in result["results"][0]["detail"]


def test_unknown_check_name_rejected(tmp_path: Path):
    with pytest.raises(ValueError, match="unknown check"):
        run_verify(project_path=str(tmp_path), checks=["made_up"])


def test_self_critique_ab_is_deferred(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    result = run_verify(
        project_path=str(tmp_path),
        checks=["self_critique_ab"],
        ab_baseline_run_id="baseline-001",
    )
    assert result["passed"] is False
    assert "deferred" in result["results"][0]["detail"]


def test_self_critique_ab_requires_baseline(tmp_path: Path):
    with pytest.raises(ValueError, match="ab_baseline_run_id"):
        run_verify(project_path=str(tmp_path), checks=["self_critique_ab"])


def test_summary_file_written(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    result = run_verify(
        project_path=str(tmp_path),
        checks=["schema", "triggers", "dry_run"],
        dry_run_input="ok",
    )
    summary_path = Path(result["summary_path"])
    assert summary_path.exists()
    data = json.loads(summary_path.read_text(encoding="utf-8"))
    assert data["passed"] == result["passed"]
    assert len(data["results"]) == 3


def test_default_checks_when_none_specified(tmp_path: Path):
    _setup_healthy_project(tmp_path)
    # Without dry_run_input, default checks fail because dry_run requires it
    with pytest.raises(ValueError, match="dry_run_input required"):
        run_verify(project_path=str(tmp_path))

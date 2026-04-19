"""Unit tests for run_evolve with mocked Gemini."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gemini_harness.runtime.harness_runtime import EvolveError, run_evolve


class MockGemini:
    def __init__(self, payloads: list[str]):
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        if not self._payloads:
            raise RuntimeError("MockGemini exhausted")
        return SimpleNamespace(
            text=self._payloads.pop(0),
            usage=SimpleNamespace(prompt_token_count=50, candidates_token_count=30),
            tool_calls=None,
            finish_reason="STOP",
            blocked_reason=None,
            raw={},
        )


LONG_ROLE = "긴 역할 설명으로 최소 길이 제한을 충족한다."


def _bootstrap_harness(root: Path) -> None:
    # Minimal workflow
    wf = {
        "version": "1.0",
        "pattern": "fan_out_fan_in",
        "retry_limit": 3,
        "routing_config": {"integrator_id": "integrator"},
        "initial_registry": [
            {
                "id": "writer",
                "name": "writer",
                "role": LONG_ROLE,
                "system_prompt_path": ".agents/writer/SYSTEM_PROMPT.md",
                "skills": [],
                "tools": [],
            },
            {
                "id": "integrator",
                "name": "integrator",
                "role": LONG_ROLE,
                "system_prompt_path": ".agents/integrator/SYSTEM_PROMPT.md",
                "skills": [],
                "tools": [],
            },
        ],
    }
    (root / "workflow.json").write_text(json.dumps(wf), encoding="utf-8")

    body = (
        "## 핵심 역할\n짧은 본문.\n\n"
        "## 작업 원칙\n1. 원칙.\n\n"
        "## 입력/출력 프로토콜\ninbox / artifacts.\n\n"
        "## 에러 핸들링\nManager 복귀.\n\n"
        "## 자가 검증\nok.\n"
    ) + ("padding. " * 80)
    for aid in ("writer", "integrator"):
        p = root / ".agents" / aid / "SYSTEM_PROMPT.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"---\nname: {aid}\nversion: \"1.0\"\nmodel: gemini-3.1-pro-preview\ntools: []\n---\n\n# {aid}\n\n{body}\n",
            encoding="utf-8",
        )

    (root / "CLAUDE.md").write_text(
        "# CLAUDE.md\n\n"
        "**변경 이력:**\n"
        "| 날짜 | 변경 내용 | 대상 | 사유 |\n"
        "|------|----------|------|------|\n"
        "| 2026-04-18 | 초기 생성 | 전체 | - |\n",
        encoding="utf-8",
    )


def test_evolve_requires_workflow(tmp_path: Path):
    with pytest.raises(EvolveError, match="HARNESS_NOT_INITIALIZED"):
        run_evolve(
            project_path=str(tmp_path),
            feedback="please improve the writer",
            gemini_client=MockGemini([]),
        )


def test_evolve_rejects_short_feedback(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    with pytest.raises(EvolveError, match="at least 10"):
        run_evolve(
            project_path=str(tmp_path),
            feedback="tiny",
            gemini_client=MockGemini([]),
        )


def test_evolve_rejects_unknown_scope(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    with pytest.raises(EvolveError, match="INVALID_INPUT"):
        run_evolve(
            project_path=str(tmp_path),
            feedback="tweak the ghost agent",
            scope=[{"kind": "agent", "id": "ghost"}],
            gemini_client=MockGemini([]),
        )


def test_evolve_applies_agent_update(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    new_body = (
        "## 핵심 역할\n개선된 역할 — 더 엄격한 검토.\n\n"
        "## 작업 원칙\n1. 엄격.\n2. 근거.\n\n"
        "## 입력/출력 프로토콜\ninbox / artifacts.\n\n"
        "## 에러 핸들링\nManager 복귀.\n\n"
        "## 자가 검증\n세 가지 체크.\n"
    ) + ("padding. " * 80)
    design = {
        "summary": "writer 역할을 더 엄격하게 업데이트.",
        "changes": [
            {
                "kind": "agent_update",
                "target": "writer",
                "old_excerpt": "짧은 본문",
                "new_content": new_body,
                "rationale": "사용자 피드백: 너무 느슨함.",
            }
        ],
    }
    client = MockGemini([json.dumps(design)])
    result = run_evolve(
        project_path=str(tmp_path),
        feedback="writer 에이전트가 너무 느슨합니다. 엄격한 검토를 추가하세요.",
        gemini_client=client,
    )
    assert result["applied"] is True
    assert len(result["changes"]) == 1
    assert "개선된 역할" in (
        (tmp_path / ".agents" / "writer" / "SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    )
    assert result["context_log_appended"] is True
    # CLAUDE.md change log updated
    ch = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "harness.evolve" in ch


def test_evolve_dry_run_does_not_write(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    original = (tmp_path / ".agents" / "writer" / "SYSTEM_PROMPT.md").read_text(encoding="utf-8")
    new_body = (
        "## 핵심 역할\n수정된 역할입니다.\n\n"
        "## 작업 원칙\n1. 검증.\n\n"
        "## 입력/출력 프로토콜\nI/O.\n\n"
        "## 에러 핸들링\nok.\n\n"
        "## 자가 검증\n자가 체크.\n"
    ) + ("pad. " * 80)
    design = {
        "summary": "dry run 테스트",
        "changes": [
            {
                "kind": "agent_update",
                "target": "writer",
                "old_excerpt": "",
                "new_content": new_body,
                "rationale": "...",
            }
        ],
    }
    result = run_evolve(
        project_path=str(tmp_path),
        feedback="변경을 제안만 해주세요.",
        dry_run=True,
        gemini_client=MockGemini([json.dumps(design)]),
    )
    assert result["applied"] is False
    assert result["changes"]  # diff proposed
    # File unchanged
    assert (tmp_path / ".agents" / "writer" / "SYSTEM_PROMPT.md").read_text(encoding="utf-8") == original


def test_evolve_routing_config_change(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    design = {
        "summary": "retry_limit 5 로 상향",
        "changes": [
            {
                "kind": "workflow_field",
                "target": "retry_limit",
                "old_excerpt": "3",
                "new_content": 5,
                "rationale": "사용자 요청.",
            }
        ],
    }
    result = run_evolve(
        project_path=str(tmp_path),
        feedback="retry_limit을 5로 올려주세요 — 더 끈질기게.",
        gemini_client=MockGemini([json.dumps(design)]),
    )
    assert result["applied"] is True
    wf = json.loads((tmp_path / "workflow.json").read_text(encoding="utf-8"))
    assert wf["retry_limit"] == 5


def test_evolve_rejects_unknown_kind(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    design = {
        "summary": "invalid change",
        "changes": [{"kind": "nuke_everything", "target": "*", "new_content": ""}],
    }
    result = run_evolve(
        project_path=str(tmp_path),
        feedback="무언가 잘못된 변경 요청.",
        gemini_client=MockGemini([json.dumps(design)]),
    )
    assert result["applied"] is False
    assert result["errors"]


def test_evolve_retries_on_parse_error(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    good = {
        "summary": "retry_limit 상향",
        "changes": [
            {
                "kind": "workflow_field",
                "target": "retry_limit",
                "old_excerpt": "3",
                "new_content": 4,
                "rationale": "-",
            }
        ],
    }
    client = MockGemini(["this is not json", json.dumps(good)])
    result = run_evolve(
        project_path=str(tmp_path),
        feedback="retry_limit 4로 하되 JSON이 깨지는 시나리오 리트라이.",
        gemini_client=client,
    )
    assert result["applied"] is True
    assert len(client.calls) == 2


def test_evolve_gives_up_after_retries(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    client = MockGemini(["garbage"] * 5)
    with pytest.raises(EvolveError, match="failed after"):
        run_evolve(
            project_path=str(tmp_path),
            feedback="절대 파싱 안되는 응답만 오는 시나리오.",
            gemini_client=client,
        )


def test_evolve_records_metrics(tmp_path: Path):
    _bootstrap_harness(tmp_path)
    design = {
        "summary": "retry_limit 상향",
        "changes": [
            {
                "kind": "workflow_field",
                "target": "retry_limit",
                "old_excerpt": "3",
                "new_content": 6,
                "rationale": "-",
            }
        ],
    }
    result = run_evolve(
        project_path=str(tmp_path),
        feedback="retry_limit 6으로 조정.",
        gemini_client=MockGemini([json.dumps(design)]),
    )
    assert result["metrics"]["calls"] == 1
    assert result["metrics"]["input_tokens"] == 50
    assert result["metrics"]["output_tokens"] == 30

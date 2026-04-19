"""Unit tests for run_build with mocked Gemini client."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from gemini_harness.runtime.harness_runtime import BuildError, run_build


class MockGeminiClient:
    """Returns a canned JSON payload (or sequence of payloads for retry tests)."""

    def __init__(self, payloads: list[str]):
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    def call(self, **kwargs):
        self.calls.append(kwargs)
        if not self._payloads:
            raise RuntimeError("MockGeminiClient exhausted")
        text = self._payloads.pop(0)
        return SimpleNamespace(
            text=text,
            usage_metadata={"input_tokens": 100, "output_tokens": 50},
            tool_calls=None,
            finish_reason="STOP",
            blocked_reason=None,
            raw={},
        )


def _valid_design(pattern: str = "fan_out_fan_in") -> dict:
    # Body padded to meet linter's min-length for SYSTEM_PROMPT body.
    long_body = (
        "## 핵심 역할\n테스트 에이전트.\n\n"
        "## 작업 원칙\n1. 검증.\n2. 재현.\n\n"
        "## 입력/출력 프로토콜\n입력: inbox. 출력: artifacts/.\n\n"
        "## 에러 핸들링\n실패 시 Manager로 복귀.\n\n"
        "## 자가 검증\n결과 shape 확인.\n"
    ) + ("부연 설명. " * 80)

    skill_body = (
        "# Research Skill\n\n## 목적\n도메인 조사.\n\n"
        "## 사용\nWorker에서 web-research 스킬 호출 시 이 파일을 참조한다.\n\n"
        "## 실행\n`python scripts/main.py --query Q`\n\n"
        "## 검증\nassertion: output_json.sources[0].url starts with https.\n"
    ) + ("추가 설명. " * 30)

    return {
        "pattern": pattern,
        "rationale": "도메인이 다관점 조사를 요구하므로 fan_out_fan_in 선택.",
        "routing_config": {"integrator_id": "integrator"},
        "agents": [
            {
                "id": "researcher-a",
                "name": "researcher-a",
                "role": "공식 채널 조사 담당",
                "skills": ["web-research"],
                "tools": ["google-search"],
                "system_prompt_body": long_body,
            },
            {
                "id": "researcher-b",
                "name": "researcher-b",
                "role": "커뮤니티 반응 조사 담당",
                "skills": ["web-research"],
                "tools": ["google-search"],
                "system_prompt_body": long_body,
            },
            {
                "id": "integrator",
                "name": "integrator",
                "role": "세 조사 결과를 종합 보고",
                "skills": [],
                "tools": [],
                "system_prompt_body": long_body,
            },
        ],
        "skills": [
            {
                "name": "web-research",
                "description": "웹 기반 도메인 조사 스킬. google-search로 1차 출처 수집 후 구조화. 리서치 에이전트가 web-research skill을 선언할 때 이 파일이 로드된다.",
                "runtime": "python",
                "entry": "scripts/main.py",
                "body": skill_body,
            }
        ],
    }


def test_happy_path_writes_workflow_and_agents(tmp_path: Path):
    design = _valid_design()
    client = MockGeminiClient([json.dumps(design)])

    result = run_build(
        project_path=str(tmp_path),
        domain_description="다관점 리서치 하네스를 설계해줘 — 공식·커뮤니티·종합 3인 팀.",
        gemini_client=client,
    )

    assert result["pattern"] == "fan_out_fan_in"
    assert len(result["final_registry"]) == 3
    assert Path(result["workflow_path"]).exists()

    # workflow.json content matches
    wf = json.loads(Path(result["workflow_path"]).read_text(encoding="utf-8"))
    assert wf["pattern"] == "fan_out_fan_in"
    assert [a["id"] for a in wf["initial_registry"]] == [
        "researcher-a",
        "researcher-b",
        "integrator",
    ]
    # Each SYSTEM_PROMPT.md written
    for aid in ("researcher-a", "researcher-b", "integrator"):
        assert (tmp_path / ".agents" / aid / "SYSTEM_PROMPT.md").exists()
    # Skill
    assert (tmp_path / ".agents" / "skills" / "web-research" / "SKILL.md").exists()
    assert (
        tmp_path / ".agents" / "skills" / "web-research" / "scripts" / "main.py"
    ).exists()
    # CLAUDE.md pointer
    assert (tmp_path / "CLAUDE.md").exists()
    assert client.calls  # at least one Gemini call made


def test_retry_on_invalid_json(tmp_path: Path):
    design = _valid_design()
    client = MockGeminiClient([
        "this is not json",
        json.dumps(design),
    ])

    result = run_build(
        project_path=str(tmp_path),
        domain_description="도메인 리서치 — 공식·커뮤니티·종합 3인 팀 구성.",
        gemini_client=client,
    )
    assert len(client.calls) == 2  # one fail, one success
    assert Path(result["workflow_path"]).exists()


def test_retry_on_lint_failure(tmp_path: Path):
    bad_design = _valid_design()
    # Break agent id to fail linter
    bad_design["agents"][0]["id"] = "Bad-ID-Starts-Upper"
    good_design = _valid_design()
    client = MockGeminiClient([
        json.dumps(bad_design),
        json.dumps(good_design),
    ])

    result = run_build(
        project_path=str(tmp_path),
        domain_description="유효하지 않은 에이전트 ID 시나리오 리트라이 테스트.",
        gemini_client=client,
    )
    assert len(client.calls) == 2
    assert result["pattern"] == "fan_out_fan_in"


def test_give_up_after_max_retries(tmp_path: Path):
    broken = _valid_design()
    broken["agents"][0]["id"] = "Invalid"  # will never lint clean
    client = MockGeminiClient([json.dumps(broken)] * 5)

    with pytest.raises(BuildError) as excinfo:
        run_build(
            project_path=str(tmp_path),
            domain_description="절대 lint 통과하지 못하는 설계 반복 시나리오.",
            gemini_client=client,
        )
    assert "architect failed" in str(excinfo.value)


def test_short_domain_description_rejected(tmp_path: Path):
    client = MockGeminiClient([json.dumps(_valid_design())])
    with pytest.raises(BuildError, match="at least 20 characters"):
        run_build(
            project_path=str(tmp_path),
            domain_description="short",
            gemini_client=client,
        )


def test_existing_harness_blocks_without_force(tmp_path: Path):
    (tmp_path / "workflow.json").write_text("{}", encoding="utf-8")
    client = MockGeminiClient([json.dumps(_valid_design())])
    with pytest.raises(BuildError, match="Existing harness"):
        run_build(
            project_path=str(tmp_path),
            domain_description="기존 하네스 있음. force 없이 실패해야 함.",
            gemini_client=client,
        )


def test_force_overwrites_and_backs_up(tmp_path: Path):
    (tmp_path / "workflow.json").write_text('{"prev": true}', encoding="utf-8")
    client = MockGeminiClient([json.dumps(_valid_design())])
    result = run_build(
        project_path=str(tmp_path),
        domain_description="force 모드로 기존 덮어쓰기. 이전 버전은 _workspace에 백업.",
        gemini_client=client,
        force=True,
    )
    assert result["warnings"]
    assert any("backed up" in w for w in result["warnings"])
    # Backup exists
    backup_dirs = list((tmp_path / "_workspace").glob("prev-*"))
    assert backup_dirs and any((d / "workflow.json").exists() for d in backup_dirs)


def test_tool_executor_routing_is_passed_through(tmp_path: Path):
    design = _valid_design()
    client = MockGeminiClient([json.dumps(design)])
    result = run_build(
        project_path=str(tmp_path),
        domain_description="tool_executor 블록이 routing_config에 주입되어야 함.",
        tool_executor={"max_tool_iterations": 3, "allowed_tools": ["google-search"]},
        gemini_client=client,
    )
    wf = json.loads(Path(result["workflow_path"]).read_text(encoding="utf-8"))
    te = wf["routing_config"].get("tool_executor")
    assert te == {"max_tool_iterations": 3, "allowed_tools": ["google-search"]}


def test_metrics_collected(tmp_path: Path):
    client = MockGeminiClient([json.dumps(_valid_design())])
    result = run_build(
        project_path=str(tmp_path),
        domain_description="메트릭 수집 검증 — input_tokens, output_tokens, wall_clock_ms.",
        gemini_client=client,
    )
    m = result["metrics"]
    assert m["calls"] == 1
    assert m["input_tokens"] == 100
    assert m["output_tokens"] == 50
    assert m["wall_clock_ms"] >= 0

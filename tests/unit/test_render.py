"""Render tests — verify templates produce lint-clean output with sample inputs."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from gemini_harness.meta import (
    lint_agent,
    lint_skill,
    lint_workflow,
    render_skill,
    render_system_prompt,
    render_workflow,
)


def _parse_md(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, flags=re.DOTALL)
    assert m, f"No frontmatter found in rendered output:\n{text[:200]}"
    fm = yaml.safe_load(m.group(1)) or {}
    body = m.group(2)
    for k in ("version",):
        if k in fm and not isinstance(fm[k], str):
            fm[k] = str(fm[k])
    return fm, body


def test_render_system_prompt_passes_linter():
    rendered = render_system_prompt(
        agent_name="writer",
        role_title="Writer",
        core_role="주어진 주제로 초안을 작성하는 생산자 에이전트. 검토자 피드백을 반영한다.",
        principles="1. 사실 우선. 근거: 통합 단계에서 출처 추적이 필요.\n2. 명확한 톤 유지.",
        self_critique_items="1. 사실 정확성 검증.\n2. 톤 일관성 확인.\n3. 출처 표기 확인.",
        rationale="writer/editor 분리는 producer_reviewer 패턴 요구사항.",
        tools=["file-manager"],
    )
    fm, body = _parse_md(rendered)
    result = lint_agent(fm, body)
    assert result.passed, [f.message for f in result.failures]
    assert fm["name"] == "writer"
    assert fm["model"].startswith("gemini-")


def test_render_skill_passes_linter(tmp_path: Path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "main.py").write_text("print('ok')\n", encoding="utf-8")

    rendered = render_skill(
        skill_name="web-research",
        skill_title="Web Research",
        description=(
            "주어진 주제에 대해 공식 채널 웹 검색과 크롤링·요약을 수행한다. "
            "'공식 자료 조사', '리서치' 상황에서 researcher-* 에이전트가 호출되면 사용."
        ),
        runtime="python",
        entry="scripts/main.py",
        purpose="공식 1차 자료 수집.",
        callers="researcher-a, researcher-b",
        execution="`python scripts/main.py --topic ...`",
        verification="출력 파일이 최소 3개 불릿 포함 여부 self-check.",
    )
    fm, body = _parse_md(rendered)
    result = lint_skill(fm, body, entry_path=fm["entry"], read_root=str(tmp_path))
    assert result.passed, [f.message for f in result.failures]


def test_render_workflow_parses_and_passes_linter():
    registry = [
        {
            "id": "writer",
            "name": "writer",
            "role": "주어진 주제로 초안을 작성한다.",
            "system_prompt_path": ".agents/writer/SYSTEM_PROMPT.md",
        },
        {
            "id": "editor",
            "name": "editor",
            "role": "초안을 검토하여 pass/수정 요청을 반환한다.",
            "system_prompt_path": ".agents/editor/SYSTEM_PROMPT.md",
        },
    ]
    routing = {"producer_id": "writer", "reviewer_id": "editor"}
    rendered = render_workflow(
        pattern="producer_reviewer",
        initial_registry=registry,
        routing_config=routing,
        retry_limit=3,
    )
    data = json.loads(rendered)
    assert data["version"] == "1.0"
    assert data["pattern"] == "producer_reviewer"
    result = lint_workflow(data)
    assert result.passed, [f.message for f in result.failures]

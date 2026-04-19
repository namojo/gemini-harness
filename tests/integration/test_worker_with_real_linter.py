"""Integration: Worker wired to the real ``gemini_harness.meta.linter``.

Verifies that a create_agents response containing a valid SYSTEM_PROMPT body
passes the real linter and lands in the registry, and that a response missing
required sections is rejected.
"""
from __future__ import annotations

import json
from pathlib import Path

from gemini_harness.meta import linter as meta_linter
from gemini_harness.runtime.contracts import GeminiResponseLike
from gemini_harness.runtime.worker import WorkerDeps, make_worker_node


def _seed_parent(tmp_path: Path) -> dict:
    target = tmp_path / ".agents" / "parent" / "SYSTEM_PROMPT.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# parent\n")
    return {
        "id": "parent",
        "name": "parent",
        "role": "parent that spawns children during the test",
        "system_prompt_path": ".agents/parent/SYSTEM_PROMPT.md",
    }


_GOOD_BODY = """---
name: child1
version: 1.0
model: gemini-3.1-pro-preview
tools: []
---

# child1

## 핵심 역할

Generate small test payloads and verify invariants when the parent requests it.
Keeps its scope narrow — the parent handles orchestration. The role statement
is long enough to satisfy ``agent.role_not_empty``.

## 입력/출력 프로토콜

Receives inbox messages from parent. Returns JSON with ``event_summary`` and
optional ``artifacts``. No tool calls.

## 자가 검증

1. Does the output JSON parse?
2. Does every artifact path stay inside the sandbox?
3. Are there no forbidden shell patterns in the body?

The checklist above is deliberately short so the reader can run through it in
under a minute; longer checklists tend to be skipped.
"""


_BAD_BODY = """---
name: evil
version: 1.0
model: gemini-3.1-pro-preview
tools: []
---

# evil

Nothing useful here, and no required sections. The body is intentionally long
enough to clear the Worker's minimum-length pre-check so the request reaches
the real linter, which must still reject it for missing the mandatory
Korean-named sections (핵심 역할, 작업 원칙, 입력/출력 프로토콜, 에러 핸들링,
자가 검증). Padding follows so the byte count exceeds the 200-char floor:
""" + ("filler. " * 40)


def _mkstate(agent_id, registry):
    return {
        "workflow": {"pattern": "pipeline", "routing_config": {}},
        "registry": registry,
        "inbox": {},
        "history": [],
        "artifacts": {},
        "retry_count": 0,
        "retry_limit": 3,
        "test_passed": False,
        "errors": [],
        "run_id": "run-wire",
        "pending_tool_calls": [],
        "tool_results": {},
        "tool_iterations": 0,
        "current_target": agent_id,
    }


def _stub_gemini(text):
    def call(**_kwargs):
        return GeminiResponseLike(text=text)
    return call


def test_worker_accepts_create_agent_passing_real_linter(tmp_path: Path):
    parent = _seed_parent(tmp_path)
    response_text = json.dumps(
        {
            "create_agents": [
                {
                    "id": "child1",
                    "name": "child1",
                    "role": "parent-created child, role long enough to pass",
                    "system_prompt_path": ".agents/child1/SYSTEM_PROMPT.md",
                    "system_prompt_body": _GOOD_BODY,
                }
            ],
            "event_summary": "spawned child1",
        }
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response_text),
        linter=meta_linter,
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    out = worker(_mkstate("parent", [parent]))

    assert "registry" in out, out.get("errors")
    assert out["registry"][0]["id"] == "child1"
    child_path = tmp_path / ".agents" / "child1" / "SYSTEM_PROMPT.md"
    assert child_path.exists()


def test_worker_rejects_create_agent_failing_real_linter(tmp_path: Path):
    parent = _seed_parent(tmp_path)
    response_text = json.dumps(
        {
            "create_agents": [
                {
                    "id": "evil",
                    "name": "evil",
                    "role": "a child whose body is missing the required sections",
                    "system_prompt_path": ".agents/evil/SYSTEM_PROMPT.md",
                    "system_prompt_body": _BAD_BODY,
                }
            ],
            "event_summary": "tried to spawn evil",
        }
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response_text),
        linter=meta_linter,
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    out = worker(_mkstate("parent", [parent]))

    assert "registry" not in out
    lint_errs = [e for e in out.get("errors", []) if e["kind"] == "create_agent_lint_failed"]
    assert lint_errs, f"expected lint_failed error, got {out.get('errors')}"
    check_names = {f["check_name"] for f in lint_errs[0]["failures"]}
    # ``## 핵심 역할`` is present in _BAD_BODY, but ``## 자가 검증`` is not.
    assert "sp.has_self_critique_section" in check_names
    assert not (tmp_path / ".agents" / "evil" / "SYSTEM_PROMPT.md").exists()

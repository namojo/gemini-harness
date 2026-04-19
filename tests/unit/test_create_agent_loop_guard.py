"""Regression tests for the create_agent infinite-loop bug (v0.1.0 field report).

Symptom observed by a user running v0.1.0 in Gemini CLI:
    "PM kept retrying team formation, agents stuck punching the time-clock"

Root causes (both fixed in v0.1.2):
  1. Worker silently fabricated a placeholder ``system_prompt_body`` when
     Gemini omitted one → always failed the linter → infinite retry.
  2. The prompt composer did not surface ``state.errors`` back to the agent,
     so the LLM had no feedback and kept producing the same malformed output.

Guard added: if the same agent produces `create_agent_*` errors on 3
consecutive worker turns without adding any agent, Manager aborts with a
`create_agent_loop_aborted` error.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gemini_harness.runtime.contracts import GeminiResponseLike, LintResult
from gemini_harness.runtime.manager import manager_node
from gemini_harness.runtime.state import HarnessState, initial_state
from gemini_harness.runtime.worker import WorkerDeps, make_worker_node


class _PassLinter:
    def lint_agent(self, fm, body, meta=None):
        return LintResult(passed=True, failures=[])

    def lint_skill(self, fm, body, entry, root):
        return LintResult(passed=True, failures=[])

    def lint_workflow(self, wf):
        return LintResult(passed=True, failures=[])


def _stub_gemini(response: GeminiResponseLike):
    def _call(**kwargs):
        return response

    return _call


def _seed_agent(root: Path, agent_id: str) -> dict:
    sp = root / ".agents" / agent_id / "SYSTEM_PROMPT.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(f"# {agent_id}\nrole: test\n", encoding="utf-8")
    return {
        "id": agent_id,
        "name": agent_id,
        "role": "test agent long enough to satisfy role_not_empty",
        "system_prompt_path": f".agents/{agent_id}/SYSTEM_PROMPT.md",
    }


def _mkstate(current: str, registry: list[dict]) -> dict:
    return {
        "workflow": {"pattern": "pipeline", "routing_config": {}},
        "registry": registry,
        "inbox": {},
        "history": [],
        "artifacts": {},
        "tool_results": {},
        "pending_tool_calls": [],
        "tool_iterations": 0,
        "retry_count": 0,
        "retry_limit": 3,
        "test_passed": False,
        "errors": [],
        "run_id": "test-run",
        "current_target": current,
        "phase": None,
    }


def test_missing_system_prompt_body_is_rejected_with_specific_error(tmp_path: Path):
    """Worker must NOT fabricate a placeholder body. It must emit a
    create_agent_missing_body error so the LLM learns what to fix.
    """
    parent = _seed_agent(tmp_path, "parent")
    response = GeminiResponseLike(
        text=json.dumps(
            {
                "create_agents": [
                    {
                        "id": "child-no-body",
                        "name": "child-no-body",
                        "role": "role long enough but body missing",
                        "system_prompt_path": ".agents/child-no-body/SYSTEM_PROMPT.md",
                        # system_prompt_body intentionally absent
                    }
                ],
                "event_summary": "attempted spawn",
            }
        ),
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response), linter=_PassLinter(), repo_root=str(tmp_path)
    )
    worker = make_worker_node(deps)
    out = worker(_mkstate("parent", [parent]))
    assert "registry" not in out
    errs = out.get("errors") or []
    kinds = {e.get("kind") for e in errs if isinstance(e, dict)}
    assert "create_agent_missing_body" in kinds, f"kinds={kinds}"
    # And the file must NOT have been created
    assert not (tmp_path / ".agents" / "child-no-body" / "SYSTEM_PROMPT.md").exists()


def test_previous_errors_are_surfaced_in_next_prompt(tmp_path: Path):
    """When state.errors contains create_agent failures from a previous turn,
    the composed prompt must include them so the LLM can correct course.
    """
    parent = _seed_agent(tmp_path, "parent")
    captured_prompts: list[str] = []

    def _capture(**kwargs):
        captured_prompts.append(str(kwargs.get("prompt") or ""))
        return GeminiResponseLike(text=json.dumps({"event_summary": "ack"}))

    deps = WorkerDeps(gemini=_capture, linter=_PassLinter(), repo_root=str(tmp_path))
    worker = make_worker_node(deps)

    state = _mkstate("parent", [parent])
    state["errors"] = [
        {
            "kind": "create_agent_missing_body",
            "id": "child-x",
            "detail": "system_prompt_body is missing or too short",
        }
    ]
    worker(state)

    prompt = captured_prompts[0]
    assert "previous_errors" in prompt, prompt
    assert "create_agent_missing_body" in prompt
    assert "child-x" in prompt


def test_manager_aborts_after_three_consecutive_create_agent_failures(tmp_path: Path):
    """Loop guard: after 3 consecutive worker_complete events for the same
    agent with create_agent errors and 0 agents_added, Manager must go to END
    with create_agent_loop_aborted instead of routing back to Worker again.
    """
    parent = _seed_agent(tmp_path, "parent")
    state = _mkstate("parent", [parent])

    # Simulate 3 consecutive worker turns by parent that all failed.
    state["history"] = [
        {
            "ts": "2026-04-19T00:00:00Z",
            "agent": "parent",
            "node": "worker",
            "kind": "worker_complete",
            "summary": "failed to spawn",
            "create_agent_errors": 2,
            "agents_added": 0,
        }
        for _ in range(3)
    ]

    cmd = manager_node(state)
    assert cmd.goto == "__end__" or str(cmd.goto).upper() == "END", cmd
    update = cmd.update or {}
    errs = update.get("errors", [])
    assert any(
        e.get("kind") == "create_agent_loop_aborted" for e in errs
    ), f"expected abort, got {errs}"


def test_manager_does_not_abort_if_agent_eventually_succeeded(tmp_path: Path):
    """Three failed turns, then one successful turn — streak resets. Manager
    should continue routing, not abort on historical failures."""
    parent = _seed_agent(tmp_path, "parent")
    state = _mkstate("parent", [parent])
    state["history"] = [
        # Older failures
        {"node": "worker", "kind": "worker_complete", "agent": "parent",
         "create_agent_errors": 1, "agents_added": 0},
        {"node": "worker", "kind": "worker_complete", "agent": "parent",
         "create_agent_errors": 1, "agents_added": 0},
        # Then success — streak must break
        {"node": "worker", "kind": "worker_complete", "agent": "parent",
         "create_agent_errors": 0, "agents_added": 2},
    ]
    cmd = manager_node(state)
    # Manager should NOT abort. It goes to worker (pipeline routes to parent
    # again since its status stays idle in this stub state) or END if nothing
    # left — key assertion is that the error list does NOT contain
    # create_agent_loop_aborted.
    update = cmd.update or {}
    errs = update.get("errors", [])
    assert not any(e.get("kind") == "create_agent_loop_aborted" for e in errs)


def test_manager_does_not_abort_on_different_agents(tmp_path: Path):
    """If failures belong to different agents, streak doesn't apply."""
    a = _seed_agent(tmp_path, "agent-a")
    b = _seed_agent(tmp_path, "agent-b")
    state = _mkstate("agent-a", [a, b])
    state["history"] = [
        {"node": "worker", "kind": "worker_complete", "agent": "agent-a",
         "create_agent_errors": 1, "agents_added": 0},
        {"node": "worker", "kind": "worker_complete", "agent": "agent-b",
         "create_agent_errors": 1, "agents_added": 0},
        {"node": "worker", "kind": "worker_complete", "agent": "agent-a",
         "create_agent_errors": 1, "agents_added": 0},
    ]
    cmd = manager_node(state)
    update = cmd.update or {}
    errs = update.get("errors", [])
    assert not any(e.get("kind") == "create_agent_loop_aborted" for e in errs)

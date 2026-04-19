"""Worker tests with mock GeminiClient covering create_agents / send_messages /
artifacts / tool_calls paths."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gemini_harness.runtime.contracts import (
    GeminiResponseLike,
    LintFailure,
    LintResult,
    ToolCallDecl,
    UsageMetadata,
)
from gemini_harness.runtime.sandbox import SandboxViolation, resolve_safe
from gemini_harness.runtime.worker import WorkerDeps, make_worker_node


class _PassLinter:
    def lint_agent(self, frontmatter, body, agent_meta=None):
        return LintResult(passed=True, failures=[])

    def lint_skill(self, frontmatter, body, entry_path, read_root):
        return LintResult(passed=True, failures=[])

    def lint_workflow(self, workflow):
        return LintResult(passed=True, failures=[])


class _RejectLinter(_PassLinter):
    def lint_agent(self, frontmatter, body, agent_meta=None):
        return LintResult(
            passed=False,
            failures=[
                LintFailure(
                    check_name="agent.role_not_empty",
                    severity="error",
                    message="role too short",
                    field_path="role",
                )
            ],
        )


def _stub_gemini(response: GeminiResponseLike):
    def call(**kwargs):
        return response
    return call


def _seed_agent(tmp_path: Path, agent_id: str = "alice", body: str = "# prompt\n"):
    target = tmp_path / ".agents" / agent_id / "SYSTEM_PROMPT.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    return {
        "id": agent_id,
        "name": agent_id,
        "role": "tester",
        "system_prompt_path": f".agents/{agent_id}/SYSTEM_PROMPT.md",
    }


def _mkstate(agent_id, registry, workflow=None, **overrides):
    base = {
        "workflow": workflow or {"pattern": "pipeline", "routing_config": {}},
        "registry": registry,
        "inbox": {},
        "history": [],
        "artifacts": {},
        "retry_count": 0,
        "retry_limit": 3,
        "test_passed": False,
        "errors": [],
        "run_id": "run-test",
        "pending_tool_calls": [],
        "tool_results": {},
        "tool_iterations": 0,
        "current_target": agent_id,
    }
    base.update(overrides)
    return base


def test_worker_text_only_produces_history_and_drains_inbox(tmp_path):
    agent = _seed_agent(tmp_path)
    response = GeminiResponseLike(
        text=json.dumps({"text": "done", "event_summary": "did the thing"}),
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response),
        linter=_PassLinter(),
        repo_root=str(tmp_path),
        now=lambda: "2026-04-19T00:00:00Z",
    )
    worker = make_worker_node(deps)
    state = _mkstate(
        agent["id"],
        [agent],
        inbox={agent["id"]: [{"content": "please do X"}]},
    )
    out = worker(state)
    assert out["inbox"] == {agent["id"]: []}
    assert out["history"][-1]["kind"] == "worker_complete"
    assert out["history"][-1]["agent"] == agent["id"]


def test_worker_creates_agents_when_linter_passes(tmp_path):
    parent = _seed_agent(tmp_path, "parent")
    response = GeminiResponseLike(
        text=json.dumps(
            {
                "create_agents": [
                    {
                        "id": "child1",
                        "role": "a nested role that is long enough",
                        "system_prompt_path": ".agents/child1/SYSTEM_PROMPT.md",
                        "system_prompt_body": "# child1\n",
                    }
                ],
                "event_summary": "spawned child1",
            }
        ),
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response),
        linter=_PassLinter(),
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    state = _mkstate(parent["id"], [parent])
    out = worker(state)
    assert "registry" in out
    assert out["registry"][0]["id"] == "child1"
    assert out["registry"][0]["created_by"] == "parent"
    child_file = tmp_path / ".agents" / "child1" / "SYSTEM_PROMPT.md"
    assert child_file.exists()
    assert "child1" in child_file.read_text()


def test_worker_rejects_create_agent_when_linter_fails(tmp_path):
    parent = _seed_agent(tmp_path, "parent")
    response = GeminiResponseLike(
        text=json.dumps(
            {
                "create_agents": [
                    {
                        "id": "bad",
                        "role": "x",
                        "system_prompt_path": ".agents/bad/SYSTEM_PROMPT.md",
                    }
                ],
                "event_summary": "tried to spawn",
            }
        ),
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response),
        linter=_RejectLinter(),
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    state = _mkstate(parent["id"], [parent])
    out = worker(state)
    assert "registry" not in out
    assert any(e["kind"] == "create_agent_lint_failed" for e in out.get("errors", []))
    assert not (tmp_path / ".agents" / "bad" / "SYSTEM_PROMPT.md").exists()


def test_worker_rejects_sandbox_violation_on_create_agent(tmp_path):
    parent = _seed_agent(tmp_path, "parent")
    response = GeminiResponseLike(
        text=json.dumps(
            {
                "create_agents": [
                    {
                        "id": "evil",
                        "role": "escape attempt with a long role value",
                        "system_prompt_path": "../evil/SYSTEM_PROMPT.md",
                        "system_prompt_body": "hacked",
                    }
                ],
                "event_summary": "attempted escape",
            }
        ),
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response),
        linter=_PassLinter(),
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    state = _mkstate(parent["id"], [parent])
    out = worker(state)
    assert "registry" not in out
    assert any(e["kind"] == "create_agent_sandbox_violation" for e in out["errors"])


def test_worker_send_messages_populates_inbox(tmp_path):
    alice = _seed_agent(tmp_path, "alice")
    response = GeminiResponseLike(
        text=json.dumps(
            {
                "send_messages": [{"to": "bob", "content": "hello bob", "kind": "info"}],
                "event_summary": "alice greeted bob",
            }
        ),
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response),
        linter=_PassLinter(),
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    state = _mkstate("alice", [alice])
    out = worker(state)
    assert "alice" in out["inbox"]
    assert out["inbox"]["alice"] == []
    assert out["inbox"]["bob"][0]["from_id"] == "alice"
    assert out["inbox"]["bob"][0]["content"] == "hello bob"


def test_worker_writes_artifacts(tmp_path):
    alice = _seed_agent(tmp_path, "alice")
    response = GeminiResponseLike(
        text=json.dumps(
            {
                "artifacts": [
                    {
                        "path": "_workspace/out.txt",
                        "content": "hello artifact",
                    }
                ],
                "event_summary": "wrote artifact",
            }
        ),
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response),
        linter=_PassLinter(),
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    state = _mkstate("alice", [alice])
    out = worker(state)
    assert (tmp_path / "_workspace" / "out.txt").exists()
    assert out["artifacts"]["_workspace/out.txt"] == "hello artifact"


def test_worker_tool_calls_set_pending_and_skip_parsing(tmp_path):
    alice = _seed_agent(tmp_path, "alice")
    response = GeminiResponseLike(
        text=None,
        tool_calls=[
            ToolCallDecl(id="c1", name="search", args={"q": "foo"}),
        ],
        usage=UsageMetadata(),
    )
    deps = WorkerDeps(
        gemini=_stub_gemini(response),
        linter=_PassLinter(),
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    state = _mkstate("alice", [alice])
    out = worker(state)
    assert out["pending_tool_calls"][0]["id"] == "c1"
    assert out["pending_tool_calls"][0]["caller_agent"] == "alice"
    assert "registry" not in out
    assert out["history"][-1]["kind"] == "worker_tool_call"


def test_worker_missing_target_returns_error(tmp_path):
    alice = _seed_agent(tmp_path, "alice")
    deps = WorkerDeps(
        gemini=_stub_gemini(GeminiResponseLike(text=None)),
        linter=_PassLinter(),
        repo_root=str(tmp_path),
    )
    worker = make_worker_node(deps)
    state = _mkstate("ghost", [alice])
    out = worker(state)
    assert out["errors"][0]["kind"] == "worker_missing_agent"


def test_sandbox_resolve_rejects_traversal(tmp_path):
    with pytest.raises(SandboxViolation):
        resolve_safe(tmp_path, "../escape.md")


def test_sandbox_allows_known_roots(tmp_path):
    for root in (".agents", "_workspace", ".gemini"):
        p = resolve_safe(tmp_path, f"{root}/file.txt")
        assert str(p).startswith(str(tmp_path))

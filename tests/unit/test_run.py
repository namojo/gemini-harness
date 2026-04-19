"""Unit tests for run_harness with scripted GeminiClient."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gemini_harness.runtime.contracts import GeminiResponseLike, LintResult
from gemini_harness.runtime.harness_runtime import RunError, run_harness


class _PassLinter:
    def lint_agent(self, frontmatter, body, agent_meta=None):
        return LintResult(passed=True, failures=[])

    def lint_skill(self, frontmatter, body, entry_path, read_root):
        return LintResult(passed=True, failures=[])

    def lint_workflow(self, workflow):
        return LintResult(passed=True, failures=[])


def _seed_agent(root: Path, agent_id: str) -> dict:
    sp = root / ".agents" / agent_id / "SYSTEM_PROMPT.md"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(f"# system prompt for {agent_id}\nrole: test\n", encoding="utf-8")
    return {
        "id": agent_id,
        "name": agent_id,
        "role": f"test agent responsible for {agent_id} role (long enough for schema).",
        "system_prompt_path": f".agents/{agent_id}/SYSTEM_PROMPT.md",
    }


def _write_workflow(root: Path, workflow: dict) -> None:
    (root / "workflow.json").write_text(
        json.dumps(workflow, ensure_ascii=False), encoding="utf-8"
    )


def _scripted(responses: dict[str, dict]):
    def call(**kwargs):
        system = kwargs.get("system") or ""
        for agent_id, payload in responses.items():
            if agent_id in system:
                return GeminiResponseLike(text=json.dumps(payload))
        return GeminiResponseLike(text=json.dumps({"event_summary": "no-op"}))

    return call


def test_run_without_workflow_raises(tmp_path: Path):
    with pytest.raises(RunError, match="No workflow.json"):
        run_harness(
            project_path=str(tmp_path),
            user_input="test",
            gemini_callable=_scripted({}),
            linter=_PassLinter(),
        )


def test_run_rejects_schema_violation(tmp_path: Path):
    bad = {"version": "1.0", "pattern": "bogus_pattern", "initial_registry": []}
    _write_workflow(tmp_path, bad)
    with pytest.raises(RunError, match="schema"):
        run_harness(
            project_path=str(tmp_path),
            user_input="test",
            gemini_callable=_scripted({}),
            linter=_PassLinter(),
        )


def test_run_happy_path_fan_out_fan_in(tmp_path: Path):
    a = _seed_agent(tmp_path, "worker-a")
    b = _seed_agent(tmp_path, "worker-b")
    integrator = _seed_agent(tmp_path, "integrator-x")
    workflow = {
        "version": "1.0",
        "pattern": "fan_out_fan_in",
        "retry_limit": 3,
        "initial_registry": [a, b, integrator],
        "routing_config": {"integrator_id": "integrator-x"},
    }
    _write_workflow(tmp_path, workflow)

    scripted = _scripted(
        {
            "worker-a": {
                "artifacts": [
                    {"path": "_workspace/a.txt", "content": "A"}
                ],
                "event_summary": "worker-a done",
            },
            "worker-b": {
                "artifacts": [
                    {"path": "_workspace/b.txt", "content": "B"}
                ],
                "event_summary": "worker-b done",
            },
            "integrator-x": {
                "artifacts": [
                    {"path": "_workspace/out.txt", "content": "A+B"}
                ],
                "event_summary": "integrated",
            },
        }
    )

    result = run_harness(
        project_path=str(tmp_path),
        user_input="fan out and combine these",
        gemini_callable=scripted,
        linter=_PassLinter(),
    )

    assert result["errors"] == []
    assert result["steps"] > 0
    assert "worker-a" in [
        a["id"] for a in result["final_registry"]
    ]
    # Artifacts produced
    artifacts = set(result["artifacts"])
    assert any("a.txt" in p for p in artifacts)
    assert any("b.txt" in p for p in artifacts)
    # Files on disk
    assert (tmp_path / "_workspace" / "out.txt").exists()
    # Context written
    assert Path(result["context_md_path"]).exists()
    # Checkpoint created
    assert Path(result["checkpoint_path"]).exists()


def test_run_pipeline_pattern(tmp_path: Path):
    analyst = _seed_agent(tmp_path, "analyst")
    writer = _seed_agent(tmp_path, "writer")
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "retry_limit": 3,
        "initial_registry": [analyst, writer],
        "routing_config": {},
    }
    _write_workflow(tmp_path, workflow)

    scripted = _scripted(
        {
            "analyst": {
                "artifacts": [{"path": "_workspace/plan.md", "content": "plan"}],
                "event_summary": "analysis complete",
            },
            "writer": {
                "artifacts": [{"path": "_workspace/draft.md", "content": "draft"}],
                "event_summary": "draft complete",
            },
        }
    )

    result = run_harness(
        project_path=str(tmp_path),
        user_input="analyze then write",
        gemini_callable=scripted,
        linter=_PassLinter(),
    )
    assert result["errors"] == []
    assert (tmp_path / "_workspace" / "plan.md").exists()
    assert (tmp_path / "_workspace" / "draft.md").exists()


def test_run_seeds_user_input_into_entry_inbox(tmp_path: Path):
    entry = _seed_agent(tmp_path, "entry-agent")
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "retry_limit": 3,
        "initial_registry": [entry],
        "routing_config": {},
    }
    _write_workflow(tmp_path, workflow)

    captured_prompts: list[str] = []

    def capture_call(**kwargs):
        prompt = kwargs.get("prompt") or ""
        if isinstance(prompt, list):
            prompt = "\n".join(prompt)
        captured_prompts.append(str(prompt))
        return GeminiResponseLike(
            text=json.dumps({"event_summary": "done"})
        )

    run_harness(
        project_path=str(tmp_path),
        user_input="please analyze this domain deeply",
        gemini_callable=capture_call,
        linter=_PassLinter(),
    )

    assert any("analyze this domain deeply" in p for p in captured_prompts)


def test_run_writes_context_md(tmp_path: Path):
    agent = _seed_agent(tmp_path, "solo")
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "retry_limit": 3,
        "initial_registry": [agent],
        "routing_config": {},
    }
    _write_workflow(tmp_path, workflow)

    scripted = _scripted(
        {
            "solo": {
                "artifacts": [{"path": "_workspace/out.txt", "content": "hi"}],
                "event_summary": "solo done",
            }
        }
    )

    run_harness(
        project_path=str(tmp_path),
        user_input="go",
        gemini_callable=scripted,
        linter=_PassLinter(),
    )
    ctx = (tmp_path / ".gemini" / "context.md").read_text(encoding="utf-8")
    assert "run=" in ctx
    assert "node=" in ctx


def test_run_respects_step_limit(tmp_path: Path):
    agents = [_seed_agent(tmp_path, f"a-{i}") for i in range(3)]
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "retry_limit": 3,
        "initial_registry": agents,
        "routing_config": {},
    }
    _write_workflow(tmp_path, workflow)

    scripted = _scripted(
        {f"a-{i}": {"event_summary": f"a-{i} done"} for i in range(3)}
    )

    result = run_harness(
        project_path=str(tmp_path),
        user_input="go",
        gemini_callable=scripted,
        linter=_PassLinter(),
        step_limit=2,
    )
    assert result["steps"] <= 2


def test_run_generates_run_id_when_absent(tmp_path: Path):
    agent = _seed_agent(tmp_path, "solo")
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "retry_limit": 3,
        "initial_registry": [agent],
        "routing_config": {},
    }
    _write_workflow(tmp_path, workflow)

    scripted = _scripted(
        {"solo": {"event_summary": "done"}}
    )
    result = run_harness(
        project_path=str(tmp_path),
        user_input="go",
        gemini_callable=scripted,
        linter=_PassLinter(),
    )
    assert result["run_id"].startswith("run-")

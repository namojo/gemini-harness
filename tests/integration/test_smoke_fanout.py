"""End-to-end smoke test: a tiny 2-agent fan_out_fan_in graph with a mock
GeminiClient driving each node to completion."""
from __future__ import annotations

import json
from pathlib import Path

from gemini_harness.runtime import (
    WorkerDeps,
    build_harness_graph,
    initial_state,
)
from gemini_harness.runtime.contracts import GeminiResponseLike, LintResult


class _PassLinter:
    def lint_agent(self, meta):
        return LintResult(ok=True)

    def lint_skill(self, meta):
        return LintResult(ok=True)

    def lint_workflow(self, workflow):
        return LintResult(ok=True)


def _seed(repo: Path, agent_id: str, body: str = "") -> dict:
    target = repo / ".agents" / agent_id / "SYSTEM_PROMPT.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body or f"# {agent_id}\n")
    return {
        "id": agent_id,
        "name": agent_id,
        "role": "short test role here",
        "system_prompt_path": f".agents/{agent_id}/SYSTEM_PROMPT.md",
    }


def _scripted_gemini(responses_by_agent: dict[str, GeminiResponseLike]):
    """Return a gemini stub that dispatches based on which agent's system_prompt
    is active. The prompt contains 'pattern=' plus the inbox, but the simplest
    discriminator we have is the system string — we embed the agent id there.

    Since WorkerDeps.gemini gets ``system=`` plus the prompt, we peek at
    ``system`` to pick the scripted response.
    """

    def call(**kwargs):
        system = kwargs.get("system") or ""
        for agent_id, resp in responses_by_agent.items():
            if agent_id in system:
                return resp
        return GeminiResponseLike(
            text=json.dumps({"event_summary": "default"}),
        )

    return call


def test_fan_out_fan_in_completes(tmp_path: Path):
    # Seed .agents/<id>/SYSTEM_PROMPT.md so the system string includes the id —
    # scripted_gemini discriminates on that.
    worker_a = _seed(tmp_path, "worker_a", body="# system worker_a\nsystem prompt\n")
    worker_b = _seed(tmp_path, "worker_b", body="# system worker_b\nsystem prompt\n")
    integrator = _seed(tmp_path, "integrator", body="# system integrator\nsystem prompt\n")

    workflow = {
        "version": "1.0",
        "pattern": "fan_out_fan_in",
        "initial_registry": [worker_a, worker_b, integrator],
        "routing_config": {"integrator_id": "integrator"},
        "retry_limit": 3,
    }

    scripted = _scripted_gemini(
        {
            "worker_a": GeminiResponseLike(
                text=json.dumps(
                    {
                        "artifacts": [
                            {"path": "_workspace/a.txt", "content": "A"}
                        ],
                        "event_summary": "worker_a done",
                    }
                )
            ),
            "worker_b": GeminiResponseLike(
                text=json.dumps(
                    {
                        "artifacts": [
                            {"path": "_workspace/b.txt", "content": "B"}
                        ],
                        "event_summary": "worker_b done",
                    }
                )
            ),
            "integrator": GeminiResponseLike(
                text=json.dumps(
                    {
                        "artifacts": [
                            {"path": "_workspace/combined.txt", "content": "A+B"}
                        ],
                        "event_summary": "integrator done",
                    }
                )
            ),
        }
    )

    deps = WorkerDeps(
        gemini=scripted,
        linter=_PassLinter(),
        repo_root=str(tmp_path),
        now=lambda: "2026-04-19T00:00:00Z",
    )

    app = build_harness_graph(worker_deps=deps, tool_executor_deps=None)

    seed_state = initial_state(workflow, run_id="smoke-run")
    config = {"configurable": {"thread_id": "smoke-run"}, "recursion_limit": 50}

    final = app.invoke(seed_state, config)

    complete_events = [e for e in final["history"] if e.get("kind") == "worker_complete"]
    agents_run = {e["agent"] for e in complete_events}
    assert agents_run == {"worker_a", "worker_b", "integrator"}

    assert (tmp_path / "_workspace" / "combined.txt").exists()
    assert (tmp_path / "_workspace" / "a.txt").exists()
    assert (tmp_path / "_workspace" / "b.txt").exists()

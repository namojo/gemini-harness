"""Prove that fan_out_fan_in actually executes workers in parallel wall-clock.

Uses a mock Gemini client that ``time.sleep(delay)`` inside each call. If the
runtime is truly parallel, total time ≈ max(delays) + overhead, not the sum.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from gemini_harness.runtime.contracts import GeminiResponseLike, LintResult
from gemini_harness.runtime.harness_runtime import run_harness


class _PassLinter:
    def lint_agent(self, fm, body, agent_meta=None):
        return LintResult(passed=True, failures=[])

    def lint_skill(self, fm, body, ep, rr):
        return LintResult(passed=True, failures=[])

    def lint_workflow(self, wf):
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


def _slow_gemini(delay_s: float, agent_payloads: dict[str, dict]):
    """Mock that sleeps ``delay_s`` inside every call before returning."""

    def call(**kwargs):
        time.sleep(delay_s)
        system = kwargs.get("system") or ""
        for agent_id, payload in agent_payloads.items():
            if agent_id in system:
                return GeminiResponseLike(text=json.dumps(payload))
        return GeminiResponseLike(text=json.dumps({"event_summary": "no-op"}))

    return call


def test_fan_out_fan_in_runs_workers_in_parallel(tmp_path: Path):
    """4 workers × 1.0s each + integrator (1.0s).

    Sequential total ≈ 5.0s. Parallel total ≈ 2.0s (max(workers) + integrator).
    Budget 3.5s proves parallelism (allows generous overhead).
    """
    workers = [_seed_agent(tmp_path, f"worker-{i}") for i in range(4)]
    integrator = _seed_agent(tmp_path, "integrator")
    workflow = {
        "version": "1.0",
        "pattern": "fan_out_fan_in",
        "retry_limit": 3,
        "initial_registry": workers + [integrator],
        "routing_config": {"integrator_id": "integrator"},
    }
    _write_workflow(tmp_path, workflow)

    payloads = {
        f"worker-{i}": {
            "artifacts": [{"path": f"_workspace/worker-{i}/out.txt", "content": f"W{i}"}],
            "event_summary": f"worker-{i} done",
        }
        for i in range(4)
    }
    payloads["integrator"] = {
        "artifacts": [{"path": "_workspace/integrator/out.txt", "content": "merged"}],
        "event_summary": "integrated",
    }

    per_call_delay = 1.0
    scripted = _slow_gemini(per_call_delay, payloads)

    t0 = time.monotonic()
    result = run_harness(
        project_path=str(tmp_path),
        user_input="fan out test",
        gemini_callable=scripted,
        linter=_PassLinter(),
    )
    elapsed = time.monotonic() - t0

    assert result["errors"] == [], result["errors"]
    # 4 workers × 1.0s sequential would be 4.0s + integrator 1.0s = 5.0s.
    # Parallel should finish under 3.5s.
    assert elapsed < 3.5, (
        f"fan_out_fan_in did not run in parallel: elapsed={elapsed:.2f}s "
        f"(expected < 3.5s, sequential ≈ 5.0s)"
    )


def test_pipeline_runs_sequentially_as_expected(tmp_path: Path):
    """Sanity: pipeline (not parallel) should still be sequential.

    2 agents × 0.5s each → should take ≥ 1.0s (sequential).
    Upper bound 2.0s catches regressions.
    """
    agents = [_seed_agent(tmp_path, f"step-{i}") for i in range(2)]
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "retry_limit": 3,
        "initial_registry": agents,
        "routing_config": {},
    }
    _write_workflow(tmp_path, workflow)

    scripted = _slow_gemini(
        0.5,
        {f"step-{i}": {"event_summary": f"step-{i} done"} for i in range(2)},
    )

    t0 = time.monotonic()
    result = run_harness(
        project_path=str(tmp_path),
        user_input="pipeline test",
        gemini_callable=scripted,
        linter=_PassLinter(),
    )
    elapsed = time.monotonic() - t0

    assert result["errors"] == []
    assert 0.9 < elapsed < 2.0, f"pipeline elapsed={elapsed:.2f}s"

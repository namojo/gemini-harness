"""Manager routing tests — verify Command returns under various state shapes."""
from __future__ import annotations

from gemini_harness.runtime.compat import END, Command, Send
from gemini_harness.runtime.manager import manager_node


def _mkstate(**overrides):
    base = {
        "workflow": {
            "version": "1.0",
            "pattern": "pipeline",
            "initial_registry": [],
        },
        "registry": [],
        "inbox": {},
        "history": [],
        "artifacts": {},
        "retry_count": 0,
        "retry_limit": 3,
        "test_passed": False,
        "errors": [],
        "run_id": "t",
        "pending_tool_calls": [],
        "tool_results": {},
        "tool_iterations": 0,
    }
    base.update(overrides)
    return base


def _is_cmd_goto(cmd, target):
    return isinstance(cmd, Command) and cmd.goto == target


def test_manager_routes_to_tool_executor_when_pending_calls():
    state = _mkstate(pending_tool_calls=[{"id": "c1", "name": "x", "args": {}}])
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, "tool_executor")


def test_manager_end_when_no_route():
    state = _mkstate(registry=[])
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, END)


def test_manager_dispatches_to_worker_for_pipeline():
    state = _mkstate(
        registry=[{"id": "a"}, {"id": "b"}],
    )
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, "worker")
    assert cmd.update == {"current_target": "a"}


def test_manager_increments_retry_on_producer_rerun():
    workflow = {
        "version": "1.0",
        "pattern": "producer_reviewer",
        "initial_registry": [],
        "routing_config": {"producer_id": "p", "reviewer_id": "r"},
    }
    state = _mkstate(
        workflow=workflow,
        registry=[{"id": "p"}, {"id": "r"}],
        history=[
            {"kind": "worker_complete", "agent": "p"},
            {"kind": "worker_complete", "agent": "r"},
        ],
        retry_count=0,
        test_passed=False,
    )
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, "worker")
    assert cmd.update["current_target"] == "p"
    assert cmd.update["retry_count"] == 1


def test_manager_ends_when_retry_exhausted_in_producer_reviewer():
    workflow = {
        "version": "1.0",
        "pattern": "producer_reviewer",
        "initial_registry": [],
        "routing_config": {"producer_id": "p", "reviewer_id": "r"},
    }
    state = _mkstate(
        workflow=workflow,
        registry=[{"id": "p"}, {"id": "r"}],
        history=[
            {"kind": "worker_complete", "agent": "p"},
            {"kind": "worker_complete", "agent": "r"},
        ],
        retry_count=3,
        retry_limit=3,
    )
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, END)


def test_manager_ends_when_producer_reviewer_passed():
    workflow = {
        "version": "1.0",
        "pattern": "producer_reviewer",
        "initial_registry": [],
        "routing_config": {"producer_id": "p", "reviewer_id": "r"},
    }
    state = _mkstate(
        workflow=workflow,
        registry=[{"id": "p"}, {"id": "r"}],
        history=[
            {"kind": "worker_complete", "agent": "p"},
            {"kind": "worker_complete", "agent": "r"},
        ],
        test_passed=True,
    )
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, END)


def test_manager_returns_sends_for_fan_out():
    workflow = {
        "version": "1.0",
        "pattern": "fan_out_fan_in",
        "initial_registry": [],
        "routing_config": {"integrator_id": "i"},
    }
    state = _mkstate(
        workflow=workflow,
        registry=[{"id": "a"}, {"id": "b"}, {"id": "i"}],
    )
    cmd = manager_node(state)
    assert isinstance(cmd, Command)
    assert isinstance(cmd.goto, list)
    assert all(isinstance(s, Send) for s in cmd.goto)
    assert {s.arg["current_target"] for s in cmd.goto} == {"a", "b"}


def test_manager_fan_in_routes_to_integrator_when_workers_done():
    workflow = {
        "version": "1.0",
        "pattern": "fan_out_fan_in",
        "initial_registry": [],
        "routing_config": {"integrator_id": "i"},
    }
    state = _mkstate(
        workflow=workflow,
        registry=[{"id": "a"}, {"id": "b"}, {"id": "i"}],
        history=[
            {"kind": "worker_complete", "agent": "a"},
            {"kind": "worker_complete", "agent": "b"},
        ],
    )
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, "worker")
    assert cmd.update["current_target"] == "i"


def test_manager_after_tool_executor_goes_back_to_worker_with_same_target():
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "initial_registry": [],
        "routing_config": {"tool_executor": {"max_tool_iterations": 5}},
    }
    state = _mkstate(
        workflow=workflow,
        registry=[{"id": "a"}],
        current_target="a",
        history=[{"kind": "tool_executor_complete", "agent": "a"}],
        tool_iterations=1,
    )
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, "worker")
    assert cmd.update["current_target"] == "a"
    assert cmd.update["tool_iterations"] == 2


def test_manager_tool_iter_exhausted_ends():
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "initial_registry": [],
        "routing_config": {"tool_executor": {"max_tool_iterations": 2}},
    }
    state = _mkstate(
        workflow=workflow,
        registry=[{"id": "a"}],
        current_target="a",
        history=[{"kind": "tool_executor_complete", "agent": "a"}],
        tool_iterations=2,
    )
    cmd = manager_node(state)
    assert _is_cmd_goto(cmd, END)
    assert cmd.update["errors"][0]["kind"] == "tool_iter_exhausted"


def test_manager_composite_pattern_picks_sub_from_phase_map():
    workflow = {
        "version": "1.0",
        "pattern": "fan_out_fan_in+producer_reviewer",
        "initial_registry": [],
        "routing_config": {
            "integrator_id": "i",
            "producer_id": "p",
            "reviewer_id": "r",
            "phase_map": {"review": "producer_reviewer"},
        },
    }
    state = _mkstate(
        workflow=workflow,
        registry=[{"id": "p"}, {"id": "r"}],
        phase="review",
    )
    cmd = manager_node(state)
    # producer_reviewer with no history → producer
    assert _is_cmd_goto(cmd, "worker")
    assert cmd.update["current_target"] == "p"

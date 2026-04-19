"""Unit tests for each of the 6 pattern routing functions."""
from __future__ import annotations

from gemini_harness.runtime.compat import Send
from gemini_harness.runtime.patterns import (
    route_expert_pool,
    route_fan_out_fan_in,
    route_hierarchical,
    route_pipeline,
    route_producer_reviewer,
    route_supervisor,
)


def _st(**kw):
    base = {
        "workflow": {"routing_config": {}},
        "registry": [],
        "inbox": {},
        "history": [],
        "task_queue": [],
        "retry_count": 0,
        "retry_limit": 3,
        "test_passed": False,
    }
    base.update(kw)
    return base


# ----- Pipeline -------------------------------------------------------------


def test_pipeline_returns_first_agent():
    state = _st(registry=[{"id": "a"}, {"id": "b"}])
    assert route_pipeline(state) == "a"


def test_pipeline_skips_completed():
    state = _st(
        registry=[{"id": "a"}, {"id": "b"}],
        history=[{"kind": "worker_complete", "agent": "a"}],
    )
    assert route_pipeline(state) == "b"


def test_pipeline_returns_none_when_all_done():
    state = _st(
        registry=[{"id": "a"}],
        history=[{"kind": "worker_complete", "agent": "a"}],
    )
    assert route_pipeline(state) is None


# ----- Fan-out/Fan-in -------------------------------------------------------


def test_fan_out_returns_sends_for_each_worker():
    state = _st(
        workflow={"routing_config": {"integrator_id": "i"}},
        registry=[{"id": "a"}, {"id": "b"}, {"id": "i"}],
    )
    result = route_fan_out_fan_in(state)
    assert isinstance(result, list)
    assert len(result) == 2
    assert all(isinstance(s, Send) for s in result)
    assert {s.arg["current_target"] for s in result} == {"a", "b"}


def test_fan_out_returns_integrator_when_workers_done():
    state = _st(
        workflow={"routing_config": {"integrator_id": "i"}},
        registry=[{"id": "a"}, {"id": "i"}],
        history=[{"kind": "worker_complete", "agent": "a"}],
    )
    assert route_fan_out_fan_in(state) == "i"


def test_fan_out_returns_none_when_all_done():
    state = _st(
        workflow={"routing_config": {"integrator_id": "i"}},
        registry=[{"id": "a"}, {"id": "i"}],
        history=[
            {"kind": "worker_complete", "agent": "a"},
            {"kind": "worker_complete", "agent": "i"},
        ],
    )
    assert route_fan_out_fan_in(state) is None


# ----- Expert Pool ----------------------------------------------------------


def test_expert_pool_keyword_classifier_matches():
    state = _st(
        workflow={"routing_config": {"classifier": {"tax": "tax_expert", "law": "law_expert"}}},
        registry=[{"id": "tax_expert"}, {"id": "law_expert"}],
        inbox={"_router": [{"content": "question about tax rules"}]},
    )
    assert route_expert_pool(state) == "tax_expert"


def test_expert_pool_no_classifier_falls_back_to_pipeline():
    state = _st(
        workflow={"routing_config": {}},
        registry=[{"id": "a"}, {"id": "b"}],
        history=[{"kind": "worker_complete", "agent": "a"}],
    )
    assert route_expert_pool(state) == "b"


# ----- Producer-Reviewer ----------------------------------------------------


def test_pr_starts_with_producer():
    state = _st(
        workflow={"routing_config": {"producer_id": "p", "reviewer_id": "r"}},
        registry=[{"id": "p"}, {"id": "r"}],
    )
    assert route_producer_reviewer(state) == "p"


def test_pr_goes_to_reviewer_after_producer():
    state = _st(
        workflow={"routing_config": {"producer_id": "p", "reviewer_id": "r"}},
        registry=[{"id": "p"}, {"id": "r"}],
        history=[{"kind": "worker_complete", "agent": "p"}],
    )
    assert route_producer_reviewer(state) == "r"


def test_pr_ends_when_passed():
    state = _st(
        workflow={"routing_config": {"producer_id": "p", "reviewer_id": "r"}},
        registry=[{"id": "p"}, {"id": "r"}],
        history=[
            {"kind": "worker_complete", "agent": "p"},
            {"kind": "worker_complete", "agent": "r"},
        ],
        test_passed=True,
    )
    assert route_producer_reviewer(state) is None


def test_pr_loops_back_to_producer_on_fail_within_limit():
    state = _st(
        workflow={"routing_config": {"producer_id": "p", "reviewer_id": "r"}},
        registry=[{"id": "p"}, {"id": "r"}],
        history=[
            {"kind": "worker_complete", "agent": "p"},
            {"kind": "worker_complete", "agent": "r"},
        ],
        test_passed=False,
        retry_count=1,
        retry_limit=3,
    )
    assert route_producer_reviewer(state) == "p"


def test_pr_ends_when_retry_exhausted():
    state = _st(
        workflow={"routing_config": {"producer_id": "p", "reviewer_id": "r"}},
        registry=[{"id": "p"}, {"id": "r"}],
        history=[
            {"kind": "worker_complete", "agent": "p"},
            {"kind": "worker_complete", "agent": "r"},
        ],
        retry_count=3,
        retry_limit=3,
    )
    assert route_producer_reviewer(state) is None


# ----- Supervisor -----------------------------------------------------------


def test_supervisor_runs_first_when_queue_empty():
    state = _st(
        workflow={"routing_config": {"supervisor_id": "sup"}},
        registry=[{"id": "sup"}, {"id": "w1"}],
        task_queue=[],
    )
    assert route_supervisor(state) == "sup"


def test_supervisor_dispatches_to_assignee():
    state = _st(
        workflow={"routing_config": {"supervisor_id": "sup"}},
        registry=[{"id": "sup"}, {"id": "w1"}],
        task_queue=[{"id": "t1", "status": "pending", "assigned_to": "w1"}],
    )
    assert route_supervisor(state) == "w1"


def test_supervisor_picks_idle_worker_when_no_assignee():
    state = _st(
        workflow={"routing_config": {"supervisor_id": "sup"}},
        registry=[{"id": "sup"}, {"id": "w1"}, {"id": "w2"}],
        task_queue=[{"id": "t1", "status": "pending", "assigned_to": None}],
    )
    assert route_supervisor(state) in {"w1", "w2"}


def test_supervisor_done_when_tasks_completed():
    state = _st(
        workflow={"routing_config": {"supervisor_id": "sup"}},
        registry=[{"id": "sup"}, {"id": "w1"}],
        task_queue=[{"id": "t1", "status": "completed"}],
    )
    assert route_supervisor(state) is None


# ----- Hierarchical ---------------------------------------------------------


def test_hierarchical_starts_with_root():
    state = _st(
        workflow={"routing_config": {"root_id": "boss"}},
        registry=[{"id": "boss"}],
    )
    assert route_hierarchical(state) == "boss"


def test_hierarchical_dispatches_children_after_root_first_run():
    state = _st(
        workflow={"routing_config": {"root_id": "boss"}},
        registry=[
            {"id": "boss"},
            {"id": "child1", "created_by": "boss"},
            {"id": "child2", "created_by": "boss"},
        ],
        history=[{"kind": "worker_complete", "agent": "boss"}],
    )
    assert route_hierarchical(state) == "child1"


def test_hierarchical_reruns_root_after_all_children_complete():
    state = _st(
        workflow={"routing_config": {"root_id": "boss"}},
        registry=[
            {"id": "boss"},
            {"id": "child1", "created_by": "boss"},
        ],
        history=[
            {"kind": "worker_complete", "agent": "boss"},
            {"kind": "worker_complete", "agent": "child1"},
        ],
    )
    assert route_hierarchical(state) == "boss"


def test_hierarchical_ends_after_root_second_run():
    state = _st(
        workflow={"routing_config": {"root_id": "boss"}},
        registry=[
            {"id": "boss"},
            {"id": "child1", "created_by": "boss"},
        ],
        history=[
            {"kind": "worker_complete", "agent": "boss"},
            {"kind": "worker_complete", "agent": "child1"},
            {"kind": "worker_complete", "agent": "boss"},
        ],
    )
    assert route_hierarchical(state) is None

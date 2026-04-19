"""Reducer tests — append_unique, merge_inboxes, merge_dicts, add."""
from __future__ import annotations

from gemini_harness.runtime.compat import (
    add,
    append_unique,
    merge_dicts,
    merge_inboxes,
)
from gemini_harness.runtime.state import initial_state


def test_append_unique_dedupes_by_id():
    a = [{"id": "x", "role": "r1"}]
    b = [{"id": "x", "role": "r2"}, {"id": "y", "role": "r3"}]
    out = append_unique(a, b)
    assert [item["id"] for item in out] == ["x", "y"]
    # First write wins (idempotent replay).
    assert out[0]["role"] == "r1"


def test_append_unique_is_idempotent():
    a = [{"id": "x"}]
    once = append_unique(a, [{"id": "x"}])
    twice = append_unique(once, [{"id": "x"}])
    assert once == twice == [{"id": "x"}]


def test_append_unique_preserves_non_dict_items():
    out = append_unique([], ["string", {"id": "a"}])
    assert out == ["string", {"id": "a"}]


def test_append_unique_handles_none():
    assert append_unique(None, [{"id": "a"}]) == [{"id": "a"}]
    assert append_unique([{"id": "a"}], None) == [{"id": "a"}]
    assert append_unique(None, None) == []


def test_merge_inboxes_appends_per_key():
    a = {"alice": [{"content": "hi"}]}
    b = {"alice": [{"content": "there"}], "bob": [{"content": "hey"}]}
    out = merge_inboxes(a, b)
    assert len(out["alice"]) == 2
    assert out["bob"] == [{"content": "hey"}]


def test_merge_inboxes_empty_list_drains_key():
    a = {"alice": [{"content": "hi"}, {"content": "there"}]}
    b = {"alice": []}
    out = merge_inboxes(a, b)
    assert out["alice"] == []


def test_merge_inboxes_handles_none():
    assert merge_inboxes(None, {"a": [1]}) == {"a": [1]}
    assert merge_inboxes({"a": [1]}, None) == {"a": [1]}


def test_merge_dicts_rhs_wins():
    assert merge_dicts({"a": 1, "b": 2}, {"b": 99, "c": 3}) == {"a": 1, "b": 99, "c": 3}


def test_merge_dicts_handles_none():
    assert merge_dicts(None, {"a": 1}) == {"a": 1}
    assert merge_dicts({"a": 1}, None) == {"a": 1}


def test_add_concats_lists():
    assert add([1, 2], [3]) == [1, 2, 3]


def test_initial_state_shapes_workflow():
    workflow = {
        "version": "1.0",
        "pattern": "pipeline",
        "initial_registry": [{"id": "a"}],
        "retry_limit": 5,
    }
    state = initial_state(workflow, run_id="run-1")
    assert state["run_id"] == "run-1"
    assert state["registry"] == [{"id": "a"}]
    assert state["retry_limit"] == 5
    assert state["retry_count"] == 0
    assert state["tool_iterations"] == 0
    assert state["pending_tool_calls"] == []


def test_initial_state_routing_config_retry_override():
    workflow = {
        "version": "1.0",
        "pattern": "producer_reviewer",
        "initial_registry": [{"id": "a"}],
        "retry_limit": 3,
        "routing_config": {"retry_limit": 7},
    }
    state = initial_state(workflow, run_id="run-2")
    assert state["retry_limit"] == 7

"""Expert-pool pattern: classify the current task, dispatch to one expert.

``routing_config.classifier`` is either:
- a string (LLM prompt — not implemented in v1; raises NotImplementedError)
- a dict mapping keyword -> expert_id (keyword match on
  ``state['current_task']`` or last inbox message, case-insensitive)

v1 uses the keyword-map form; the LLM classifier is deferred.
"""
from __future__ import annotations

from ..state import HarnessState


def _current_task_text(state: HarnessState) -> str:
    task = state.get("current_task") or ""  # optional loose field
    if isinstance(task, str) and task:
        return task
    for events in (state.get("history") or [])[::-1]:
        if events.get("kind") == "task":
            return str(events.get("summary", ""))
    for msgs in (state.get("inbox") or {}).values():
        if msgs:
            last = msgs[-1]
            return str(last.get("content", ""))
    return ""


def route(state: HarnessState) -> str | None:
    routing = (state.get("workflow") or {}).get("routing_config") or {}
    classifier = routing.get("classifier")
    registry = state.get("registry", [])

    if not registry:
        return None

    if classifier is None:
        completed = {
            e.get("agent")
            for e in state.get("history") or []
            if e.get("kind") == "worker_complete"
        }
        for a in registry:
            if a.get("id") not in completed:
                return a.get("id")
        return None

    if isinstance(classifier, dict):
        text = _current_task_text(state).lower()
        for keyword, expert_id in classifier.items():
            if keyword.lower() in text:
                return expert_id
        return None

    if isinstance(classifier, str):
        raise NotImplementedError(
            "LLM-based classifier not implemented in v1; use a keyword map."
        )

    return None

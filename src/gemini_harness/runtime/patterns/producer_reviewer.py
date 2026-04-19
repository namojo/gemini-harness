"""Producer-Reviewer pattern (aka self-critique).

Contract (ADR 0003):
- No completed agent yet → producer.
- Producer just completed → reviewer.
- Reviewer just completed → if ``test_passed``: END; else if
  ``retry_count < retry_limit``: producer (Manager increments retry_count before
  the producer runs); else: None (END, Manager will escalate).

``producer_id`` and ``reviewer_id`` are required in ``routing_config``.
"""
from __future__ import annotations

from ..state import HarnessState


def _last_completed(state: HarnessState) -> str | None:
    for event in reversed(state.get("history") or []):
        if event.get("kind") == "worker_complete":
            return event.get("agent")
    return None


def route(state: HarnessState) -> str | None:
    routing = (state.get("workflow") or {}).get("routing_config") or {}
    producer_id = routing.get("producer_id")
    reviewer_id = routing.get("reviewer_id")
    if not producer_id or not reviewer_id:
        return None

    last = _last_completed(state)
    retry_count = state.get("retry_count", 0)
    retry_limit = state.get("retry_limit", 3)

    if last is None:
        return producer_id

    if last == producer_id:
        return reviewer_id

    if last == reviewer_id:
        if state.get("test_passed"):
            return None
        if retry_count >= retry_limit:
            return None
        return producer_id

    return producer_id

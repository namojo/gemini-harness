"""Add src/ to sys.path for in-repo imports (no pyproject installed yet)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def tmp_metrics_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the metrics writer at a per-test file."""
    target = tmp_path / "calls.jsonl"
    monkeypatch.setenv("GEMINI_HARNESS_METRICS_PATH", str(target))
    return target


@pytest.fixture(autouse=True)
def _scrub_gemini_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: ensure a deterministic GEMINI_API_KEY value so tests never
    accidentally depend on the developer's real key."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-unused")
    if os.environ.get("GEMINI_HARNESS_METRICS_PATH") and "PYTEST_CURRENT_TEST" not in os.environ:
        monkeypatch.delenv("GEMINI_HARNESS_METRICS_PATH", raising=False)

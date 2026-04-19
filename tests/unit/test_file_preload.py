"""Tests for the file pre-loading shortcut in _seed_user_input.

Until Worker wires Gemini function-calling, agents cannot browse the
filesystem. ``_seed_user_input`` therefore inspects the user_input for
project-relative file paths and attaches their contents to the seed
state (inbox + artifacts). This lets the reviewer-style harnesses
actually analyze files the user mentions.
"""
from __future__ import annotations

from pathlib import Path

from gemini_harness.runtime._run import (
    _extract_referenced_files,
    _seed_user_input,
)


def test_extract_referenced_files_picks_existing_files(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "cli.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")

    result = _extract_referenced_files(
        "Please review src/cli.py and README.md for style.", tmp_path
    )
    paths = [p for p, _ in result]
    assert "src/cli.py" in paths
    assert "README.md" in paths


def test_extract_referenced_files_ignores_nonexistent(tmp_path: Path):
    result = _extract_referenced_files("review src/missing.py", tmp_path)
    assert result == []


def test_extract_referenced_files_truncates_large(tmp_path: Path):
    big = tmp_path / "big.log"
    big.write_text("A" * (70 * 1024), encoding="utf-8")
    result = _extract_referenced_files("check big.log", tmp_path)
    assert result
    _, content = result[0]
    assert "truncated" in content.lower()


def test_extract_referenced_files_rejects_escape(tmp_path: Path):
    # ../ paths that resolve outside project_root must be rejected.
    outside = tmp_path.parent / "escape-check.txt"
    outside.write_text("secret\n", encoding="utf-8")
    try:
        result = _extract_referenced_files(
            f"read ../{outside.name}",
            tmp_path,
        )
        assert result == []
    finally:
        outside.unlink(missing_ok=True)


def test_seed_user_input_adds_artifacts_when_files_referenced(tmp_path: Path):
    (tmp_path / "notes.md").write_text("content\n", encoding="utf-8")
    registry = [{"id": "reviewer", "role": "test"}]
    state = {"registry": registry, "inbox": {}, "artifacts": {}}
    out = _seed_user_input(
        state,
        "Please review notes.md in detail.",
        project_root=tmp_path,
    )
    assert "input/notes.md" in out["artifacts"]
    assert out["artifacts"]["input/notes.md"].strip() == "content"
    inbox_msg = out["inbox"]["reviewer"][-1]
    assert "notes.md" in inbox_msg["content"]
    assert "pre-loaded" in inbox_msg["content"].lower()


def test_seed_user_input_no_project_root_keeps_behavior(tmp_path: Path):
    registry = [{"id": "a", "role": "test"}]
    state = {"registry": registry}
    out = _seed_user_input(state, "just analyze this idea")
    assert "artifacts" not in out or not out["artifacts"]
    assert out["inbox"]["a"][0]["content"].startswith("User request: just analyze")

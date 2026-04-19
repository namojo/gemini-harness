"""Unit tests for tool_executor wiring in run_harness (_make_tool_executor).

We test dispatch rules without actual MCP servers — by monkey-patching the
integration functions.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from gemini_harness.runtime._run import _make_tool_executor


def test_allowed_tools_whitelist(monkeypatch):
    called = {}

    def _fake_invoke(skill, args, cwd=".", timeout=60, env=None):
        called["skill"] = skill
        called["args"] = args
        return "OK"

    # Patch the import inside _make_tool_executor's closure
    import gemini_harness.integrations.cli_bridge as cb

    monkeypatch.setattr(cb, "invoke_cli_skill", _fake_invoke)

    executor = _make_tool_executor(
        {"allowed_tools": ["cli:file-manager"], "tool_timeout_s": 5},
        run_id="test",
    )

    # Whitelisted call succeeds
    result = executor("cli:file-manager", {"path": "x"}, timeout_s=5)
    assert result.is_error is False
    assert called["skill"] == "file-manager"

    # Non-whitelisted call blocked
    result2 = executor("cli:other", {}, timeout_s=5)
    assert result2.is_error is True
    assert "allowed_tools" in (result2.text or "")


def test_unknown_transport(monkeypatch):
    executor = _make_tool_executor({"allowed_tools": ["weird"]}, run_id="test")
    result = executor("weird", {}, timeout_s=5)
    assert result.is_error is True
    assert "unknown tool transport" in (result.text or "")


def test_mcp_server_not_configured(monkeypatch):
    executor = _make_tool_executor(
        {"allowed_tools": ["mcp:missing/echo"], "mcp_servers": {}},
        run_id="test",
    )
    result = executor("mcp:missing/echo", {}, timeout_s=5)
    assert result.is_error is True
    assert "not configured" in (result.text or "")


def test_mcp_malformed_tool_name(monkeypatch):
    executor = _make_tool_executor(
        {"allowed_tools": ["mcp:badname"], "mcp_servers": {"x": ["echo"]}},
        run_id="test",
    )
    result = executor("mcp:badname", {}, timeout_s=5)
    assert result.is_error is True
    assert "mcp:<server>" in (result.text or "")


def test_cli_translates_args_to_flags(monkeypatch):
    captured = {}

    def _fake_invoke(skill, args, cwd=".", timeout=60, env=None):
        captured["args"] = args
        return "ran"

    import gemini_harness.integrations.cli_bridge as cb

    monkeypatch.setattr(cb, "invoke_cli_skill", _fake_invoke)

    executor = _make_tool_executor(
        {"allowed_tools": ["cli:search"]},
        run_id="test",
    )
    executor("cli:search", {"query": "langgraph", "limit": 5})
    assert captured["args"] == ["--query", "langgraph", "--limit", "5"]


def test_empty_allowed_list_permits_all(monkeypatch):
    # When allowed_tools is empty the whitelist doesn't apply (by design).
    # An unknown transport still fails — but "cli:*" passes whitelist.
    def _fake_invoke(skill, args, cwd=".", timeout=60, env=None):
        return "ok"

    import gemini_harness.integrations.cli_bridge as cb

    monkeypatch.setattr(cb, "invoke_cli_skill", _fake_invoke)

    executor = _make_tool_executor({"allowed_tools": []}, run_id="test")
    result = executor("cli:anything", {})
    assert result.is_error is False

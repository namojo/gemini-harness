"""Unit tests for `gemini_harness.integrations.mcp_adapter`.

We avoid spinning up real stdio subprocesses — instead we patch the
`_open_session` helper to hand back an in-memory fake session.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest


def _make_fake_session(
    *,
    tool_result=None,
    list_tools_result=None,
    raise_on_call: BaseException | None = None,
    delay: float = 0.0,
):
    class _FakeSession:
        async def call_tool(self, name, args):
            if raise_on_call is not None:
                raise raise_on_call
            if delay:
                await asyncio.sleep(delay)
            return tool_result

        async def list_tools(self):
            return list_tools_result

    return _FakeSession()


def _install_fake_session(monkeypatch, session):
    from gemini_harness.integrations import mcp_adapter

    @asynccontextmanager
    async def _fake_ctx():
        yield session

    async def _fake_open_session(server):
        return _fake_ctx()

    monkeypatch.setattr(mcp_adapter, "_open_session", _fake_open_session)


# -------- happy path --------


def test_call_mcp_tool_joins_text_blocks(tmp_metrics_path, monkeypatch):
    from gemini_harness.integrations import mcp_adapter

    blocks = [
        SimpleNamespace(type="text", text="row 1"),
        SimpleNamespace(type="text", text="row 2"),
        SimpleNamespace(type="image", text=None),
    ]
    tool_result = SimpleNamespace(
        content=blocks,
        structuredContent={"rows": 2},
        isError=False,
    )
    _install_fake_session(monkeypatch, _make_fake_session(tool_result=tool_result))

    server = mcp_adapter.McpServerSpec(
        name="db", transport="stdio", command=["echo-server"]
    )
    result = asyncio.run(
        mcp_adapter.call_mcp_tool(server, "query", {"sql": "select 1"}, node="worker", run_id="t-mcp-1")
    )

    assert result.is_error is False
    assert result.text == "row 1\nrow 2"
    assert result.structured == {"rows": 2}
    assert len(result.raw_content) == 3

    rec = json.loads(tmp_metrics_path.read_text().splitlines()[0])
    assert rec["channel"] == "mcp"
    assert rec["server"] == "db"
    assert rec["tool"] == "query"
    assert rec["outcome"] == "ok"


def test_call_mcp_tool_is_error_surfaced_not_raised(tmp_metrics_path, monkeypatch):
    from gemini_harness.integrations import mcp_adapter

    blocks = [SimpleNamespace(type="text", text="nope")]
    tool_result = SimpleNamespace(content=blocks, structuredContent=None, isError=True)
    _install_fake_session(monkeypatch, _make_fake_session(tool_result=tool_result))

    server = mcp_adapter.McpServerSpec(
        name="api", transport="stdio", command=["srv"]
    )
    result = asyncio.run(
        mcp_adapter.call_mcp_tool(server, "do_thing", {}, run_id="t-mcp-err")
    )
    assert result.is_error is True
    assert result.text == "nope"


def test_call_mcp_tool_timeout(monkeypatch, tmp_metrics_path):
    """Timeout errors are retried by RETRY_TRANSIENT but ultimately surface."""
    from gemini_harness.integrations import mcp_adapter
    from gemini_harness.integrations._errors import GeminiTimeoutError

    # Neuter tenacity delays.
    import gemini_harness.integrations._retry as retry_mod
    from tenacity import wait_none
    import importlib

    monkeypatch.setattr(retry_mod, "wait_exponential", lambda **kw: wait_none())
    importlib.reload(mcp_adapter)

    _install_fake_session(
        monkeypatch,
        _make_fake_session(tool_result=None, delay=1.0),
    )

    server = mcp_adapter.McpServerSpec(name="slow", transport="stdio", command=["s"])
    with pytest.raises(GeminiTimeoutError):
        asyncio.run(mcp_adapter.call_mcp_tool(server, "t", {}, timeout=0.05))


def test_spec_validation_stdio_requires_command():
    from gemini_harness.integrations import mcp_adapter

    bad = mcp_adapter.McpServerSpec(name="x", transport="stdio", command=None)
    with pytest.raises(ValueError):
        mcp_adapter._validate_spec(bad)


def test_spec_validation_http_requires_url():
    from gemini_harness.integrations import mcp_adapter

    bad = mcp_adapter.McpServerSpec(name="x", transport="http", url=None)
    with pytest.raises(ValueError):
        mcp_adapter._validate_spec(bad)


def test_list_mcp_tools_returns_dicts(monkeypatch):
    from gemini_harness.integrations import mcp_adapter

    listed = SimpleNamespace(
        tools=[
            SimpleNamespace(name="t1", description="", inputSchema={"type": "object"}),
            SimpleNamespace(name="t2", description="hi", inputSchema={"type": "object"}),
        ]
    )
    _install_fake_session(monkeypatch, _make_fake_session(list_tools_result=listed))

    server = mcp_adapter.McpServerSpec(name="x", transport="stdio", command=["c"])
    out = asyncio.run(mcp_adapter.list_mcp_tools(server))
    assert [t["name"] for t in out] == ["t1", "t2"]

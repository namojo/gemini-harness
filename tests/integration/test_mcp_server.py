"""Integration test — spawn `gemini-harness-mcp` as a subprocess and
exercise it via the official MCP Python client.

The runtime layer may not be wired yet, so we only assert handshake +
list_tools + that a harness.audit call against a fixture project returns a
well-formed CallToolResult (even if the payload is an error because the
runtime module is unavailable). We validate *protocol*, not runtime output.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import pytest


async def _list_and_call(project_path: str) -> tuple[list[str], object]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    # Spawn the in-repo server via the same Python interpreter that runs pytest.
    # We prefer `python -m gemini_harness.mcp_server` over the installed script
    # so the test is hermetic to the workspace.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gemini_harness.mcp_server"],
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            # harness.audit is the cheapest to exercise — read-only.
            result = await session.call_tool(
                "harness.audit",
                {"project_path": project_path, "include_skills": False},
            )
            return names, result


def test_mcp_server_lists_five_tools(tmp_path: Path):
    """Spawn the server, list tools, and run harness.audit against a tmp project."""
    # Some environments lack the mcp stdio runtime deps; skip gracefully.
    pytest.importorskip("mcp")

    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    # minimal project — no harness yet
    (project_dir / "README.md").write_text("demo")

    names, result = asyncio.run(_list_and_call(str(project_dir)))
    expected = {
        "harness.audit",
        "harness.build",
        "harness.verify",
        "harness.evolve",
        "harness.run",
    }
    assert expected.issubset(set(names))

    # The runtime layer may not be wired yet — accept either:
    #   - isError=True with structuredContent.error_code == "INTERNAL"
    #   - isError=False (runtime wired and returned a real audit)
    structured = getattr(result, "structuredContent", None)
    is_error = bool(getattr(result, "isError", False))
    if is_error:
        assert structured is not None
        assert "error_code" in structured
    else:
        # If success, validate shape we contract in guide/mcp_tools.md §1.3
        assert isinstance(structured, dict)


def test_mcp_server_rejects_relative_project_path(tmp_path: Path):
    pytest.importorskip("mcp")

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def _run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "gemini_harness.mcp_server"],
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(
                    "harness.audit",
                    {"project_path": "relative/path"},
                )

    result = asyncio.run(_run())
    assert getattr(result, "isError", False) is True
    sc = getattr(result, "structuredContent", None)
    assert sc is not None
    assert sc["error_code"] == "INVALID_INPUT"

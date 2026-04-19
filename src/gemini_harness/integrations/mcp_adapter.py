"""Outbound MCP client wrapper.

Contract: `_workspace/guide/gemini_integration.md` §3.

This is the **client** side — code that calls into external MCP servers from
inside Worker nodes. The harness's *own* MCP server lives in
`gemini_harness.mcp_server`.

Each call opens a fresh `ClientSession` (no connection pooling in v1).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from gemini_harness.integrations._errors import (
    GeminiTimeoutError,
    McpConnectionError,
    McpProtocolError,
)
from gemini_harness.integrations._metrics import record_call
from gemini_harness.integrations._retry import retry_transient

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    transport: Literal["stdio", "http"]
    command: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None


@dataclass(frozen=True)
class McpToolResult:
    is_error: bool
    text: str | None
    structured: dict[str, Any] | None
    raw_content: list[dict[str, Any]] = field(default_factory=list)


def _validate_spec(server: McpServerSpec) -> None:
    if server.transport == "stdio":
        if not server.command:
            raise ValueError(f"MCP server {server.name!r}: stdio transport requires `command`")
    elif server.transport == "http":
        if not server.url:
            raise ValueError(f"MCP server {server.name!r}: http transport requires `url`")
    else:
        raise ValueError(f"MCP server {server.name!r}: unknown transport {server.transport!r}")


def _coerce_structured(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    # SDK may return a pydantic model; best-effort coerce
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            return dumped if isinstance(dumped, dict) else None
        except Exception:  # noqa: BLE001
            return None
    return None


def _extract_text_blocks(content: Any) -> tuple[str | None, list[dict[str, Any]]]:
    """Join TextContent blocks and dump the raw content list for QA."""
    raw: list[dict[str, Any]] = []
    texts: list[str] = []
    if not content:
        return None, raw
    for block in content:
        block_type = getattr(block, "type", None)
        block_text = getattr(block, "text", None)
        entry: dict[str, Any] = {"type": block_type}
        if block_text is not None:
            entry["text"] = block_text
            if block_type == "text":
                texts.append(block_text)
        raw.append(entry)
    joined = "\n".join(texts) if texts else None
    return joined, raw


async def _open_session(server: McpServerSpec):
    """Yield an initialized ClientSession for the given server.

    Returns an async context manager. Callers use:
        async with _open_session(server) as session: ...
    """
    _validate_spec(server)

    # Import locally so test environments without the full MCP transport
    # surface can still import this module for unit testing.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _ctx():
        if server.transport == "stdio":
            from mcp import ClientSession
            from mcp.client.stdio import StdioServerParameters, stdio_client

            cmd = list(server.command or [])
            if not cmd:
                raise McpConnectionError(f"{server.name}: empty stdio command")
            params = StdioServerParameters(
                command=cmd[0],
                args=cmd[1:],
                env=server.env,
            )
            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            except (OSError, FileNotFoundError, ConnectionError) as exc:
                raise McpConnectionError(
                    f"{server.name}: stdio connection failed: {exc}"
                ) from exc
        else:
            from mcp import ClientSession
            from mcp.client.streamable_http import streamablehttp_client

            try:
                async with streamablehttp_client(server.url) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session
            except (OSError, ConnectionError) as exc:
                raise McpConnectionError(
                    f"{server.name}: http connection failed: {exc}"
                ) from exc

    return _ctx()


@retry_transient()
async def call_mcp_tool(
    server: McpServerSpec,
    tool: str,
    args: dict[str, Any],
    *,
    timeout: float = 30.0,
    node: str = "unknown",
    run_id: str = "unknown",
) -> McpToolResult:
    """Call a single tool on an external MCP server.

    Retries on transient errors (timeout / 429-equivalent / 5xx-equivalent).
    `is_error` in the return value is a **domain** signal — the caller decides
    if it is fatal. Protocol errors raise `McpProtocolError`.
    """
    import asyncio

    _validate_spec(server)
    start_ns = time.monotonic_ns()
    outcome = "ok"
    error_kind: str | None = None
    is_error = False

    try:
        ctx = await _open_session(server)
        async with ctx as session:
            try:
                result = await asyncio.wait_for(
                    session.call_tool(tool, args),
                    timeout=timeout,
                )
            except asyncio.TimeoutError as exc:
                outcome = "error"
                error_kind = "GeminiTimeoutError"
                raise GeminiTimeoutError(
                    f"MCP {server.name}/{tool} exceeded timeout {timeout}s"
                ) from exc

        content = getattr(result, "content", None)
        text, raw = _extract_text_blocks(content)
        structured = _coerce_structured(getattr(result, "structuredContent", None))
        is_error = bool(getattr(result, "isError", False))

        return McpToolResult(
            is_error=is_error,
            text=text,
            structured=structured,
            raw_content=raw,
        )
    except McpConnectionError:
        outcome = "error"
        error_kind = "McpConnectionError"
        raise
    except (McpProtocolError, GeminiTimeoutError):
        outcome = "error"
        if error_kind is None:
            error_kind = "McpProtocolError"
        raise
    except Exception as exc:  # noqa: BLE001
        # Any other SDK-surfaced error is treated as a protocol error.
        outcome = "error"
        error_kind = "McpProtocolError"
        raise McpProtocolError(f"{server.name}/{tool}: {exc}") from exc
    finally:
        _emit_mcp_metric(
            server=server.name,
            tool=tool,
            node=node,
            run_id=run_id,
            outcome=outcome,
            error_kind=error_kind,
            is_error=is_error,
            latency_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
        )


async def list_mcp_tools(
    server: McpServerSpec,
    *,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Enumerate tools exposed by the server. Used at startup for validation."""
    import asyncio

    _validate_spec(server)
    try:
        ctx = await _open_session(server)
        async with ctx as session:
            try:
                result = await asyncio.wait_for(session.list_tools(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise GeminiTimeoutError(
                    f"MCP {server.name} list_tools timed out after {timeout}s"
                ) from exc
    except McpConnectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise McpProtocolError(f"{server.name} list_tools failed: {exc}") from exc

    tools = getattr(result, "tools", []) or []
    out: list[dict[str, Any]] = []
    for t in tools:
        out.append(
            {
                "name": getattr(t, "name", ""),
                "description": getattr(t, "description", "") or "",
                "inputSchema": getattr(t, "inputSchema", None) or {},
            }
        )
    return out


def _emit_mcp_metric(
    *,
    server: str,
    tool: str,
    node: str,
    run_id: str,
    outcome: str,
    is_error: bool,
    latency_ms: int,
    error_kind: str | None = None,
) -> None:
    record: dict[str, Any] = {
        "channel": "mcp",
        "node": node,
        "run_id": run_id,
        "outcome": outcome,
        "latency_ms": latency_ms,
        "server": server,
        "tool": tool,
        "is_error": is_error,
    }
    if error_kind is not None:
        record["error_kind"] = error_kind
    record_call(record)

"""`gemini-harness-mcp` — stdio MCP server exposing `harness.*` tools.

Contract: `_workspace/guide/mcp_tools.md`.

Runtime imports are performed **lazily** inside each handler so the server
module can be loaded (and tests can import it) before the LangGraph runtime
wiring is finalized by the parallel `langgraph-developer` effort.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable


def _autoload_dotenv() -> None:
    """Best-effort .env loading. No-op if python-dotenv missing."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    for path in (Path.cwd() / ".env",):
        if path.is_file():
            load_dotenv(path, override=False)
            break


_autoload_dotenv()

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from gemini_harness import __version__ as _VERSION

_log = logging.getLogger(__name__)

SERVER_NAME = "gemini-harness"

# ------------------------- JSON schemas -------------------------

_PROJECT_PATH = {
    "type": "string",
    "minLength": 1,
    "description": "Absolute path to the user's project root. Must exist and be writable.",
}
_RUN_ID = {
    "type": "string",
    "pattern": r"^[a-z0-9_-]{4,64}$",
    "description": "LangGraph checkpointer thread_id. Server generates one if omitted.",
}

_INPUT_SCHEMAS: dict[str, dict[str, Any]] = {
    "harness.audit": {
        "type": "object",
        "required": ["project_path"],
        "additionalProperties": False,
        "properties": {
            "project_path": _PROJECT_PATH,
            "include_skills": {"type": "boolean", "default": True},
            "include_history": {"type": "boolean", "default": False},
        },
    },
    "harness.build": {
        "type": "object",
        "required": ["project_path", "domain_description"],
        "additionalProperties": False,
        "properties": {
            "project_path": _PROJECT_PATH,
            "domain_description": {"type": "string", "minLength": 20},
            "run_id": _RUN_ID,
            "pattern_hint": {
                "anyOf": [
                    {
                        "enum": [
                            "pipeline",
                            "fan_out_fan_in",
                            "expert_pool",
                            "producer_reviewer",
                            "supervisor",
                            "hierarchical",
                        ]
                    },
                    {"type": "string", "pattern": r"^([a-z_]+)(\+[a-z_]+)+$"},
                ]
            },
            "max_agents": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8},
            "tool_executor": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "max_tool_iterations": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "default": 5,
                    },
                    "allowed_tools": {"type": "array", "items": {"type": "string"}},
                    "tool_timeout_s": {
                        "type": "number",
                        "minimum": 1,
                        "maximum": 300,
                        "default": 30,
                    },
                },
            },
            "force": {"type": "boolean", "default": False},
        },
    },
    "harness.verify": {
        "type": "object",
        "required": ["project_path"],
        "additionalProperties": False,
        "properties": {
            "project_path": _PROJECT_PATH,
            "checks": {
                "type": "array",
                "default": ["schema", "triggers", "dry_run"],
                "items": {"enum": ["schema", "triggers", "dry_run", "self_critique_ab"]},
            },
            "dry_run_input": {"type": "string"},
            "ab_baseline_run_id": {"type": "string"},
        },
    },
    "harness.evolve": {
        "type": "object",
        "required": ["project_path", "feedback"],
        "additionalProperties": False,
        "properties": {
            "project_path": _PROJECT_PATH,
            "feedback": {"type": "string", "minLength": 10},
            "scope": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["kind"],
                    "properties": {
                        "kind": {
                            "enum": ["agent", "skill", "routing_config", "workflow_field"]
                        },
                        "id": {"type": "string"},
                    },
                },
            },
            "dry_run": {"type": "boolean", "default": False},
        },
    },
    "harness.run": {
        "type": "object",
        "required": ["project_path", "user_input"],
        "additionalProperties": False,
        "properties": {
            "project_path": _PROJECT_PATH,
            "user_input": {"type": "string", "minLength": 1},
            "run_id": _RUN_ID,
            "resume": {"type": "boolean", "default": False},
            "step_limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 1000,
                "default": 200,
            },
            "stream": {"type": "boolean", "default": False},
        },
    },
}

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "harness.audit": (
        "Scan an existing project for a previously generated harness and report drift "
        "between workflow.json and filesystem state. Read-only."
    ),
    "harness.build": (
        "Run Phases 1–5 end-to-end: domain analysis, pattern selection, agent/skill "
        "generation, orchestrator + workflow.json emission."
    ),
    "harness.verify": (
        "Phase 6 — structural, trigger, and dry-run validation of the generated harness. "
        "Writes under _workspace/qa/ only."
    ),
    "harness.evolve": (
        "Phase 7 — targeted adjustments based on user feedback. Never wholesale regenerates."
    ),
    "harness.run": (
        "Execute the generated orchestrator with a user input. Loads workflow.json from disk "
        "and drives the LangGraph runtime."
    ),
}


# ------------------------- Common errors -------------------------


def _make_error_payload(
    error_code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    remediation: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"error_code": error_code, "message": message}
    if details is not None:
        payload["details"] = details
    if remediation is not None:
        payload["remediation"] = remediation
    return payload


def _build_error_result(
    error_code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    remediation: str | None = None,
) -> types.CallToolResult:
    payload = _make_error_payload(
        error_code, message, details=details, remediation=remediation
    )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"[{error_code}] {message}")],
        structuredContent=payload,
        isError=True,
    )


def _validate_project_path(path: str) -> tuple[bool, types.CallToolResult | None]:
    if not os.path.isabs(path):
        return False, _build_error_result(
            "INVALID_INPUT",
            f"project_path must be absolute, got {path!r}",
        )
    if not os.path.exists(path):
        return False, _build_error_result(
            "PROJECT_NOT_FOUND",
            f"project_path does not exist: {path}",
        )
    if not os.access(path, os.W_OK):
        return False, _build_error_result(
            "INVALID_INPUT",
            f"project_path is not writable: {path}",
            remediation="Ensure the directory is writable by the current user.",
        )
    return True, None


# ------------------------- Lazy runtime dispatch -------------------------


def _load_runtime_fn(attr: str) -> Callable[..., Any]:
    """Import `gemini_harness.runtime.harness_runtime.<attr>` on demand.

    This function is called per-request (cheap — Python caches `sys.modules`),
    which lets the runtime module come online after the MCP server module is
    first imported during tests.
    """
    from gemini_harness.runtime import harness_runtime  # type: ignore[attr-defined]

    fn = getattr(harness_runtime, attr, None)
    if fn is None or not callable(fn):
        raise RuntimeError(
            f"gemini_harness.runtime.harness_runtime.{attr} not available — "
            "runtime layer not wired yet."
        )
    return fn


async def _call_runtime(attr: str, **kwargs: Any) -> Any:
    """Invoke a runtime entry point, awaiting if it is a coroutine."""
    fn = _load_runtime_fn(attr)
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


def _invoke_workflow_linter(workflow_path: Path) -> dict[str, Any]:
    """Run `meta.lint_workflow` against a workflow.json on disk.

    Returns `{passed: bool, failures: list[dict], skipped: bool}`. If the
    meta package is unavailable, returns `skipped=True` and `passed=True`
    so the MCP server degrades gracefully during bootstrap.
    """
    try:
        from gemini_harness.meta import lint_workflow
    except ImportError:
        return {"passed": True, "failures": [], "skipped": True}

    try:
        data = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "passed": False,
            "failures": [
                {
                    "code": "workflow_parse_error",
                    "message": f"failed to read/parse {workflow_path.name}: {exc}",
                }
            ],
            "skipped": False,
        }

    try:
        result = lint_workflow(data)
    except Exception as exc:  # noqa: BLE001
        return {
            "passed": False,
            "failures": [{"code": "linter_exception", "message": str(exc)}],
            "skipped": False,
        }

    failures = []
    for f in getattr(result, "failures", []) or []:
        if hasattr(f, "code") or hasattr(f, "message"):
            failures.append(
                {
                    "code": getattr(f, "code", "unknown"),
                    "message": getattr(f, "message", str(f)),
                    "path": getattr(f, "path", None),
                }
            )
        else:
            failures.append({"code": "unknown", "message": str(f)})
    return {"passed": bool(getattr(result, "passed", False)), "failures": failures, "skipped": False}


# ------------------------- Server definition -------------------------


def _build_server() -> Server:
    """Construct the MCP server with handlers wired."""
    server: Server = Server(SERVER_NAME)

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name=name,
                description=_TOOL_DESCRIPTIONS[name],
                inputSchema=_INPUT_SCHEMAS[name],
            )
            for name in (
                "harness.audit",
                "harness.build",
                "harness.verify",
                "harness.evolve",
                "harness.run",
            )
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        handler = _HANDLERS.get(name)
        if handler is None:
            return _build_error_result(
                "INVALID_INPUT", f"unknown tool {name!r}"
            )
        try:
            return await handler(arguments or {})
        except Exception as exc:  # noqa: BLE001
            _log.exception("tool handler %s crashed", name)
            return _build_error_result(
                "INTERNAL",
                f"tool handler {name} crashed: {exc}",
                details={"exception_type": type(exc).__name__},
            )

    return server


# ------------------------- Tool handlers -------------------------


async def _handle_audit(arguments: dict[str, Any]) -> types.CallToolResult:
    project_path = arguments.get("project_path", "")
    ok, err = _validate_project_path(project_path)
    if not ok:
        assert err is not None
        return err
    try:
        result = await _call_runtime(
            "run_audit",
            project_path=project_path,
            include_skills=bool(arguments.get("include_skills", True)),
            include_history=bool(arguments.get("include_history", False)),
        )
    except RuntimeError as exc:
        return _build_error_result(
            "INTERNAL",
            str(exc),
            remediation="runtime layer not yet wired; try again after task #1 lands",
        )
    return _ok_result(result)


async def _handle_build(arguments: dict[str, Any]) -> types.CallToolResult:
    project_path = arguments.get("project_path", "")
    ok, err = _validate_project_path(project_path)
    if not ok:
        assert err is not None
        return err

    domain = arguments.get("domain_description", "") or ""
    if len(domain) < 20:
        return _build_error_result(
            "INVALID_INPUT",
            "domain_description must be at least 20 characters",
        )

    # Path-traversal guard — the linter's lint_agent is called by meta/linter; here
    # we reject obvious traversal attempts in project_path before touching runtime.
    if ".." in Path(project_path).parts:
        return _build_error_result(
            "INVALID_INPUT",
            "project_path must not contain '..' segments",
        )

    # Pre-flight workflow lint (if the generated workflow already exists).
    workflow_path = Path(project_path) / "workflow.json"
    if workflow_path.exists() and not arguments.get("force", False):
        return _build_error_result(
            "HARNESS_NOT_INITIALIZED",
            "harness already exists; pass force=true to overwrite",
            remediation="Set `force: true` or delete workflow.json first.",
        )

    if workflow_path.exists():
        lint_result = _invoke_workflow_linter(workflow_path)
        if not lint_result["passed"]:
            return _build_error_result(
                "LINTER_REJECTED",
                "existing workflow.json failed lint pre-flight",
                details={"failures": lint_result["failures"]},
            )

    try:
        result = await _call_runtime(
            "run_build",
            project_path=project_path,
            domain_description=domain,
            run_id=arguments.get("run_id"),
            pattern_hint=arguments.get("pattern_hint"),
            max_agents=int(arguments.get("max_agents", 8)),
            tool_executor=arguments.get("tool_executor"),
            force=bool(arguments.get("force", False)),
        )
    except RuntimeError as exc:
        return _build_error_result(
            "INTERNAL",
            str(exc),
            remediation="runtime layer not yet wired; try again after task #1 lands",
        )
    return _ok_result(result)


async def _handle_verify(arguments: dict[str, Any]) -> types.CallToolResult:
    project_path = arguments.get("project_path", "")
    ok, err = _validate_project_path(project_path)
    if not ok:
        assert err is not None
        return err

    checks = arguments.get("checks") or ["schema", "triggers", "dry_run"]
    if "dry_run" in checks and not arguments.get("dry_run_input"):
        return _build_error_result(
            "INVALID_INPUT",
            "dry_run requires dry_run_input",
        )
    if "self_critique_ab" in checks and not arguments.get("ab_baseline_run_id"):
        return _build_error_result(
            "INVALID_INPUT",
            "self_critique_ab requires ab_baseline_run_id",
        )

    try:
        result = await _call_runtime(
            "run_verify",
            project_path=project_path,
            checks=list(checks),
            dry_run_input=arguments.get("dry_run_input"),
            ab_baseline_run_id=arguments.get("ab_baseline_run_id"),
        )
    except RuntimeError as exc:
        return _build_error_result(
            "INTERNAL",
            str(exc),
            remediation="runtime layer not yet wired; try again after task #1 lands",
        )
    return _ok_result(result)


async def _handle_evolve(arguments: dict[str, Any]) -> types.CallToolResult:
    project_path = arguments.get("project_path", "")
    ok, err = _validate_project_path(project_path)
    if not ok:
        assert err is not None
        return err

    feedback = arguments.get("feedback", "") or ""
    if len(feedback) < 10:
        return _build_error_result(
            "INVALID_INPUT",
            "feedback must be at least 10 characters",
        )

    if not (Path(project_path) / "workflow.json").exists():
        return _build_error_result(
            "HARNESS_NOT_INITIALIZED",
            "no workflow.json at project_path",
        )

    try:
        result = await _call_runtime(
            "run_evolve",
            project_path=project_path,
            feedback=feedback,
            scope=arguments.get("scope") or [],
            dry_run=bool(arguments.get("dry_run", False)),
        )
    except RuntimeError as exc:
        return _build_error_result(
            "INTERNAL",
            str(exc),
            remediation="runtime layer not yet wired; try again after task #1 lands",
        )
    return _ok_result(result)


async def _handle_run(arguments: dict[str, Any]) -> types.CallToolResult:
    project_path = arguments.get("project_path", "")
    ok, err = _validate_project_path(project_path)
    if not ok:
        assert err is not None
        return err

    user_input = arguments.get("user_input", "") or ""
    if not user_input:
        return _build_error_result("INVALID_INPUT", "user_input must be non-empty")

    if not (Path(project_path) / "workflow.json").exists():
        return _build_error_result(
            "HARNESS_NOT_INITIALIZED",
            "no workflow.json at project_path",
        )

    try:
        result = await _call_runtime(
            "run_harness",
            project_path=project_path,
            user_input=user_input,
            run_id=arguments.get("run_id"),
            resume=bool(arguments.get("resume", False)),
            step_limit=int(arguments.get("step_limit", 200)),
        )
    except RuntimeError as exc:
        return _build_error_result(
            "INTERNAL",
            str(exc),
            remediation="runtime layer not yet wired; try again after task #1 lands",
        )
    return _ok_result(result)


_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[types.CallToolResult]]] = {
    "harness.audit": _handle_audit,
    "harness.build": _handle_build,
    "harness.verify": _handle_verify,
    "harness.evolve": _handle_evolve,
    "harness.run": _handle_run,
}


def _ok_result(payload: Any) -> types.CallToolResult:
    structured = payload if isinstance(payload, dict) else {"result": payload}
    summary = json.dumps(structured, ensure_ascii=False, indent=2, default=str)
    if len(summary) > 4000:
        summary = summary[:4000] + "\n... (truncated)"
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=summary)],
        structuredContent=structured,
        isError=False,
    )


# ------------------------- Entry points -------------------------


async def _run_stdio() -> None:
    server = _build_server()
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(
            read,
            write,
            InitializationOptions(
                server_name=SERVER_NAME,
                server_version=_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main(argv: list[str] | None = None) -> int:
    """`gemini-harness-mcp` entry point."""
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_run_stdio())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

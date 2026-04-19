"""Built-in tools — the LAST-RESORT fallback.

Tool-access priority inside a harness run:

  1. Gemini CLI's native tools (file-manager, google-search, write_todos, …)
     are invoked by Gemini CLI itself BEFORE ``harness.run`` is called.
     The ``/harness:run`` slash-command prompt instructs Gemini CLI to
     pre-collect any files / search results mentioned in the user input
     and pack them into ``user_input`` or the seed artifacts. This reuses
     the user's validated tool stack — nothing duplicated.

  2. User-registered MCP servers (github, slack, db, …) are discovered
     from ``~/.gemini/settings.json`` (see ``tool_discovery.py``) and
     proxied by our ``tool_executor`` via the ``mcp:<server>/<tool>``
     transport. Agents can declare e.g. ``tools: ["mcp:github/search_issues"]``
     and our server spawns the same MCP subprocess + speaks client to it.

  3. **This module** — a minimal set of sandboxed Python helpers
     (``read_file``, ``list_files``, ``glob_files``) is used ONLY when
     paths above are unavailable. Example: the user did not pre-collect
     a file and has no filesystem MCP server installed, but an agent
     still needs to read disk mid-run. Kept deliberately small so we
     don't reinvent the Gemini CLI ecosystem.

  4. Missing capability? meta-agent generates a new agent / skill on
     the fly (``create_agents`` / ``create_skills`` in Worker response).

Each built-in tool is exposed to agents as a Gemini ``ToolDecl`` and
dispatched by ``tool_executor`` via the ``builtin:<name>`` transport.
``resolve_safe`` (ADR 0004) enforces the sandbox so nothing escapes the
project root.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .sandbox import SandboxViolation, resolve_safe


MAX_READ_BYTES = 256 * 1024   # 256 KiB per read
MAX_LIST_ENTRIES = 500
MAX_GLOB_RESULTS = 500


def _read_file(root: Path, *, path: str, max_bytes: int | None = None) -> dict[str, Any]:
    target = resolve_safe(root, path)
    if not target.exists():
        return {"ok": False, "error": f"file not found: {path}"}
    if not target.is_file():
        return {"ok": False, "error": f"not a regular file: {path}"}
    cap = int(max_bytes or MAX_READ_BYTES)
    cap = min(cap, MAX_READ_BYTES)
    try:
        data = target.read_bytes()
    except OSError as exc:
        return {"ok": False, "error": f"read error: {exc}"}
    truncated = len(data) > cap
    if truncated:
        data = data[:cap]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return {
            "ok": False,
            "error": "binary file (not UTF-8 decodable)",
            "size_bytes": len(data),
        }
    return {
        "ok": True,
        "path": str(target.relative_to(root)),
        "content": text,
        "size_bytes": len(data),
        "truncated": truncated,
    }


def _list_files(root: Path, *, path: str = ".", include_hidden: bool = False) -> dict[str, Any]:
    target = resolve_safe(root, path)
    if not target.exists():
        return {"ok": False, "error": f"path not found: {path}"}
    if not target.is_dir():
        return {"ok": False, "error": f"not a directory: {path}"}
    try:
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return {"ok": False, "error": f"list error: {exc}"}
    out: list[dict[str, Any]] = []
    for child in entries:
        if not include_hidden and child.name.startswith("."):
            continue
        try:
            rel = str(child.relative_to(root))
        except ValueError:
            rel = child.name
        out.append(
            {
                "name": child.name,
                "path": rel,
                "kind": "dir" if child.is_dir() else "file",
                "size": child.stat().st_size if child.is_file() else None,
            }
        )
        if len(out) >= MAX_LIST_ENTRIES:
            break
    return {"ok": True, "path": str(target.relative_to(root)), "entries": out}


def _glob_files(root: Path, *, pattern: str) -> dict[str, Any]:
    if not pattern or ".." in pattern:
        return {"ok": False, "error": "glob pattern must not contain '..'"}
    matches: list[str] = []
    for match in root.glob(pattern):
        try:
            rel = str(match.resolve().relative_to(root))
        except (ValueError, OSError):
            continue
        matches.append(rel)
        if len(matches) >= MAX_GLOB_RESULTS:
            break
    matches.sort()
    return {"ok": True, "pattern": pattern, "matches": matches, "count": len(matches)}


@dataclass(frozen=True)
class BuiltinToolDef:
    """One built-in tool exposed to agents. Combines the Gemini function
    declaration (schema + description) with the Python callable that
    ``tool_executor`` dispatches when the model emits a matching tool_call."""

    name: str
    description: str
    parameters_json_schema: dict[str, Any]
    invoke: Callable[..., dict[str, Any]]
    # Which agent.tools labels trigger this tool being exposed.
    triggered_by: tuple[str, ...]


BUILTIN_TOOLS: list[BuiltinToolDef] = [
    BuiltinToolDef(
        name="read_file",
        description=(
            "Read a text file from the project. Use this whenever you need the "
            "actual contents of a file to reason about it — do not guess. "
            "Paths are resolved against the project root and restricted to "
            "the sandbox (`.agents/`, `_workspace/`, `.gemini/`, and other "
            "project files)."
        ),
        parameters_json_schema={
            "type": "object",
            "required": ["path"],
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project-relative path to the file.",
                },
                "max_bytes": {
                    "type": "integer",
                    "description": f"Optional byte cap (default {MAX_READ_BYTES}).",
                    "minimum": 1,
                    "maximum": MAX_READ_BYTES,
                },
            },
        },
        invoke=_read_file,
        triggered_by=("file-manager", "read-file", "builtin:read_file"),
    ),
    BuiltinToolDef(
        name="list_files",
        description=(
            "List entries in a project directory. Use this to discover what "
            "files exist before reading them. Returns names, paths, and "
            "file/dir kind."
        ),
        parameters_json_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Project-relative directory (default: project root).",
                },
                "include_hidden": {
                    "type": "boolean",
                    "description": "Include dotfiles. Default false.",
                },
            },
        },
        invoke=_list_files,
        triggered_by=("file-manager", "list-files", "builtin:list_files"),
    ),
    BuiltinToolDef(
        name="glob_files",
        description=(
            "Find project files matching a glob pattern. Use this to locate "
            "files by wildcard — e.g. `src/**/*.py` or `**/test_*.py`."
        ),
        parameters_json_schema={
            "type": "object",
            "required": ["pattern"],
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern relative to project root. "
                    "Recursive: use `**/`.",
                }
            },
        },
        invoke=_glob_files,
        triggered_by=("file-manager", "glob", "builtin:glob_files"),
    ),
]


def builtins_by_name() -> dict[str, BuiltinToolDef]:
    return {t.name: t for t in BUILTIN_TOOLS}


def select_builtins_for_agent(agent_tools: list[str] | tuple[str, ...] | None) -> list[BuiltinToolDef]:
    """Return the built-in tools an agent has access to, based on its
    declared tool labels. The matching is fuzzy by `triggered_by`: any
    label that appears in a tool's `triggered_by` tuple activates it.

    Example: ``agent.tools = ["file-manager"]`` activates all 3 filesystem
    helpers; ``agent.tools = ["read-file"]`` activates only ``read_file``.
    """
    requested = {str(t).strip() for t in (agent_tools or [])}
    if not requested:
        return []
    active: list[BuiltinToolDef] = []
    for tool in BUILTIN_TOOLS:
        if any(label in requested for label in tool.triggered_by):
            active.append(tool)
    return active


__all__ = [
    "BuiltinToolDef",
    "BUILTIN_TOOLS",
    "builtins_by_name",
    "select_builtins_for_agent",
]

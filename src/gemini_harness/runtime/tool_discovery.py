"""Discover external MCP servers the user has configured in Gemini CLI.

Gemini CLI reads ``$HOME/.gemini/settings.json`` and the project-local
``./.gemini/settings.json`` (merged, project overriding). Both may contain
an ``mcpServers`` block:

    {
      "mcpServers": {
        "github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]},
        "slack":  {"command": "node", "args": ["./mcp/slack.js"]}
      }
    }

When a harness runs inside that project we can read the same files and
proxy the declared servers so our agents can call their tools via the
existing ``mcp:<server>/<tool>`` transport.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DiscoveredMcpServer:
    name: str
    command: list[str]
    env: dict[str, str]
    source: str  # "user" | "project"


def _load_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _extract_servers(data: dict[str, Any], source: str) -> list[DiscoveredMcpServer]:
    mcp = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(mcp, dict):
        return []
    out: list[DiscoveredMcpServer] = []
    for name, spec in mcp.items():
        if not isinstance(spec, dict):
            continue
        command = spec.get("command")
        args = spec.get("args") or []
        if not isinstance(command, str):
            continue
        if not isinstance(args, list):
            args = []
        env = spec.get("env") or {}
        if not isinstance(env, dict):
            env = {}
        out.append(
            DiscoveredMcpServer(
                name=name,
                command=[command, *[str(a) for a in args]],
                env={str(k): str(v) for k, v in env.items()},
                source=source,
            )
        )
    return out


def discover_mcp_servers(project_root: Path | str) -> dict[str, DiscoveredMcpServer]:
    """Merged view of user + project MCP servers. Project wins on name collision.

    Skips the ``gemini-harness`` server itself (recursion / self-reference).
    """
    project_root = Path(project_root).resolve()
    user_cfg = Path(os.environ.get("GEMINI_SETTINGS_PATH") or (Path.home() / ".gemini" / "settings.json"))
    proj_cfg = project_root / ".gemini" / "settings.json"

    result: dict[str, DiscoveredMcpServer] = {}
    for srv in _extract_servers(_load_settings(user_cfg), "user"):
        result[srv.name] = srv
    for srv in _extract_servers(_load_settings(proj_cfg), "project"):
        result[srv.name] = srv

    result.pop("gemini-harness", None)
    result.pop("harness", None)  # avoid dispatching back into ourselves
    return result


__all__ = ["DiscoveredMcpServer", "discover_mcp_servers"]

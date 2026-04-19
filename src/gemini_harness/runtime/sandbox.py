"""Sandbox path validation (ADR 0004).

All disk writes from the runtime (SYSTEM_PROMPT files, skills, artifacts) must
resolve under one of the allowed roots, relative to the repo root passed in.
Path-traversal (``..``) and absolute paths outside the sandbox are rejected.
"""
from __future__ import annotations

from pathlib import Path

ALLOWED_ROOTS = (".agents", "_workspace", ".gemini")


class SandboxViolation(ValueError):  # noqa: N818 — domain term per ADR 0004
    pass


def resolve_safe(repo_root: str | Path, rel_path: str) -> Path:
    """Return the absolute path iff ``rel_path`` lives under an allowed root.

    Raises ``SandboxViolation`` otherwise. ``.agents/skills/`` is covered by the
    ``.agents`` root.
    """
    if not isinstance(rel_path, str) or not rel_path:
        raise SandboxViolation(f"empty path: {rel_path!r}")
    if rel_path.startswith("/"):
        raise SandboxViolation(f"absolute path forbidden: {rel_path!r}")

    root = Path(repo_root).resolve()
    target = (root / rel_path).resolve()

    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SandboxViolation(f"path escapes repo: {rel_path!r}") from exc

    relative = target.relative_to(root)
    parts = relative.parts
    if not parts or parts[0] not in ALLOWED_ROOTS:
        raise SandboxViolation(
            f"path {rel_path!r} is not under an allowed root {ALLOWED_ROOTS}"
        )
    return target


def is_safe(repo_root: str | Path, rel_path: str) -> bool:
    try:
        resolve_safe(repo_root, rel_path)
        return True
    except SandboxViolation:
        return False


__all__ = ["ALLOWED_ROOTS", "SandboxViolation", "is_safe", "resolve_safe"]

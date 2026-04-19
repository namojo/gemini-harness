"""Gemini CLI subprocess wrapper.

Contract: `_workspace/guide/gemini_integration.md` §2.

Security invariants (enforced):
  - `subprocess.run(..., shell=False)` always.
  - `args` must be `list[str]`; single-string args raise `TypeError`.
  - User-supplied values land as list elements only. Never f-string into a command.
  - `env` is merged onto a copy of `os.environ`; we never mutate the parent env.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from packaging.version import InvalidVersion, Version

from gemini_harness.integrations._errors import GeminiCliError, GeminiCliVersionError
from gemini_harness.integrations._metrics import record_call

_log = logging.getLogger(__name__)

MIN_GEMINI_CLI_VERSION = "0.28.0"

_VERSION_CACHE: dict[str, str] = {}


@dataclass(frozen=True)
class CliResult:
    stdout: str
    stderr: str
    returncode: int
    duration_ms: int


def _require_list_of_str(name: str, value: Any) -> list[str]:
    if isinstance(value, str) or not hasattr(value, "__iter__"):
        raise TypeError(
            f"{name} must be list[str]; got {type(value).__name__}. "
            "Never pass a pre-joined command string to cli_bridge."
        )
    out: list[str] = []
    for i, v in enumerate(value):
        if not isinstance(v, str):
            raise TypeError(f"{name}[{i}] must be str; got {type(v).__name__}")
        out.append(v)
    return out


def check_gemini_cli(min_version: str = MIN_GEMINI_CLI_VERSION) -> str:
    """Run `gemini --version` once per process, raise if below `min_version`.

    Returns the parsed version string on success. Cached per min_version.
    """
    if min_version in _VERSION_CACHE:
        return _VERSION_CACHE[min_version]

    try:
        result = subprocess.run(
            ["gemini", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise GeminiCliVersionError(
            "gemini CLI not found on PATH. Install with: "
            "npm install -g @google/gemini-cli@latest"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise GeminiCliVersionError("`gemini --version` timed out after 5s") from exc

    raw = (result.stdout or "").strip().splitlines()
    if not raw:
        raise GeminiCliVersionError(
            f"`gemini --version` produced no output (exit={result.returncode}, stderr={result.stderr!r})"
        )
    version_str = raw[0].lstrip("v").split()[0]

    try:
        current = Version(version_str)
        required = Version(min_version)
    except InvalidVersion as exc:
        raise GeminiCliVersionError(
            f"unparseable gemini version {version_str!r}: {exc}"
        ) from exc

    if current < required:
        raise GeminiCliVersionError(
            f"Gemini CLI >= {min_version} required, got {version_str}. "
            "Upgrade: npm install -g @google/gemini-cli@latest"
        )

    _VERSION_CACHE[min_version] = version_str
    return version_str


def _reset_version_cache() -> None:
    """Test hook — clear the per-process version cache."""
    _VERSION_CACHE.clear()


def invoke_cli_skill(
    skill: str,
    args: list[str],
    *,
    cwd: str = ".",
    timeout: int = 60,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    node: str = "unknown",
    run_id: str = "unknown",
) -> CliResult:
    """Invoke `gemini skill <skill> <args...>`. shell=False, no retry."""
    if not isinstance(skill, str) or not skill:
        raise TypeError("skill must be a non-empty str")
    safe_args = _require_list_of_str("args", args)

    cmd = ["gemini", "skill", skill, *safe_args]
    return _run(
        cmd,
        cwd=cwd,
        timeout=timeout,
        env=env,
        input_text=input_text,
        channel_label=skill,
        node=node,
        run_id=run_id,
    )


def invoke_cli_extension(
    extension: str,
    subcommand: str,
    args: list[str],
    *,
    cwd: str = ".",
    timeout: int = 60,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    node: str = "unknown",
    run_id: str = "unknown",
) -> CliResult:
    """Invoke `gemini extensions run <extension> <subcommand> <args...>`.

    The subcommand form is the assumed shape from the gemini-cli-extension-packaging
    skill; implementation must verify against `gemini extensions --help` in
    the target environment. (Contract §2.3.)
    """
    if not isinstance(extension, str) or not extension:
        raise TypeError("extension must be a non-empty str")
    if not isinstance(subcommand, str) or not subcommand:
        raise TypeError("subcommand must be a non-empty str")
    safe_args = _require_list_of_str("args", args)

    cmd = ["gemini", "extensions", "run", extension, subcommand, *safe_args]
    return _run(
        cmd,
        cwd=cwd,
        timeout=timeout,
        env=env,
        input_text=input_text,
        channel_label=f"{extension}:{subcommand}",
        node=node,
        run_id=run_id,
    )


def _run(
    cmd: list[str],
    *,
    cwd: str,
    timeout: int,
    env: dict[str, str] | None,
    input_text: str | None,
    channel_label: str,
    node: str,
    run_id: str,
) -> CliResult:
    merged_env: dict[str, str] | None
    if env is None:
        merged_env = None
    else:
        merged_env = os.environ.copy()
        merged_env.update(env)

    start_ns = time.monotonic_ns()
    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env=merged_env,
            input=input_text,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        _emit_cli_metric(
            skill=channel_label,
            node=node,
            run_id=run_id,
            outcome="error",
            error_kind="GeminiCliError",
            exit_code=-1,
            latency_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
        )
        raise GeminiCliError(f"gemini binary not found: {exc}", returncode=-1) from exc
    except subprocess.TimeoutExpired as exc:
        _emit_cli_metric(
            skill=channel_label,
            node=node,
            run_id=run_id,
            outcome="error",
            error_kind="GeminiCliError",
            exit_code=-1,
            latency_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
        )
        raise GeminiCliError(
            f"gemini CLI timed out after {timeout}s: {cmd[:3]}",
            returncode=-1,
        ) from exc

    duration_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)

    if completed.returncode != 0:
        _emit_cli_metric(
            skill=channel_label,
            node=node,
            run_id=run_id,
            outcome="error",
            error_kind="GeminiCliError",
            exit_code=completed.returncode,
            latency_ms=duration_ms,
        )
        raise GeminiCliError(
            f"gemini CLI exited {completed.returncode}: "
            f"cmd={cmd[:3]}... stderr={completed.stderr!r}",
            returncode=completed.returncode,
            stderr=completed.stderr,
        )

    _emit_cli_metric(
        skill=channel_label,
        node=node,
        run_id=run_id,
        outcome="ok",
        exit_code=0,
        latency_ms=duration_ms,
    )

    return CliResult(
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=completed.returncode,
        duration_ms=duration_ms,
    )


def _emit_cli_metric(
    *,
    skill: str,
    node: str,
    run_id: str,
    outcome: str,
    exit_code: int,
    latency_ms: int,
    error_kind: str | None = None,
) -> None:
    record: dict[str, Any] = {
        "channel": "cli",
        "node": node,
        "run_id": run_id,
        "outcome": outcome,
        "latency_ms": latency_ms,
        "skill": skill,
        "exit_code": exit_code,
    }
    if error_kind is not None:
        record["error_kind"] = error_kind
    record_call(record)

"""`gemini-harness` CLI entry point.

Subcommands mirror the 5 MCP tools. The CLI is a thin argparse layer —
it delegates to `gemini_harness.runtime.harness_runtime.run_*`, the same
entry points the MCP server uses.

At startup we enforce `gemini --version >= 0.28.0` via `cli_bridge.check_gemini_cli`
so users with an old CLI see a clear upgrade message before anything else runs.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Callable


def _autoload_dotenv() -> None:
    """Best-effort .env loading. No-op if python-dotenv missing."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    candidates = [Path.cwd() / ".env"]
    project_override = os.environ.get("LANGCHAIN_HARNESS_WORKSPACE")
    if project_override:
        candidates.append(Path(project_override) / ".env")
    for path in candidates:
        if path.is_file():
            load_dotenv(path, override=False)
            break


_autoload_dotenv()

from gemini_harness import __version__ as _VERSION
from gemini_harness.integrations.cli_bridge import (
    MIN_GEMINI_CLI_VERSION,
    check_gemini_cli,
)
from gemini_harness.integrations._errors import GeminiCliVersionError

_log = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gemini-harness", description=f"gemini-harness {_VERSION}")
    p.add_argument("--version", action="version", version=f"gemini-harness {_VERSION}")
    p.add_argument(
        "--skip-cli-version-check",
        action="store_true",
        help="Skip the `gemini --version` pre-flight check (dev / CI only).",
    )
    p.add_argument(
        "--cli-ext",
        action="store_true",
        help="Internal: invoked from Gemini CLI extension entry.",
    )
    p.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v, -vv).",
    )

    sub = p.add_subparsers(dest="cmd")

    p_audit = sub.add_parser("audit", help="Scan project for an existing harness (Phase 0).")
    p_audit.add_argument("--project", required=True, help="Absolute path to project root")
    p_audit.add_argument("--include-history", action="store_true")
    p_audit.add_argument("--no-skills", action="store_true", help="Skip skill scan")

    p_build = sub.add_parser("build", help="Generate a harness for this project (Phases 1–5).")
    p_build.add_argument("--project", required=True)
    p_build.add_argument("--domain", required=True, help="Natural-language domain description")
    p_build.add_argument("--run-id", default=None)
    p_build.add_argument("--pattern", default=None, help="Optional pattern override")
    p_build.add_argument("--max-agents", type=int, default=8)
    p_build.add_argument("--force", action="store_true")

    p_verify = sub.add_parser("verify", help="Verify the generated harness (Phase 6).")
    p_verify.add_argument("--project", required=True)
    p_verify.add_argument(
        "--check",
        action="append",
        dest="checks",
        default=None,
        help="One of: schema, triggers, dry_run, self_critique_ab (repeatable).",
    )
    p_verify.add_argument("--dry-run-input", default=None)
    p_verify.add_argument("--ab-baseline", default=None)

    p_evolve = sub.add_parser("evolve", help="Adjust an existing harness (Phase 7).")
    p_evolve.add_argument("--project", required=True)
    p_evolve.add_argument("--feedback", required=True)
    p_evolve.add_argument("--dry-run", action="store_true")

    p_run = sub.add_parser("run", help="Execute the generated orchestrator.")
    p_run.add_argument("--project", required=True)
    p_run.add_argument("--input", required=True, dest="user_input")
    p_run.add_argument("--run-id", default=None)
    p_run.add_argument("--resume", action="store_true")
    p_run.add_argument("--step-limit", type=int, default=200)

    return p


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _load_runtime_fn(attr: str) -> Callable[..., Any]:
    """Mirror of `mcp_server._load_runtime_fn` — imported lazily on each call."""
    from gemini_harness.runtime import harness_runtime  # type: ignore[attr-defined]

    fn = getattr(harness_runtime, attr, None)
    if fn is None or not callable(fn):
        raise RuntimeError(
            f"runtime entry `{attr}` unavailable. The runtime layer may not be "
            "installed yet (`gemini_harness.runtime.harness_runtime`). See task #1."
        )
    return fn


def _dispatch(attr: str, **kwargs: Any) -> Any:
    fn = _load_runtime_fn(attr)
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        result = asyncio.run(result)
    return result


def _emit(result: Any) -> None:
    if isinstance(result, (dict, list)):
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(result)


# ------------------------- Subcommand handlers -------------------------


def _cmd_audit(args: argparse.Namespace) -> int:
    result = _dispatch(
        "run_audit",
        project_path=args.project,
        include_skills=not args.no_skills,
        include_history=bool(args.include_history),
    )
    _emit(result)
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    result = _dispatch(
        "run_build",
        project_path=args.project,
        domain_description=args.domain,
        run_id=args.run_id,
        pattern_hint=args.pattern,
        max_agents=args.max_agents,
        tool_executor=None,
        force=bool(args.force),
    )
    _emit(result)
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    checks = args.checks or ["schema", "triggers", "dry_run"]
    result = _dispatch(
        "run_verify",
        project_path=args.project,
        checks=checks,
        dry_run_input=args.dry_run_input,
        ab_baseline_run_id=args.ab_baseline,
    )
    _emit(result)
    passed = bool(result.get("passed", False)) if isinstance(result, dict) else False
    return 0 if passed else 2


def _cmd_evolve(args: argparse.Namespace) -> int:
    result = _dispatch(
        "run_evolve",
        project_path=args.project,
        feedback=args.feedback,
        scope=[],
        dry_run=bool(args.dry_run),
    )
    _emit(result)
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    result = _dispatch(
        "run_harness",
        project_path=args.project,
        user_input=args.user_input,
        run_id=args.run_id,
        resume=bool(args.resume),
        step_limit=args.step_limit,
    )
    _emit(result)
    return 0


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "audit": _cmd_audit,
    "build": _cmd_build,
    "verify": _cmd_verify,
    "evolve": _cmd_evolve,
    "run": _cmd_run,
}


# ------------------------- Extension entry -------------------------


def extension_entry(context: dict[str, Any]) -> dict[str, Any]:
    """Hook called by the Gemini CLI extension runtime.

    `context` is expected to contain at least:
      - `utterance` or `user_utterance` — the phrase that matched a trigger
      - `project_path` — absolute path to the project under operation
      - optional `args` dict with pre-parsed fields

    The entry maps an utterance to a subcommand and dispatches the same
    runtime functions the CLI uses. Returns the runtime's JSON response
    unchanged so the extension host can render it.
    """
    utterance = (context.get("utterance") or context.get("user_utterance") or "").strip()
    project_path = context.get("project_path") or context.get("project") or "."
    extra = context.get("args") or {}

    subcommand = _dispatch_from_utterance(utterance)
    if subcommand == "audit":
        return _dispatch(
            "run_audit",
            project_path=project_path,
            include_skills=extra.get("include_skills", True),
            include_history=extra.get("include_history", False),
        )
    if subcommand == "build":
        if "domain_description" not in extra:
            return {
                "error_code": "INVALID_INPUT",
                "message": "domain_description required for 'build'",
            }
        return _dispatch(
            "run_build",
            project_path=project_path,
            domain_description=extra["domain_description"],
            run_id=extra.get("run_id"),
            pattern_hint=extra.get("pattern_hint"),
            max_agents=extra.get("max_agents", 8),
            tool_executor=extra.get("tool_executor"),
            force=bool(extra.get("force", False)),
        )
    if subcommand == "verify":
        return _dispatch(
            "run_verify",
            project_path=project_path,
            checks=extra.get("checks") or ["schema", "triggers", "dry_run"],
            dry_run_input=extra.get("dry_run_input"),
            ab_baseline_run_id=extra.get("ab_baseline_run_id"),
        )
    if subcommand == "evolve":
        feedback = extra.get("feedback") or utterance
        return _dispatch(
            "run_evolve",
            project_path=project_path,
            feedback=feedback,
            scope=extra.get("scope") or [],
            dry_run=bool(extra.get("dry_run", False)),
        )
    if subcommand == "run":
        if "user_input" not in extra:
            return {
                "error_code": "INVALID_INPUT",
                "message": "user_input required for 'run'",
            }
        return _dispatch(
            "run_harness",
            project_path=project_path,
            user_input=extra["user_input"],
            run_id=extra.get("run_id"),
            resume=bool(extra.get("resume", False)),
            step_limit=extra.get("step_limit", 200),
        )
    return {
        "error_code": "INVALID_INPUT",
        "message": f"no subcommand matched utterance {utterance!r}",
    }


def _dispatch_from_utterance(utterance: str) -> str:
    """Map a trigger utterance to a subcommand name.

    Order matters — we check specific verbs before the generic "build" catch-all.
    """
    low = utterance.lower()
    if any(token in low for token in ("audit", "감사", "진단", "スキャン")):
        return "audit"
    if any(token in low for token in ("verify", "검증", "確認", "verif")):
        return "verify"
    if any(token in low for token in ("evolve", "진화", "改良", "adjust", "수정")):
        return "evolve"
    if any(token in low for token in ("run ", "실행", "run:", "execute", "実行")):
        return "run"
    # Default — "build" / "하네스 구성" / "ハーネスを構成"
    return "build"


# ------------------------- main -------------------------


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)

    if not args.skip_cli_version_check:
        try:
            check_gemini_cli(MIN_GEMINI_CLI_VERSION)
        except GeminiCliVersionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3

    if args.cli_ext:
        # Extension hook path — read context JSON from stdin.
        try:
            ctx = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError as exc:
            print(f"error: --cli-ext expects JSON on stdin: {exc}", file=sys.stderr)
            return 2
        result = extension_entry(ctx)
        _emit(result)
        return 0

    if not args.cmd:
        parser.print_help()
        return 1

    handler = _COMMANDS.get(args.cmd)
    if handler is None:
        parser.error(f"unknown command: {args.cmd}")
        return 1

    try:
        return handler(args)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

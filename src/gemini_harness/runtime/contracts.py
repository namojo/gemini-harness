"""Protocols for peer-owned contracts.

These mirror the dataclasses and functions promised by ``gemini-integrator`` and
``meta-skill-designer``. They exist so the runtime typechecks and tests run
before those implementations land.

When the real modules are available, callers inject the concrete functions/
clients; the Protocols define the narrow surface the runtime actually consumes.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolDecl:
    name: str
    description: str
    parameters_json_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCallDecl:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class UsageMetadata:
    prompt_token_count: int = 0
    candidates_token_count: int = 0
    cached_content_token_count: int = 0
    thoughts_token_count: int = 0
    tool_use_prompt_token_count: int = 0
    total_token_count: int = 0


@dataclass(frozen=True)
class GeminiResponseLike:
    text: str | None
    tool_calls: list[ToolCallDecl] = field(default_factory=list)
    usage: UsageMetadata = field(default_factory=UsageMetadata)
    finish_reason: str = "STOP"
    blocked_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


class GeminiClient(Protocol):
    """Subset of ``integrations/gemini_client.call_gemini`` used by Worker."""

    def __call__(
        self,
        prompt: str | Sequence[str],
        *,
        system: str | Sequence[str] | None = None,
        context: str | Sequence[str] = (),
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: Sequence[ToolDecl] | None = None,
        tool_choice: str = "auto",
        model: str = "gemini-3.1-pro-preview",
        node: str = "worker",
        run_id: str = "unknown",
        timeout_s: float = 60.0,
    ) -> GeminiResponseLike: ...


@dataclass(frozen=True)
class LintFailure:
    """Mirror of ``gemini_harness.meta.linter.Failure`` (subset used here)."""

    check_name: str
    severity: str  # "error" | "warn"
    message: str
    field_path: str | None = None


@dataclass
class LintResult:
    """Stub mirroring ``gemini_harness.meta.linter.LintResult``.

    Used by tests that need to construct lint outcomes without depending on the
    meta package. The real class has the same ``passed`` / ``failures`` fields
    plus ``errors()`` / ``warnings()`` helpers. The Worker only reads
    ``passed`` and ``failures``, so either class satisfies the Protocol.
    """

    passed: bool
    failures: list[LintFailure] = field(default_factory=list)


class MetaLinter(Protocol):
    """Functions the Worker needs from ``gemini_harness.meta.linter``.

    The concrete implementation exports these as module-level functions. Worker
    tests inject an object with matching methods; production code can pass the
    module itself (``import gemini_harness.meta.linter as linter``).
    """

    def lint_agent(
        self,
        frontmatter: dict[str, Any],
        body: str,
        agent_meta: dict[str, Any] | None = None,
    ) -> Any: ...

    def lint_skill(
        self,
        frontmatter: dict[str, Any],
        body: str,
        entry_path: str,
        read_root: str,
    ) -> Any: ...

    def lint_workflow(self, workflow: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ToolExecResult:
    is_error: bool
    text: str | None = None
    structured: dict[str, Any] | None = None
    raw_content: list[dict[str, Any]] = field(default_factory=list)


class ToolExecutor(Protocol):
    """Subset of mcp_adapter/local-fn dispatch used by ToolExecutor node."""

    def __call__(
        self,
        call_name: str,
        call_args: dict[str, Any],
        *,
        timeout_s: float = 30.0,
        node: str = "tool_executor",
        run_id: str = "unknown",
    ) -> ToolExecResult: ...


__all__ = [
    "GeminiClient",
    "GeminiResponseLike",
    "LintFailure",
    "LintResult",
    "MetaLinter",
    "ToolCallDecl",
    "ToolDecl",
    "ToolExecResult",
    "ToolExecutor",
    "UsageMetadata",
]

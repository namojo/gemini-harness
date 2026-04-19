"""Gemini API wrapper (google-genai SDK).

Contract: `_workspace/guide/gemini_integration.md` §1.

Maps SDK exceptions onto the error matrix in §6 and records one metrics line
per call to `_workspace/metrics/calls.jsonl` (§7).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from gemini_harness.integrations._errors import (
    GeminiAuthError,
    GeminiContentBlockedError,
    GeminiRateLimitError,
    GeminiServerError,
    GeminiTimeoutError,
)
from gemini_harness.integrations._metrics import record_call
from gemini_harness.integrations._retry import retry_transient

_log = logging.getLogger(__name__)

DEFAULT_MODEL = "gemini-3.1-pro-preview"


@dataclass(frozen=True)
class ToolDecl:
    """Function-calling declaration. Maps to `types.FunctionDeclaration`."""

    name: str
    description: str
    parameters_json_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """One model-emitted function call. Mirrors `types.FunctionCall`.

    `id` is the Worker-assigned correlation key used as the dict key in
    `state.tool_results`. The Gemini SDK does not always populate an id;
    when missing, the client fills `f"{node}-{turn}-{idx}"` so callers get
    a stable identifier.
    """

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
class GeminiResponse:
    text: str | None
    tool_calls: list[ToolCall]
    usage: UsageMetadata
    finish_reason: str
    blocked_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _as_list(v: str | Sequence[str] | None) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _coerce_usage(raw_usage: Any) -> UsageMetadata:
    """Best-effort extraction of usage_metadata. SDK objects are attribute-style."""
    if raw_usage is None:
        return UsageMetadata()

    def _g(name: str) -> int:
        val = getattr(raw_usage, name, 0)
        return int(val) if val is not None else 0

    return UsageMetadata(
        prompt_token_count=_g("prompt_token_count"),
        candidates_token_count=_g("candidates_token_count"),
        cached_content_token_count=_g("cached_content_token_count"),
        thoughts_token_count=_g("thoughts_token_count"),
        tool_use_prompt_token_count=_g("tool_use_prompt_token_count"),
        total_token_count=_g("total_token_count"),
    )


def _classify_sdk_exception(exc: BaseException) -> Exception:
    """Map SDK / transport exceptions onto the integration error matrix.

    We match loosely on both exception class name and string content because
    google-genai's concrete exception types live under a few different
    submodules and have shifted across versions.
    """
    name = type(exc).__name__.lower()
    msg = str(exc).lower()

    if "timeout" in name or "timeout" in msg:
        return GeminiTimeoutError(str(exc))
    if "ratelimit" in name or "429" in msg or "rate limit" in msg or "resource_exhausted" in msg:
        return GeminiRateLimitError(str(exc))
    if "permission" in name or "unauthorized" in name or "401" in msg or "403" in msg:
        return GeminiAuthError(str(exc))
    if "unauthenticated" in msg or "api key" in msg:
        return GeminiAuthError(str(exc))
    # Server-side: 5xx, internal, unavailable
    if any(tok in msg for tok in ("500", "502", "503", "504", "internal", "unavailable")):
        return GeminiServerError(str(exc))
    if "server" in name:
        return GeminiServerError(str(exc))
    return exc


def _extract_tool_calls(response: Any, node: str, turn: int) -> list[ToolCall]:
    """Pull function calls from the SDK response, assigning stable ids."""
    raw_calls = getattr(response, "function_calls", None) or []
    out: list[ToolCall] = []
    for idx, fc in enumerate(raw_calls):
        sdk_id = getattr(fc, "id", None)
        call_id = sdk_id if sdk_id else f"{node}-{turn}-{idx}"
        args_raw = getattr(fc, "args", None) or {}
        # SDK may return a mapping-like object; coerce to plain dict
        try:
            args = dict(args_raw)
        except (TypeError, ValueError):
            args = {}
        out.append(ToolCall(id=call_id, name=getattr(fc, "name", "") or "", args=args))
    return out


def _extract_finish_and_block(response: Any) -> tuple[str, str | None]:
    """Return (finish_reason, blocked_reason)."""
    finish_reason = "OTHER"
    blocked_reason: str | None = None

    candidates = getattr(response, "candidates", None) or []
    if candidates:
        first = candidates[0]
        fr = getattr(first, "finish_reason", None)
        if fr is not None:
            finish_reason = getattr(fr, "name", None) or str(fr)

    pf = getattr(response, "prompt_feedback", None)
    if pf is not None:
        br = getattr(pf, "block_reason", None)
        if br is not None:
            blocked_reason = getattr(br, "name", None) or str(br)

    return finish_reason, blocked_reason


class GeminiClient:
    """Thin class wrapper over `call_gemini` for callers that prefer a handle.

    Instantiation is cheap — the underlying `google.genai.Client` is created
    on first use and cached. Tests can inject a pre-built SDK client via
    `sdk_client=`.
    """

    def __init__(self, api_key: str | None = None, *, sdk_client: Any | None = None):
        self._api_key = api_key
        self._sdk_client = sdk_client

    def _client(self) -> Any:
        if self._sdk_client is not None:
            return self._sdk_client
        from google import genai  # local import so tests can stub the module

        api_key = (
            self._api_key
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
        )
        if not api_key:
            raise GeminiAuthError(
                "No API key: set GEMINI_API_KEY or GOOGLE_API_KEY, or pass api_key= to GeminiClient."
            )
        self._sdk_client = genai.Client(api_key=api_key)
        return self._sdk_client

    def call(
        self,
        prompt: str | Sequence[str],
        *,
        system: str | Sequence[str] | None = None,
        context: str | Sequence[str] = (),
        temperature: float = 0.7,
        max_output_tokens: int | None = None,
        tools: Sequence[ToolDecl] | None = None,
        tool_choice: Literal["auto", "any", "none"] = "auto",
        model: str = DEFAULT_MODEL,
        node: str = "unknown",
        run_id: str = "unknown",
        turn: int = 0,
        timeout_s: float = 60.0,
    ) -> GeminiResponse:
        return _call_gemini_impl(
            self._client(),
            prompt=prompt,
            system=system,
            context=context,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            tools=tools,
            tool_choice=tool_choice,
            model=model,
            node=node,
            run_id=run_id,
            turn=turn,
            timeout_s=timeout_s,
        )


def call_gemini(
    prompt: str | Sequence[str],
    *,
    system: str | Sequence[str] | None = None,
    context: str | Sequence[str] = (),
    temperature: float = 0.7,
    max_output_tokens: int | None = None,
    tools: Sequence[ToolDecl] | None = None,
    tool_choice: Literal["auto", "any", "none"] = "auto",
    model: str = DEFAULT_MODEL,
    node: str = "unknown",
    run_id: str = "unknown",
    turn: int = 0,
    timeout_s: float = 60.0,
    client: Any | None = None,
) -> GeminiResponse:
    """Module-level convenience wrapper; see `GeminiClient.call` for the contract."""
    if client is None:
        client = GeminiClient()._client()
    return _call_gemini_impl(
        client,
        prompt=prompt,
        system=system,
        context=context,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        tools=tools,
        tool_choice=tool_choice,
        model=model,
        node=node,
        run_id=run_id,
        turn=turn,
        timeout_s=timeout_s,
    )


@retry_transient()
def _call_gemini_impl(
    sdk_client: Any,
    *,
    prompt: str | Sequence[str],
    system: str | Sequence[str] | None,
    context: str | Sequence[str],
    temperature: float,
    max_output_tokens: int | None,
    tools: Sequence[ToolDecl] | None,
    tool_choice: Literal["auto", "any", "none"],
    model: str,
    node: str,
    run_id: str,
    turn: int,
    timeout_s: float,
) -> GeminiResponse:
    from google.genai import types

    contents = [*_as_list(context), *_as_list(prompt)]

    config_kwargs: dict[str, Any] = {"temperature": temperature}
    if system is not None:
        config_kwargs["system_instruction"] = _as_list(system)
    if max_output_tokens is not None:
        config_kwargs["max_output_tokens"] = max_output_tokens

    if tools:
        fn_decls = [
            types.FunctionDeclaration(
                name=t.name,
                description=t.description,
                parameters_json_schema=t.parameters_json_schema,
            )
            for t in tools
        ]
        config_kwargs["tools"] = [types.Tool(function_declarations=fn_decls)]
        mode_map = {
            "auto": types.FunctionCallingConfigMode.AUTO,
            "any": types.FunctionCallingConfigMode.ANY,
            "none": types.FunctionCallingConfigMode.NONE,
        }
        config_kwargs["tool_config"] = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(mode=mode_map[tool_choice])
        )

    # http_options.timeout is in milliseconds per google-genai conventions.
    config_kwargs["http_options"] = types.HttpOptions(timeout=int(timeout_s * 1000))

    config = types.GenerateContentConfig(**config_kwargs)

    start_ns = time.monotonic_ns()
    outcome = "ok"
    error_kind: str | None = None
    try:
        response = sdk_client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
    except Exception as exc:  # noqa: BLE001 — classify then re-raise
        mapped = _classify_sdk_exception(exc)
        outcome = "error"
        error_kind = type(mapped).__name__
        _emit_api_metric(
            model=model,
            node=node,
            run_id=run_id,
            outcome=outcome,
            error_kind=error_kind,
            latency_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
        )
        raise mapped from exc

    usage = _coerce_usage(getattr(response, "usage_metadata", None))
    finish_reason, blocked_reason = _extract_finish_and_block(response)
    tool_calls = _extract_tool_calls(response, node=node, turn=turn)

    # Content-filter / safety blocks are terminal — not retried.
    if blocked_reason or finish_reason == "SAFETY":
        reason = blocked_reason or "SAFETY"
        _emit_api_metric(
            model=model,
            node=node,
            run_id=run_id,
            outcome="error",
            error_kind="GeminiContentBlockedError",
            latency_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
            usage=usage,
            finish_reason=finish_reason,
            tool_calls_count=len(tool_calls),
        )
        raise GeminiContentBlockedError(
            f"Gemini blocked content: {reason}",
            reason=reason,
            category=blocked_reason,
        )

    _emit_api_metric(
        model=model,
        node=node,
        run_id=run_id,
        outcome=outcome,
        latency_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
        usage=usage,
        finish_reason=finish_reason,
        tool_calls_count=len(tool_calls),
    )

    text = getattr(response, "text", None)
    if text is None and not tool_calls:
        text = ""

    return GeminiResponse(
        text=text,
        tool_calls=tool_calls,
        usage=usage,
        finish_reason=finish_reason,
        blocked_reason=blocked_reason,
        raw={},
    )


def _emit_api_metric(
    *,
    model: str,
    node: str,
    run_id: str,
    outcome: str,
    latency_ms: int,
    error_kind: str | None = None,
    usage: UsageMetadata | None = None,
    finish_reason: str | None = None,
    tool_calls_count: int | None = None,
) -> None:
    record: dict[str, Any] = {
        "channel": "api",
        "node": node,
        "run_id": run_id,
        "outcome": outcome,
        "latency_ms": latency_ms,
        "model": model,
    }
    if usage is not None:
        record.update(
            {
                "input_tokens": usage.prompt_token_count,
                "output_tokens": usage.candidates_token_count,
                "cached_tokens": usage.cached_content_token_count,
                "thoughts_tokens": usage.thoughts_token_count,
                "tool_use_tokens": usage.tool_use_prompt_token_count,
                "total_tokens": usage.total_token_count,
            }
        )
    if finish_reason is not None:
        record["finish_reason"] = finish_reason
    if tool_calls_count is not None:
        record["tool_calls_count"] = tool_calls_count
    if error_kind is not None:
        record["error_kind"] = error_kind
    record_call(record)

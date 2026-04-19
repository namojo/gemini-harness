"""Unit tests for `gemini_harness.integrations.gemini_client`.

Mocks the google-genai SDK surface so no network or auth is required.
"""

from __future__ import annotations

import json
import sys
import types as stdlib_types
from dataclasses import dataclass, field
from typing import Any

import pytest


# ---------------- fake google.genai module ----------------


class _FakeFinishReason:
    def __init__(self, name: str):
        self.name = name


@dataclass
class _FakeUsage:
    prompt_token_count: int = 10
    candidates_token_count: int = 20
    cached_content_token_count: int = 0
    thoughts_token_count: int = 0
    tool_use_prompt_token_count: int = 0
    total_token_count: int = 30


class _FakeFunctionCall:
    def __init__(self, name: str, args: dict[str, Any], fc_id: str | None = None):
        self.name = name
        self.args = args
        self.id = fc_id


@dataclass
class _FakeCandidate:
    finish_reason: _FakeFinishReason = field(default_factory=lambda: _FakeFinishReason("STOP"))


class _FakePromptFeedback:
    def __init__(self, block_reason: str | None = None):
        self.block_reason = _FakeFinishReason(block_reason) if block_reason else None


class _FakeResponse:
    def __init__(
        self,
        *,
        text: str | None = "hello",
        function_calls: list[_FakeFunctionCall] | None = None,
        finish_reason: str = "STOP",
        block_reason: str | None = None,
        usage: _FakeUsage | None = None,
    ):
        self.text = text
        self.function_calls = function_calls or []
        self.candidates = [_FakeCandidate(finish_reason=_FakeFinishReason(finish_reason))]
        self.prompt_feedback = _FakePromptFeedback(block_reason=block_reason)
        self.usage_metadata = usage or _FakeUsage()


class _FakeModels:
    def __init__(self, response: _FakeResponse | Exception):
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, *, model: str, contents: Any, config: Any) -> _FakeResponse:
        self.calls.append({"model": model, "contents": contents, "config": config})
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class _FakeClient:
    def __init__(self, response: _FakeResponse | Exception):
        self.models = _FakeModels(response)


# ---- fake google.genai.types module with enough surface ----


def _install_fake_genai(monkeypatch: pytest.MonkeyPatch, response: Any) -> _FakeClient:
    fake_google = stdlib_types.ModuleType("google")
    fake_genai = stdlib_types.ModuleType("google.genai")
    fake_types = stdlib_types.ModuleType("google.genai.types")

    @dataclass
    class GenerateContentConfig:
        temperature: float = 0.7
        system_instruction: Any = None
        max_output_tokens: int | None = None
        tools: Any = None
        tool_config: Any = None
        http_options: Any = None

    @dataclass
    class FunctionDeclaration:
        name: str
        description: str
        parameters_json_schema: dict[str, Any]

    @dataclass
    class Tool:
        function_declarations: list[FunctionDeclaration] = field(default_factory=list)

    class FunctionCallingConfigMode:
        AUTO = "AUTO"
        ANY = "ANY"
        NONE = "NONE"

    @dataclass
    class FunctionCallingConfig:
        mode: str = "AUTO"

    @dataclass
    class ToolConfig:
        function_calling_config: FunctionCallingConfig = field(
            default_factory=FunctionCallingConfig
        )

    @dataclass
    class HttpOptions:
        timeout: int = 60000

    fake_types.GenerateContentConfig = GenerateContentConfig
    fake_types.FunctionDeclaration = FunctionDeclaration
    fake_types.Tool = Tool
    fake_types.FunctionCallingConfigMode = FunctionCallingConfigMode
    fake_types.FunctionCallingConfig = FunctionCallingConfig
    fake_types.ToolConfig = ToolConfig
    fake_types.HttpOptions = HttpOptions

    client = _FakeClient(response)

    def Client(api_key: str | None = None) -> _FakeClient:  # noqa: N802
        return client

    fake_genai.Client = Client
    fake_genai.types = fake_types
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    return client


@pytest.fixture
def fake_client_factory(monkeypatch: pytest.MonkeyPatch):
    def _make(response: Any) -> _FakeClient:
        return _install_fake_genai(monkeypatch, response)

    return _make


# ---------------- tests ----------------


def test_call_gemini_returns_text_and_usage(fake_client_factory, tmp_metrics_path):
    resp = _FakeResponse(
        text="ok world",
        usage=_FakeUsage(
            prompt_token_count=12,
            candidates_token_count=34,
            total_token_count=46,
        ),
    )
    fake_client_factory(resp)

    from gemini_harness.integrations.gemini_client import call_gemini

    result = call_gemini(
        "hi",
        system="you are helpful",
        context=["bg1", "bg2"],
        node="worker",
        run_id="t-1",
    )

    assert result.text == "ok world"
    assert result.finish_reason == "STOP"
    assert result.usage.prompt_token_count == 12
    assert result.usage.candidates_token_count == 34

    # Metric line written
    lines = tmp_metrics_path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["channel"] == "api"
    assert rec["node"] == "worker"
    assert rec["run_id"] == "t-1"
    assert rec["outcome"] == "ok"
    assert rec["input_tokens"] == 12


def test_call_gemini_tool_calls_get_ids_filled(fake_client_factory, tmp_metrics_path):
    resp = _FakeResponse(
        text=None,
        function_calls=[
            _FakeFunctionCall("get_weather", {"city": "Seoul"}),
            _FakeFunctionCall("get_time", {"tz": "KST"}, fc_id="sdk-xyz"),
        ],
    )
    fake_client_factory(resp)

    from gemini_harness.integrations.gemini_client import call_gemini, ToolDecl

    decl = ToolDecl(
        name="get_weather",
        description="get weather",
        parameters_json_schema={"type": "object"},
    )
    result = call_gemini(
        "prompt",
        tools=[decl],
        tool_choice="any",
        node="worker",
        run_id="t-2",
        turn=3,
    )
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].id == "worker-3-0"  # filled by us
    assert result.tool_calls[0].name == "get_weather"
    assert result.tool_calls[0].args == {"city": "Seoul"}
    assert result.tool_calls[1].id == "sdk-xyz"  # preserved from SDK


def test_call_gemini_retries_on_rate_limit(fake_client_factory, tmp_metrics_path, monkeypatch):
    """Transient errors are retried; tenacity eventually surfaces the original class."""
    # Install the fake SDK so google.genai.types import works.
    fake_client_factory(_FakeResponse(text="ignored"))

    # Speed up tenacity: patch wait_exponential in the retry module to no-wait.
    import gemini_harness.integrations._retry as retry_mod
    from tenacity import wait_none

    monkeypatch.setattr(retry_mod, "wait_exponential", lambda **kw: wait_none())

    # Rebuild gemini_client so the decorator closes over the new wait.
    import importlib
    import gemini_harness.integrations.gemini_client as gc_mod

    importlib.reload(gc_mod)

    calls = {"n": 0}

    class RateLimitError(Exception):
        pass

    def flaky_generate_content(*, model, contents, config):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RateLimitError("429 rate limit exceeded")
        return _FakeResponse(text="eventually")

    from google.genai import Client as _Client

    fake = _Client()
    fake.models.generate_content = flaky_generate_content  # type: ignore[method-assign]

    result = gc_mod.call_gemini("hi", node="worker", run_id="t-retry", client=fake)
    assert result.text == "eventually"
    assert calls["n"] == 3


def test_call_gemini_auth_error_not_retried(fake_client_factory, tmp_metrics_path):
    """401/403 raises GeminiAuthError and is not retried."""

    class PermissionDenied(Exception):
        pass

    err = PermissionDenied("permission denied: 403 unauthorized")
    fake_client_factory(err)

    from gemini_harness.integrations.gemini_client import call_gemini
    from gemini_harness.integrations._errors import GeminiAuthError

    with pytest.raises(GeminiAuthError):
        call_gemini("hi", node="worker", run_id="t-auth")

    lines = tmp_metrics_path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["outcome"] == "error"
    assert rec["error_kind"] == "GeminiAuthError"


def test_call_gemini_content_blocked(fake_client_factory, tmp_metrics_path):
    resp = _FakeResponse(text=None, finish_reason="SAFETY", block_reason="PROHIBITED_CONTENT")
    fake_client_factory(resp)

    from gemini_harness.integrations.gemini_client import call_gemini
    from gemini_harness.integrations._errors import GeminiContentBlockedError

    with pytest.raises(GeminiContentBlockedError) as exc_info:
        call_gemini("trigger", node="worker", run_id="t-block")
    assert exc_info.value.reason == "PROHIBITED_CONTENT"

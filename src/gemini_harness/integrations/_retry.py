"""Shared tenacity retry decorator factories.

Contract: `_workspace/guide/gemini_integration.md` §4.

All factories set `reraise=True` so the call site sees the original exception
type instead of tenacity's `RetryError` wrapper.
"""

from __future__ import annotations

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from gemini_harness.integrations._errors import (
    GeminiRateLimitError,
    GeminiServerError,
    GeminiTimeoutError,
)

_TRANSIENT_EXC = (GeminiRateLimitError, GeminiServerError, GeminiTimeoutError)


def retry_transient():
    """RETRY_TRANSIENT — 3 attempts, 2s→30s exponential, transient exceptions only.

    Used by `call_gemini` and `call_mcp_tool`.
    """
    return retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=30),
        retry=retry_if_exception_type(_TRANSIENT_EXC),
        reraise=True,
    )


def retry_timeout_only():
    """RETRY_TIMEOUT_ONLY — reserved for long-running MCP tools."""
    return retry(
        stop=stop_after_attempt(2),
        wait=wait_exponential(min=5, max=20),
        retry=retry_if_exception_type(GeminiTimeoutError),
        reraise=True,
    )

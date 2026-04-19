"""External boundary wrappers — Gemini API, Gemini CLI, outbound MCP.

Per ADR 0005 and `_workspace/guide/gemini_integration.md` §0, modules in this
package must never import from `langgraph`. All LangGraph primitives flow
through `gemini_harness.runtime.compat`.
"""

from gemini_harness.integrations._errors import (
    GeminiAuthError,
    GeminiCliError,
    GeminiCliVersionError,
    GeminiContentBlockedError,
    GeminiRateLimitError,
    GeminiServerError,
    GeminiTimeoutError,
    IntegrationError,
    McpConnectionError,
    McpProtocolError,
)

__all__ = [
    "IntegrationError",
    "GeminiAuthError",
    "GeminiRateLimitError",
    "GeminiServerError",
    "GeminiTimeoutError",
    "GeminiContentBlockedError",
    "GeminiCliError",
    "GeminiCliVersionError",
    "McpConnectionError",
    "McpProtocolError",
]

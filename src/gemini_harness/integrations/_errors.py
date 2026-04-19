"""Shared exception hierarchy for the integrations package.

Matches the error matrix in `_workspace/guide/gemini_integration.md` §6.
All exceptions inherit from `IntegrationError` so Worker catch-alls can opt
into a single superclass without losing type tags.
"""

from __future__ import annotations


class IntegrationError(Exception):
    """Root of the integration exception hierarchy."""


class GeminiAuthError(IntegrationError):
    """401 / 403 — bad or missing GEMINI_API_KEY. Not retried."""


class GeminiRateLimitError(IntegrationError):
    """429 rate limit. Retried under RETRY_TRANSIENT."""


class GeminiServerError(IntegrationError):
    """5xx from Gemini API. Retried under RETRY_TRANSIENT."""


class GeminiTimeoutError(IntegrationError):
    """Network / model stall. Retried under RETRY_TRANSIENT."""


class GeminiContentBlockedError(IntegrationError):
    """finish_reason==SAFETY or blocked_reason populated. Not retried."""

    def __init__(self, message: str, *, reason: str | None = None, category: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.category = category


class GeminiCliVersionError(IntegrationError):
    """`gemini --version` < required minimum."""


class GeminiCliError(IntegrationError):
    """`gemini` subprocess exited non-zero."""

    def __init__(self, message: str, *, returncode: int | None = None, stderr: str | None = None):
        super().__init__(message)
        self.returncode = returncode
        self.stderr = stderr


class McpConnectionError(IntegrationError):
    """stdio / http handshake to an outbound MCP server failed."""


class McpProtocolError(IntegrationError):
    """MCPError surfaced from the client SDK (framing / JSON-RPC error)."""

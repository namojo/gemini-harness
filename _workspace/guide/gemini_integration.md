# Gemini Integration Contract — Phase 2 Draft

> **Scope.** Interface contracts for `gemini_client`, `cli_bridge`, and `mcp_adapter` modules consumed by LangGraph Worker nodes. **Contracts only — no implementation in this document.** Implementation lands in Phase 3+.
>
> **Source authority.** `google-genai` v1.33.0 (context7 `/googleapis/python-genai`), `mcp` Python SDK v1.12.4 (context7 `/modelcontextprotocol/python-sdk`). Gemini CLI extension API specifics marked **TBD — requires Gemini CLI team confirmation** where official schema is not surfaced via context7.

---

## 0. Module boundary (binding)

| Module | Responsibility | Allowed imports |
|--------|----------------|-----------------|
| `integrations/gemini_client.py` | Python SDK wrapper for `gemini-3.1-pro-preview` | `google.genai`, `tenacity`, stdlib |
| `integrations/cli_bridge.py` | `subprocess` wrapper around `gemini` CLI binary | `subprocess`, `shlex`, `packaging`, stdlib |
| `integrations/mcp_adapter.py` | MCP client for outbound MCP servers (DB, internal APIs) | `mcp.client`, `mcp.types`, `asyncio`, stdlib |
| `runtime/compat.py` | **Sole** LangGraph import surface | `langgraph.*`, `packaging` |

**Hard rule (ADR 0005, restated).** None of `gemini_client`, `cli_bridge`, `mcp_adapter` may `import langgraph` or any submodule. They return plain dataclasses / TypedDicts. The Worker layer (`runtime/worker.py`) is responsible for translating these into `Command` / `Send` / state updates via `runtime/compat.py`.

> Enforcement: `ruff` custom rule + grep CI check. Violations block merge.

---

## 1. `gemini_client` — Gemini API wrapper

### 1.1 Public surface

```python
# integrations/gemini_client.py — interface declaration only

from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

@dataclass(frozen=True)
class ToolDecl:
    """Function-calling declaration. Maps to types.FunctionDeclaration."""
    name: str
    description: str
    parameters_json_schema: dict[str, Any]   # JSON Schema (draft-07 subset)

@dataclass(frozen=True)
class ToolCall:
    """One model-emitted call. Mirrors types.FunctionCall.

    `id` is the Worker-assigned correlation key used as the dict key in
    `state.tool_results`. The Gemini SDK does not always populate an id;
    Worker generates a stable one (e.g. `f"{node}-{turn}-{idx}"`) when missing.
    """
    id: str
    name: str
    args: dict[str, Any]

@dataclass(frozen=True)
class UsageMetadata:
    prompt_token_count: int
    candidates_token_count: int
    cached_content_token_count: int
    thoughts_token_count: int
    tool_use_prompt_token_count: int
    total_token_count: int

@dataclass(frozen=True)
class GeminiResponse:
    text: str | None                   # None when only tool_calls returned
    tool_calls: list[ToolCall]         # empty list when none
    usage: UsageMetadata
    finish_reason: str                 # "STOP" | "MAX_TOKENS" | "SAFETY" | "RECITATION" | "OTHER"
    blocked_reason: str | None         # "SAFETY" | "PROHIBITED_CONTENT" | ... | None
    raw: dict[str, Any] = field(default_factory=dict)  # opaque for debugging; do not depend on shape

def call_gemini(
    prompt: str | Sequence[str],
    *,
    system: str | Sequence[str] | None = None,
    context: str | Sequence[str] = (),
    temperature: float = 0.7,
    max_output_tokens: int | None = None,
    tools: Sequence[ToolDecl] | None = None,
    tool_choice: Literal["auto", "any", "none"] = "auto",
    model: str = "gemini-3.1-pro-preview",
    node: str = "unknown",       # for metrics tagging
    run_id: str = "unknown",     # for metrics tagging
    timeout_s: float = 60.0,
) -> GeminiResponse: ...
```

**Notes**

1. `contents` order at the SDK layer is `[*context, *prompt]` (context first so the model treats it as background).
2. `system_instruction` is passed via `types.GenerateContentConfig(system_instruction=...)`. Sequence-of-strings is supported by the SDK and preserved here.
3. When `tools` is provided we wrap them in a single `types.Tool(function_declarations=[...])`. We do **not** use the SDK's automatic-function-calling path — Worker handles tool execution itself so the LangGraph state stays canonical.
4. `tool_choice` maps to `types.FunctionCallingConfig(mode=...)` inside `GenerateContentConfig.tool_config`. `"auto"` is the SDK default; `"any"` forces ≥1 call; `"none"` disables.
5. `GeminiResponse.raw` is intentionally unstructured. It exists for QA dumps; production code MUST NOT branch on it.

### 1.2 Async variant (optional, future)

`call_gemini_async(...)` will share the signature but `await client.aio.models.generate_content(...)`. **Out of scope for Phase 3.** Workers run synchronously inside LangGraph nodes today; we revisit when fan-out parallelism becomes a bottleneck.

### 1.3 Streaming

Not exposed in v1. `generate_content_stream` is intentionally omitted — node outputs must land atomically in state. Decision recorded with harness-architect 2026-04-19; revisit when MCP progress notification pattern is designed.

---

## 2. `cli_bridge` — Gemini CLI subprocess wrapper

### 2.1 Public surface

```python
# integrations/cli_bridge.py — interface declaration only

from dataclasses import dataclass

@dataclass(frozen=True)
class CliResult:
    stdout: str
    stderr: str
    returncode: int
    duration_ms: int

def check_gemini_cli(min_version: str = "0.28.0") -> str:
    """Run `gemini --version`, raise GeminiCliVersionError if < min_version.
    Returns the parsed version string. Cache result per-process."""

def invoke_cli_skill(
    skill: str,
    args: list[str],
    *,
    cwd: str = ".",
    timeout: int = 60,
    env: dict[str, str] | None = None,    # merged onto os.environ
    input_text: str | None = None,        # piped to stdin
    node: str = "unknown",
    run_id: str = "unknown",
) -> CliResult: ...

def invoke_cli_extension(
    extension: str,
    subcommand: str,
    args: list[str],
    *,
    cwd: str = ".",
    timeout: int = 60,
    node: str = "unknown",
    run_id: str = "unknown",
) -> CliResult: ...
```

### 2.2 Security invariants

- `subprocess.run(..., shell=False)` always. `shell=True` is forbidden anywhere in this module.
- `args` MUST be a `list[str]`. Callers passing a single string are a programmer error → raise `TypeError`.
- Any user-supplied path/value is appended as a list element only. Never f-string into a command.
- `env` is merged onto a copy of `os.environ`; we never mutate the parent process env.

### 2.3 CLI invocation form

`["gemini", "skill", skill, *args]` for `invoke_cli_skill`.
`["gemini", "extensions", "run", extension, subcommand, *args]` for `invoke_cli_extension`.
**TBD — requires Gemini CLI team confirmation** for the exact extension run subcommand. The CLI extension API is not in context7's indexed docs at v0.28.0; the form above is the assumed shape from `gemini-cli-extension-packaging` SKILL. Implementation MUST verify with `gemini extensions --help` before merging.

---

## 3. `mcp_adapter` — Outbound MCP client

### 3.1 Scope clarification

This module is the **client** side: code that calls into external MCP servers (e.g., a project-local DB MCP, an internal API MCP) from inside Worker nodes. It is **not** the harness's own MCP server (that lives in `mcp_server.py`, exposing `harness.*` tools — see `mcp_tools.md`).

### 3.2 Public surface

```python
# integrations/mcp_adapter.py — interface declaration only

from dataclasses import dataclass
from typing import Any, Literal

@dataclass(frozen=True)
class McpServerSpec:
    name: str                                     # logical id, e.g. "project-db"
    transport: Literal["stdio", "http"]
    command: list[str] | None = None              # required when transport=="stdio"
    url: str | None = None                        # required when transport=="http"
    env: dict[str, str] | None = None

@dataclass(frozen=True)
class McpToolResult:
    is_error: bool
    text: str | None                              # joined TextContent blocks
    structured: dict[str, Any] | None             # CallToolResult.structuredContent if present
    raw_content: list[dict[str, Any]]             # opaque dump of result.content for QA

async def call_mcp_tool(
    server: McpServerSpec,
    tool: str,
    args: dict[str, Any],
    *,
    timeout: float = 30.0,
    node: str = "unknown",
    run_id: str = "unknown",
) -> McpToolResult: ...

async def list_mcp_tools(server: McpServerSpec, *, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Returns name/description/inputSchema for each tool. Used at runtime startup
    to validate that requested servers expose the tools the harness expects."""
```

### 3.3 Session lifecycle

- Each call opens a fresh `ClientSession` via `stdio_client` / `streamablehttp_client`. **No connection pooling in v1.** Re-evaluate if measured per-call overhead exceeds 200 ms.
- `session.initialize()` is always invoked before `call_tool`.
- `MCPError` from the SDK is caught and converted to `McpProtocolError` (see §6).

### 3.4 SDK version pin

We target `mcp >= 1.12, < 2.0`. The v2 constructor-based handler API (per migration docs) affects **server** code only; this client adapter is unaffected by that change. If we adopt v2.x, the change is isolated to `mcp_server.py`.

---

## 4. Retry policy (`tenacity`)

Single shared decorator factory, defined once and reused across `gemini_client` and `mcp_adapter`. `cli_bridge` does **not** retry by default (CLI failures are usually deterministic — version, missing skill, bad args).

```python
# Conceptual; lives in integrations/_retry.py
from tenacity import (
    retry, stop_after_attempt, wait_exponential, retry_if_exception_type, reraise=True
)
```

| Policy name | Used by | `stop` | `wait` | Retried exceptions |
|-------------|---------|--------|--------|--------------------|
| `RETRY_TRANSIENT` | `call_gemini`, `call_mcp_tool` | `stop_after_attempt(3)` | `wait_exponential(min=2, max=30)` | `RateLimitError` (429), `ServerError` (5xx), `TimeoutError` |
| `RETRY_NONE` | `invoke_cli_skill`, `invoke_cli_extension`, auth failures | n/a | n/a | nothing — surface immediately |
| `RETRY_TIMEOUT_ONLY` | reserved for long-running MCP tools | `stop_after_attempt(2)` | `wait_exponential(min=5, max=20)` | `TimeoutError` only |

**Always set `reraise=True`** so the original exception type is observable at the call site (the default tenacity behaviour wraps in `RetryError`, which destroys the error matrix in §6).

---

## 5. 1M context utilization

### 5.1 Principles

1. **Structure beats volume.** Long context only helps when the model can locate the relevant span. Wrap each section in a markdown header (`## workflow`, `## current_registry`, `## recent_errors`). Reference back explicitly: "see `## workflow` above".
2. **Prefer cached content** for stable preambles (system + global context). The SDK's `cached_content_token_count` field surfaces hit rate — log it.
3. **One copy per fact.** Repetition is read by the model as emphasis; duplicating the same registry block twice biases the next decision.
4. **Cap per call.** Soft budget: 200K input tokens for routine Worker calls, full 1M reserved for `harness.audit` / `harness.evolve` whole-project sweeps.

### 5.2 Anti-patterns (forbidden)

- Concatenating every file in `.agents/` into one prompt without headers. Use a manifest + on-demand fetch via `cli_bridge` instead.
- Re-injecting full inbox history every turn. Pass only the last N messages relevant to the addressed agent.
- Embedding raw JSON dumps > 50 KB. Summarize structurally and link to the artifact path; the Worker can fetch on demand.
- Using context to "remind" the model of policy already encoded in `system_instruction`. System instructions are persistent — context should carry data, not rules.

---

## 6. Error matrix

| Error class | Trigger | Exception type | Retried? | Worker action | State write |
|-------------|---------|----------------|----------|---------------|-------------|
| 429 rate limit | Gemini API quota | `GeminiRateLimitError` | yes (3×, exp 2→30 s) | After exhaustion: append to `state.errors`, escalate to Manager | `errors += [{"kind":"rate_limit","node":..,"detail":...}]` |
| 5xx server | Gemini API server | `GeminiServerError` | yes (3×) | Same as above | Same |
| 401 / 403 auth | Bad / missing `GEMINI_API_KEY` | `GeminiAuthError` | **no** | Hard fail. Surface to user with remediation: "set GEMINI_API_KEY". | `errors += [{"kind":"auth","action_required":"set GEMINI_API_KEY"}]` then runtime exit |
| Timeout | Network / model stall | `GeminiTimeoutError` | yes (3×) | After exhaustion: degrade — reduce `max_output_tokens` and retry once at Worker level, then fail | `errors += [{"kind":"timeout","duration_ms":...}]` |
| Content filter | `finish_reason == "SAFETY"` or `blocked_reason` set | `GeminiContentBlockedError` | **no** | Record reason, do not retry the same prompt. Manager may rephrase or escalate. | `errors += [{"kind":"content_blocked","reason":...,"category":...}]` |
| CLI version mismatch | `check_gemini_cli` < 0.28.0 | `GeminiCliVersionError` | **no** | Hard fail at runtime startup before graph is built | runtime exit (no graph yet) |
| CLI nonzero exit | `invoke_cli_*` returncode ≠ 0 | `GeminiCliError` | **no** | Surface to Worker; usually a deterministic bug (bad args / missing skill) | `errors += [{"kind":"cli","skill":...,"stderr":...}]` |
| MCP connection failure | stdio/http handshake fails | `McpConnectionError` | **no** (silent fallback forbidden — see role doc) | Fail loudly with server name + transport. User decides whether to disable. | `errors += [{"kind":"mcp_conn","server":...}]` |
| MCP tool error | `CallToolResult.isError == true` | `McpToolError` (not raised — surfaced via `McpToolResult.is_error`) | n/a | Worker reads `is_error` and decides; do **not** auto-raise — some tools use `is_error` semantically | `errors += [{"kind":"mcp_tool","server":..,"tool":..,"text":...}]` if Worker decides it is fatal |
| MCP protocol error | `MCPError` from SDK | `McpProtocolError` | **no** | Hard fail | `errors += [{"kind":"mcp_protocol","detail":...}]` |

All custom exceptions inherit from a shared base `IntegrationError` so Worker catch-all sites can opt into one super-class without losing the type tag.

---

## 7. Metrics — `_workspace/metrics/calls.jsonl`

One JSON object per line, appended on every Gemini API call, CLI invocation, and MCP tool call. **No partial / streaming records** — write at completion (success or terminal failure).

### 7.1 Line schema (draft-07 fragment)

```json
{
  "$id": "https://gemini-harness.local/schema/metrics.calls.v1.json",
  "type": "object",
  "required": ["ts", "channel", "node", "run_id", "outcome", "latency_ms"],
  "additionalProperties": false,
  "properties": {
    "ts": { "type": "string", "format": "date-time" },
    "channel": { "enum": ["api", "cli", "mcp"] },
    "node": { "type": "string", "description": "LangGraph node id (e.g. 'worker', 'manager', 'meta-skill-designer')" },
    "run_id": { "type": "string", "description": "Checkpointer thread_id; ties to state.run_id" },
    "outcome": { "enum": ["ok", "retried_ok", "error"] },
    "latency_ms": { "type": "integer", "minimum": 0 },
    "attempt_count": { "type": "integer", "minimum": 1 },

    "model": { "type": "string", "description": "API channel: e.g. 'gemini-3.1-pro-preview'" },
    "input_tokens": { "type": "integer", "minimum": 0 },
    "output_tokens": { "type": "integer", "minimum": 0 },
    "cached_tokens": { "type": "integer", "minimum": 0 },
    "thoughts_tokens": { "type": "integer", "minimum": 0 },
    "tool_use_tokens": { "type": "integer", "minimum": 0 },
    "total_tokens": { "type": "integer", "minimum": 0 },
    "finish_reason": { "type": "string" },
    "tool_calls_count": { "type": "integer", "minimum": 0 },

    "skill": { "type": "string", "description": "CLI channel: skill or extension name" },
    "exit_code": { "type": "integer" },

    "server": { "type": "string", "description": "MCP channel: server logical name" },
    "tool": { "type": "string", "description": "MCP channel: tool name" },
    "is_error": { "type": "boolean" },

    "error_kind": { "type": "string", "description": "Present iff outcome=='error'. Values from §6 error matrix." }
  }
}
```

### 7.2 Write rules

- Append-only. Never rewrite existing lines. Rotation policy is QA's concern, not the integration layer's.
- File path is fixed: `_workspace/metrics/calls.jsonl`. Created on first write with `mkdir(parents=True, exist_ok=True)`.
- Writes are best-effort: a metrics write failure logs a warning but never raises into the call site.
- `run_id` and `node` flow through call kwargs (see §1.1, §2.1, §3.2). Workers MUST pass them; `"unknown"` defaults exist only so unit tests don't need to thread them.

---

## 7a. Tool-calling loop (binding)

Per architect decision 2026-04-19 (Q2 resolution), tool-calling is **explicit, checkpointer-visible** state. Three new HarnessState fields (writers shown):

| Field | Type | Reducer | Writer | Reader |
|-------|------|---------|--------|--------|
| `pending_tool_calls` | `list[ToolCall]` | overwrite (single writer) | Worker (sets) / ToolExecutor (clears to `[]`) | ToolExecutor |
| `tool_results` | `dict[call_id, McpToolResult \| dict]` | `merge_dicts` | ToolExecutor | Worker (next turn) |
| `tool_iterations` | `int` | overwrite | Manager (increments before each Worker turn) | Manager (compare to `routing_config.tool_executor.max_tool_iterations`) |

Loop topology (build-time fixed; not a runtime `add_node`, ADR 0001 unchanged):

```
Worker → (Gemini emits tool_calls) → state.pending_tool_calls = [...]
       → Command(goto="tool_executor")
ToolExecutor → for each call: dispatch via mcp_adapter / local fn
             → state.tool_results |= {id: result}
             → state.pending_tool_calls = []
             → Command(goto="manager")
Manager → state.tool_iterations += 1
       → if < max: Command(goto="worker", update={"current_target": same})
       → else: state.errors += [{"kind": "tool_iter_exhausted", ...}] → escalate / END
```

The hardcoded build-time topology becomes **Manager + Worker + ToolExecutor + Registry** (extension of ADR 0001's "Manager + Worker + Registry"; treated as clarifying extension, not reversal).

**Order of processing within a single Worker response.** Per ADR 0004 amendment: if Gemini returns both `create_agents` directives and `tool_calls`, the Worker processes `tool_calls` **first** (results may inform metadata), then `create_agents`. Never interleave.

**`state.inbox` does not carry tool-call messages.** Tool calls are intra-Worker only; inbox is for inter-agent communication. The two channels stay orthogonal.

**routing_config dependency.** This loop is gated by `workflow.json.routing_config.tool_executor` (see `mcp_tools.md` §2.2 reference). Omit that block to disable tool-calling for a workflow entirely; the ToolExecutor node is still wired but never reached.

---

## 8. Compat boundary — restated

Per ADR 0005:

> **`from langgraph import ...` is permitted only in `runtime/compat.py`.** All other modules import LangGraph primitives via `from runtime.compat import StateGraph, START, END, Command, Send, ...`.

The integration modules in this document **never need LangGraph types**. Their inputs are plain Python primitives and their outputs are dataclasses defined here. The Worker layer adapts these into `Command` / `Send` returns using `runtime/compat.py`. CI grep rule:

```
rg -nP '^\s*(from|import)\s+langgraph(\.|$|\s)' --glob '!runtime/compat.py' src/
```

A non-empty result blocks merge.

---

## 9. Architect resolutions (2026-04-19)

- **compat.py v1 surface (closed):** `StateGraph`, `START`, `END`, `Command`, `Send`, `SqliteSaver` (+ `from_conn_string`), reducer helpers (`add`, `append_unique`, `merge_inboxes`, `merge_dicts`), `packaging.version.Version`. **Excluded from v1:** `interrupt()`/`Interrupt`, `RetryPolicy` (retry stays in tenacity per §4), streaming primitives (`astream`/`astream_events`).
- **Tool-calling state:** Option (a) accepted with refinement — see §7a. New HarnessState fields `pending_tool_calls`, `tool_results`, `tool_iterations` and new build-time graph node `tool_executor`. No new ADR; State matrix amendment lands in `port-mapping.md` from architect side.
- **routing_config extension:** New `tool_executor` block in `workflow.v1.json.routing_config` with `max_tool_iterations` (default 5, max 20), `allowed_tools` whitelist, `tool_timeout_s` (default 30 s, max 300 s). No `tool_executor_id` field — the node is hardcoded, not an agent.
- **Cross-doc sync:** `mcp_tools.md` §2.2 (`harness.build`) updated to surface the new routing_config block; State × tool matrix in §6 augmented with the three new fields.

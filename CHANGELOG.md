# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-04-19

### Fixed
- **`create_agents` infinite-retry loop** (reported in v0.1.0 real-world usage).
  When Gemini's response omitted `system_prompt_body`, the Worker silently
  fabricated a short placeholder that always failed the linter → Manager
  kept routing back to the same agent → "팀원들이 계속 출근 카드만 찍는"
  symptom. Fix:
  - Worker no longer fabricates a placeholder body. `system_prompt_body`
    missing or <200 chars yields a specific `create_agent_missing_body`
    error that is fed back to the next prompt.
  - Worker strips any YAML frontmatter the LLM accidentally included in
    `system_prompt_body`, then wraps with the canonical frontmatter we
    resolve from `config.get_model()`.
  - Prompt composer adds a `## previous_errors` section so the LLM sees
    exactly what went wrong on its last turn. `create_agent_*` /
    `create_skill_*` / `artifact_*` errors are always surfaced (they are
    actionable feedback for the current agent, regardless of the failing
    entry's `id`).
  - Prompt composer adds a `## create_agents requirements (strict)`
    section enumerating every mandatory field and section so the first
    attempt has a much better chance of passing.
  - Manager loop-guard: if the same agent produces `create_agent_*` errors
    on 3 consecutive `worker_complete` events without adding any agent,
    Manager terminates with `create_agent_loop_aborted` instead of routing
    back to Worker again.

### Added
- 5 regression tests in `tests/unit/test_create_agent_loop_guard.py`
  covering missing-body rejection, error feedback in prompts, and the
  three loop-guard scenarios (abort / streak reset / different agents).
- `harness.run` response now includes an `agent_timeline: [{id, role, status}]`
  field where `status ∈ {idle, completed, blocked}`. This is the shape the
  Gemini CLI LLM is instructed to convert into `write_todos` calls.

### Changed
- **HUD strategy corrected.** In v0.1.1 we claimed MCP progress notifications
  would show active agents in Gemini CLI's HUD. In practice Gemini CLI
  does not render stdio MCP `notifications/progress` — the notifications
  were silently dropped. v0.1.2 switches to the supported path:
  1. The `/harness:run` slash command now instructs Gemini's LLM to call
     `write_todos` (Gemini CLI's native task-list tool that renders above
     the input prompt) both before and after `harness.run`.
  2. `harness.run` returns `agent_timeline` so the LLM can populate the
     todos deterministically from the response.
  3. The MCP progress notification path is kept (protocol-correct, useful
     for other MCP clients like MCP Inspector) but documented as passive
     for Gemini CLI.
  4. The accompanying `_call_runtime` change (sync runtime fns offloaded
     to `asyncio.to_thread`) remains unconditionally useful — it keeps
     the MCP event loop responsive during long harness runs regardless
     of HUD rendering.

## [0.1.1] - 2026-04-19

### Fixed
- **Parallel agent execution restored.** `fan_out_fan_in` / `supervisor` / composite patterns previously executed workers sequentially because the worker node was synchronous and `run_harness` used `.stream()`. Added `make_aworker_node` (async via `asyncio.to_thread`), `AsyncSqliteSaver` to `compat.py`, and an async path in `run_harness` driven by `.astream()`. Verified with `tests/integration/test_parallel_timing.py`: 4 workers × 1s complete in <3.5s (sequential was 5.0s).
- Gemini CLI extension manifest replaced with real schema (`gemini-extension.json` at project root with `mcpServers` + `contextFileName`). Removed the speculative `extension/manifest.json` that used `triggers` (not a real Gemini CLI field). MCP server command switched from `gemini-harness-mcp` binary (PATH-dependent) to `python3 -m gemini_harness.mcp_server` (PATH-independent).

### Added
- **MCP progress notifications** — long-running tools emit `session.send_progress_notification` per LangGraph step so Gemini CLI's HUD displays the currently active agent. `run_harness` accepts `progress_callback(progress, total, message)`; the MCP handler wires it via `asyncio.run_coroutine_threadsafe` so notifications flow without blocking the thread running the harness.
- **Non-blocking MCP handlers** — sync runtime functions are now offloaded via `asyncio.to_thread` inside `_call_runtime`, keeping the event loop responsive to progress + cancellation.
- Five Gemini CLI slash commands — `/harness:build`, `/harness:audit`, `/harness:verify`, `/harness:evolve`, `/harness:run`. The build command now reports pattern/agents/skills/routing/files in structured form and explicitly asks the user for the next action rather than auto-invoking `/harness:run`.
- `GEMINI.md` context file with ko/en/ja trigger phrases and natural-language → MCP tool mapping.
- `run_audit`, `run_build`, `run_evolve`, `run_harness`, `run_verify` end-to-end implementations, meaning MCP handlers no longer return `INTERNAL`.

### Runtime
- `runtime/_audit.py` — pure-Python scan of `.agents/` + `workflow.json` drift.
- `runtime/_verify.py` — schema / triggers / dry-run checks with reports under `_workspace/qa/verify-{run_id}/`.
- `runtime/_build.py` — meta-architect Gemini call with retry, atomic disk writes, CLAUDE.md upsert.
- `runtime/_evolve.py` — feedback-driven unified-diff edits with linter pre-check and dry-run preview.
- `runtime/_run.py` — LangGraph streaming with checkpointing (`SqliteSaver` for sync, `AsyncSqliteSaver` for parallel patterns).
- `runtime/_make_tool_executor` — MCP / CLI dispatch for workflows that declare `routing_config.tool_executor`.

### Configuration
- **One-time model setup** — new `gemini-harness configure` CLI subcommand lists the Gemini models the user's API key can access and writes their selection to `$XDG_CONFIG_HOME/gemini-harness/config.json` (chmod `0600`). `gemini-harness configure --show` / `--model NAME` covers scripted use. Resolution order: `LANGCHAIN_HARNESS_MODEL` env > config file > `DEFAULT_MODEL`.
- `gemini_harness.config` module centralizes model resolution; all runtime sites (`_build.py`, `_evolve.py`, `_run.py`) now go through `_resolve_model()` instead of inlining `os.environ.get(...)` with a hardcoded default.

### UX
- The `/harness:build` slash command prompt now **stops and asks** the user for the next action (run / evolve / verify / redo) instead of auto-chaining into `/harness:run`. Build reports pattern, agent table, skills, routing_config, and written-file summary before prompting.

## [0.1.0] - 2026-04-19

### Added
- Initial Gemini integrations:
  - `integrations/gemini_client.py` — `google-genai` wrapper with tenacity-based retry on transient errors (429/5xx/timeout), tool-calling support, and per-call metrics to `_workspace/metrics/calls.jsonl`.
  - `integrations/cli_bridge.py` — `subprocess` wrapper for the Gemini CLI with `shell=False` invariant, `gemini --version >= 0.28.0` pre-flight gate, and typed `CliResult`.
  - `integrations/mcp_adapter.py` — outbound MCP client for stdio/http servers using the `mcp` Python SDK; transient-error retry.
- `mcp_server.py` — stdio MCP server exposing `harness.audit`, `harness.build`, `harness.verify`, `harness.evolve`, `harness.run` with JSON Schema input validation and structured-content responses.
- `cli.py` — `gemini-harness` CLI with `audit`, `build`, `verify`, `evolve`, `run` subcommands and a `--cli-ext` hook for Gemini CLI extension dispatch. `extension_entry(context)` maps utterances (ko/en/ja) to subcommands.
- `pyproject.toml` packaging with `gemini-harness` / `gemini-harness-mcp` scripts, `gemini.extensions` entry point, and LangGraph pin `>=1.0,<2.0` (per ADR 0005).
- `extension/manifest.json` — Gemini CLI extension manifest with ko/en/ja trigger phrases.

### Known limitations
- `gemini_harness.runtime.harness_runtime` is provided by a parallel workstream (task #1); MCP/CLI handlers dispatch to it lazily and return `INTERNAL` if the runtime module is not yet wired.
- `meta.linter` pre-flight check is best-effort — returns `passed=True` when the module is not yet installed.
- Streaming responses from `harness.run` are reserved for v2 (see `guide/mcp_tools.md` §5.2).

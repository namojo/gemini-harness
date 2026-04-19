# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

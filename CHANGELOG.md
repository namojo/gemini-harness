# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

# gemini-harness

[한국어](./README.md) · **English**

**Team-architecture factory for Gemini CLI.** Turn a one-sentence domain description into a coordinated team of Gemini agents and the skills they use — powered by LangGraph.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-215%20passed-brightgreen.svg)](#development)
[![Port of](https://img.shields.io/badge/port_of-revfactory%2Fharness%20v1.2.0-orange.svg)](https://github.com/revfactory/harness)

---

## TL;DR

> **"Build a harness: a 4-perspective architecture team — frontend, backend, DB, infra + integrator."** → Gemini auto-selects the best of 6 architecture patterns → agent team + skills + `workflow.json` + orchestrator emitted to disk → immediately runnable.

## Overview

`gemini-harness` is a port of [revfactory/harness](https://github.com/revfactory/harness) v1.2.0 (originally a Claude Code plugin) to the **Gemini CLI + LangGraph** stack. The original's philosophy, workflow, and 6 architecture patterns are preserved 1:1; only the runtime is replaced.

### Key features

- **Meta-agent factory** — designs an optimal expert team from a single-sentence domain description.
- **Six auto-selected architecture patterns**
  - `pipeline` (sequential dependency), `fan_out_fan_in` (parallel research + integration), `expert_pool` (router-based selection)
  - `producer_reviewer` (generate-review loop), `supervisor` (dynamic dispatch), `hierarchical` (recursive delegation)
  - Composite patterns: `"fan_out_fan_in+producer_reviewer"`
- **Manager + Worker + Registry runtime** — LangGraph StateGraph; agents added dynamically at runtime.
- **Self-correction loop** — `producer_reviewer` pattern reduces defects (PRD target: 80% vs single-model baseline).
- **Sandbox boundary** — generated artifacts write only under `.agents/`, `_workspace/`, `.gemini/`.
- **Installable Gemini CLI extension** — MCP server + custom slash commands + natural-language triggers.

### MCP tools exposed

| Tool | Role | Upstream phase |
|------|------|--------------|
| `harness.audit` | Scan current project for harness state and drift | Phase 0 |
| `harness.build` | Domain description → team, skills, `workflow.json` | Phase 1-5 |
| `harness.verify` | Structural, trigger, dry-run validation | Phase 6 |
| `harness.evolve` | Feedback-driven incremental modifications (unified diff) | Phase 7 |
| `harness.run` | Execute the generated `workflow.json` via LangGraph | — |

## Requirements

- **Python ≥ 3.11**
- **Gemini CLI ≥ 0.28.0** (v0.36+ recommended)
- **Google Gemini API key** (`GOOGLE_API_KEY` or `GEMINI_API_KEY`)

## Installation

### 1) Install the package

```bash
# From PyPI (after release)
pip install gemini-harness

# From TestPyPI (pre-release, recommended while we're in beta):
pip install -i https://test.pypi.org/simple/ gemini-harness

# Or from source
git clone https://github.com/namojo/gemini-harness
cd gemini-harness
pip install -e '.[dev]'
```

> 💡 **Dependencies are declared in `pyproject.toml` under `[project.dependencies]` as the single source of truth, and pip resolves and installs them automatically.** The package list shown during install is normal output. We don't ship a separate `requirements.txt` because tracking versions in two places risks drift — if you need a reproducibility lock, generate one with `pip-compile` (e.g. `requirements.lock`).

### 2) Register as a Gemini CLI extension

```bash
# From the repo root — gemini-extension.json, GEMINI.md, and commands/ are picked up
gemini extensions install /path/to/gemini-harness

# If you are using Windows
gemini extensions install .   
```

### 3) Set up the API key

Create `.env` at the project root (auto-loaded):

```env
GOOGLE_API_KEY=your_api_key_here
```

Or export it:

```bash
export GOOGLE_API_KEY=your_api_key_here
```

### 4) One-time model selection

Run this **once** after install to pick a Gemini model. The CLI queries the models your API key can actually access and lets you choose.

```bash
gemini-harness configure
```

Example output:
```
Fetching available Gemini models from your API key...

Available models:
 *  1. gemini-3.1-pro-preview
    2. gemini-2.5-pro
    3. gemini-2.5-flash
    4. gemini-2.0-pro
    ...

Current selection: gemini-3.1-pro-preview
Enter the number of the model to use (blank = keep current):
>
```

Stored at `$XDG_CONFIG_HOME/gemini-harness/config.json` (default `~/.config/gemini-harness/config.json`, mode `0600`).

To change later, re-run the same command, or for a one-shot override use the environment variable:

```bash
# Show current configuration
gemini-harness configure --show

# Set a specific model non-interactively
gemini-harness configure --model gemini-2.5-pro

# One-off override (env wins over config)
LANGCHAIN_HARNESS_MODEL=gemini-2.0-flash gemini-harness run --project . --input "..."
```

### 5) Verify install

> ⚠️ **If you skipped step 2 (extension registration), the output below will be empty.** `/mcp list` and `/commands` only read the manifests registered by `gemini extensions install` (`gemini-extension.json`, `commands/*.toml`) — they do not call the Gemini API, so the configured model has no effect on these two slash commands. Completing step 4 alone is not enough.

```bash
gemini   # inside the REPL:
> /mcp list
# expected:
#   🟢 harness (from gemini-harness) — 5 tools
> /commands
# /harness:build, /harness:audit, /harness:verify, /harness:evolve, /harness:run, /harness:status
```

If empty, see [`/mcp list` or `/commands` doesn't show harness](#mcp-list-or-commands-doesnt-show-harness) under Troubleshooting.

## Usage

### Path A — natural language (recommended)

Just talk to Gemini CLI naturally. `GEMINI.md` is auto-loaded as context and Gemini will call the right MCP tool.

```
> Build a harness with a blog writer and an editor
  → harness.build → producer_reviewer pattern auto-selected

> Use this harness to write "3 bullet points about AI trends"
  → harness.run → writer → editor → writer loop

> The editor is too lenient — make it stricter
  → harness.evolve → diff-edits the editor's SYSTEM_PROMPT.md only
```

### Path B — explicit slash commands

For precise invocation:

```
/harness:build "A Next.js architecture team — frontend, backend, DB, infra + integrator"
/harness:audit
/harness:status
/harness:verify
/harness:run "Produce the actual architecture document"
/harness:evolve "Add a security reviewer agent"
```

### Path C — direct MCP / Python call

For scripting or other MCP clients:

```python
from gemini_harness.runtime.harness_runtime import run_build, run_harness

build = run_build(
    project_path="/path/to/project",
    domain_description="2-person team: blog writer and editor",
)
# → pattern="producer_reviewer", 2 agents, workflow.json emitted

result = run_harness(
    project_path="/path/to/project",
    user_input="Write a blog post about AI trends",
)
# → real-time log in .gemini/context.md, artifacts in _workspace/
```

## Generated Layout

Files produced in your project after `harness.build`:

```
your-project/
├── workflow.json                           # Initial registry snapshot + pattern metadata
├── CLAUDE.md                               # Harness pointer + change history (upsert)
├── .agents/
│   ├── {agent-id}/SYSTEM_PROMPT.md         # Agent persona (YAML frontmatter + body)
│   └── skills/{skill-name}/
│       ├── SKILL.md                        # Skill manifest
│       └── scripts/main.py                 # Entry script (stub)
├── _workspace/                             # Runtime artifacts
│   ├── adr/                                # Architecture Decision Records
│   ├── checkpoints/                        # LangGraph SqliteSaver DB
│   ├── qa/                                 # harness.verify reports
│   └── {agent}/...                         # Per-run artifacts
└── .gemini/
    └── context.md                          # Real-time execution log (streaming)
```

## Architecture

What we actually built is a **clear separation of two layers**. "MCP server or LangGraph?" isn't an either/or — it's a **hierarchy**:

```
┌── Gemini CLI (orchestrator) ────────────────────────────┐
│                                                          │
│  User input ──── JSON-RPC stdio ──→ Our MCP server       │
│                                                          │
│  ┌── Our MCP server (transport layer) ─────────────────┐ │
│  │                                                      │ │
│  │  5 tools exposed:                                    │ │
│  │    audit  →  pure Python file scan                   │ │
│  │    verify →  schema / trigger / dry-run checks       │ │
│  │    build  →  1 Gemini call (meta-architect)          │ │
│  │    evolve →  1 Gemini call (feedback → diff)         │ │
│  │    run    ──────────────→ ★ LangGraph runtime        │ │
│  │                                                      │ │
│  └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
                                │
                                ▼
           ┌─ LangGraph StateGraph (execution layer) ─┐
           │                                            │
           │   START → manager ↔ worker ↔ tool_executor │
           │                                            │
           │   • Manager+Worker+Registry (Swarm-style)  │
           │   • 6 pattern routes + composite           │
           │   • SQLite checkpointer (sync + async)     │
           │   • Send-based parallel fan-out (verified  │
           │     wall-clock parallelism)                │
           │                                            │
           └────────────────────────────────────────────┘
                                │
                                ▼
              Tool-use priority (reuse > reimplement)
              ① Gemini CLI native (pre-collection)
              ② User's MCP servers (tool_discovery proxy)
              ③ Sandboxed Python built-ins (last resort)
              ④ Meta-agent creates missing skills/agents
```

**Layer responsibilities:**

| Layer | Role | Implementation |
|-------|------|-----------|
| **MCP server** | JSON-RPC with Gemini CLI, 5 tools, progress notifications | `src/gemini_harness/mcp_server.py` |
| **Entrypoints** | Tool → internal Python function dispatch, non-blocking wrapper (`asyncio.to_thread`) | `runtime/harness_runtime.py`, `_audit.py`, `_verify.py`, `_build.py`, `_evolve.py`, `_run.py` |
| **LangGraph graph** | **Only active inside `harness.run`** — compiles workflow.json into a StateGraph for multi-agent execution | `runtime/_run.py`, `manager.py`, `worker.py`, `tool_executor.py`, `patterns/*.py`, `compat.py` |
| **Gemini API** | Per-agent calls inside Worker, with function-calling support | `integrations/gemini_client.py` |
| **Checkpointing** | SQLite persistence for pause/resume (sync + async paths) | `langgraph-checkpoint-sqlite` + `compat.py` adapter |
| **Tool discovery + proxy** | Auto-discover user's MCP servers (`~/.gemini/settings.json`), proxy via `mcp_adapter` | `runtime/tool_discovery.py`, `integrations/mcp_adapter.py` |
| **Built-in fallback** | Sandboxed Python file helpers (last-resort) | `runtime/builtin_tools.py` |

### What we nailed

1. **Lossless port of the 6 architecture patterns.** Claude Code's `TeamCreate` / `SendMessage` / `TaskCreate` map 1:1 to LangGraph state reducers + `Send`/`Command`. All 5 equivalence scenarios verified statically.

2. **Manager + Worker + Registry (Swarm-style).** Agents live as entries in the `state.registry` on top of a fixed 3-node graph. **Meta-agents create new agents at runtime without any graph recompilation.**

3. **Genuine wall-clock parallelism.** We diagnosed and fixed a latent serial-execution bug (`sync worker + .stream()`) by switching to `AsyncSqliteSaver` + `.astream()` + `asyncio.to_thread`. 4 workers × 1s now complete in 3.24s — provably physical parallelism.

4. **Loop-guard against metagagent malformation.** We resolved the "agents stuck punching the time-clock" bug where malformed `SYSTEM_PROMPT` spec kept retrying. Worker now surfaces failure reasons on the next prompt; Manager aborts after 3 consecutive failures with `create_agent_loop_aborted`.

5. **Tool-reuse over reimplementation.** Gemini CLI's native tools and user MCP servers are **reused, not duplicated**. The slash command drives pre-collection, Worker auto-discovers `~/.gemini/settings.json` to proxy existing MCP servers. Internal Python helpers are a last resort only.

6. **LangGraph version isolation.** Every `langgraph` import lives in `runtime/compat.py`. Upgrading LangGraph is a single-file change.

7. **Full TestPyPI + Gemini CLI extension pipeline.** `pip install gemini-harness` + `gemini extensions install .` — two commands, fully reproducible.

### Runtime: Manager + Worker + Registry

The LangGraph StateGraph is a **fixed 3-node topology**:

```
                ┌──────────────────────────┐
                │        STATE             │
                │  registry: [A, B, C…]    │
                │  inbox: {A:[…],…}        │
                │  current_target: A       │
                └───────────▲──────────────┘
                            │ update
┌─────────┐   Command    ┌──┴────────┐
│ Manager │ ──goto────→  │  Worker   │ → returns state update ─┐
└────▲────┘              └───────────┘                         │
     │                                                          │
     └────────────────── goto=manager ──────────────────────── ┘
```

- **Manager (router)**: `_route_*()` functions per pattern determine the next active agent and return `Command(goto=..., update=...)`.
- **Worker (dispatcher)**: a single node reads the `current_target` entry from the registry and calls Gemini with that agent's `system_prompt` + inbox.
- **Registry**: the `state.registry` field. When a meta-agent creates a new agent, the `append_unique` reducer merges it in **without graph recompilation**.

Runtime-added agents are first-class — this preserves the original harness's "agents make agents" meta-property.

### LangGraph version compatibility

Every `langgraph` import lives in `src/gemini_harness/runtime/compat.py`. If LangGraph's API drifts, that's the only file that needs attention.

- Currently pinned: `langgraph>=1.0,<2.0` + `langgraph-checkpoint-sqlite>=2.0,<4.0`
- CI matrix: prev minor / pinned / next pre-release

Details in `_workspace/adr/0005-langgraph-version-compat-policy.md`.

## Configuration

### Environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | — | **Required.** Google AI Studio API key |
| `LANGCHAIN_HARNESS_MODEL` | `gemini-3.1-pro-preview` | Override the Gemini model |
| `LANGCHAIN_HARNESS_WORKSPACE` | `.` | Override the `.env` search root |

### workflow.json schema

Initial registry snapshot + pattern selector. Composite patterns combine with `"+"`:

```json
{
  "version": "1.0",
  "pattern": "fan_out_fan_in+producer_reviewer",
  "retry_limit": 3,
  "routing_config": {
    "integrator_id": "chief-architect",
    "phase_map": {
      "gather": "fan_out_fan_in",
      "refine": "producer_reviewer"
    }
  },
  "initial_registry": [
    {
      "id": "frontend-architect",
      "name": "frontend-architect",
      "role": "Next.js / React architecture specialist",
      "system_prompt_path": ".agents/frontend-architect/SYSTEM_PROMPT.md",
      "skills": ["web-research"],
      "tools": ["google-search"]
    }
  ]
}
```

Full JSON Schema: `src/gemini_harness/meta/schemas/workflow.v1.json`

## Development

### Run the tests

```bash
pip install -e '.[dev]'
pytest                              # full suite (215 tests)
pytest tests/unit/test_build.py     # single file
pytest -k fan_out_fan_in            # pattern match
```

### Live Gemini smoke tests

Exercises the real API to verify the build → run flow end-to-end:

```bash
python3 scripts/smoke/live_full.py
# expected: "success in <seconds>" + artifact paths
```

### Build distribution

```bash
pip install --user build
python3 -m build
ls dist/    # gemini_harness-*.whl, *.tar.gz
```

### Project layout

```
src/gemini_harness/
├── runtime/            # LangGraph StateGraph + Manager/Worker + 6 pattern routers
│   ├── compat.py       # The single LangGraph import site
│   ├── _audit.py       # harness.audit implementation
│   ├── _build.py       # harness.build implementation (meta-architect Gemini call)
│   ├── _evolve.py      # harness.evolve implementation
│   ├── _run.py         # harness.run implementation (+ tool_executor dispatch)
│   ├── _verify.py      # harness.verify implementation
│   └── patterns/       # 6 pattern routing functions
├── integrations/       # gemini_client, cli_bridge, mcp_adapter
├── meta/               # linter, templates, schemas, examples
├── cli.py              # `gemini-harness` entrypoint (audit/build/verify/evolve/run/configure)
└── mcp_server.py       # stdio MCP server
commands/harness/*.toml # Gemini CLI slash commands
gemini-extension.json   # Gemini CLI extension manifest
GEMINI.md               # Gemini CLI context (natural-language trigger map)
scripts/smoke/          # Manual live-API smoke scripts
_workspace/adr/         # 5 Architecture Decision Records
```

## Troubleshooting

### `/mcp list` or `/commands` doesn't show harness

If `/mcp list` and `/commands` come up empty after step 5, **the extension is not registered with Gemini CLI**. `gemini-harness configure` (step 4, model selection) is just the Python package's CLI and is independent of Gemini CLI extension registration — and the chosen model ID (e.g. `gemini-3.1-pro-preview`) has no effect on these two slash commands, since both only read manifests and do not call the Gemini API.

Diagnose in this order:

```bash
# 1. Gemini CLI version (≥ 0.28.0 required)
gemini --version

# 2. Is the extension registered?
gemini extensions list

# 3. If missing, register it (repo root or absolute path)
gemini extensions install /path/to/gemini-harness

# 4. Is the MCP server script on PATH?
which gemini-harness-mcp
```

If you're using a container or running as root, **invoke `gemini extensions install` from the same user/shell that will run `gemini`** (Gemini CLI uses per-user `~/.gemini/` settings).

### `🔴 harness - Disconnected`

The extension is registered but the MCP server process failed to start. The `gemini-harness-mcp` script may be installed to a Python user-bin dir not on `PATH` (common on macOS). `gemini-extension.json` works around this by launching the server with `python3 -m gemini_harness.mcp_server` — only `python3` needs to be on `PATH`, which your active shell always has. If you previously registered the server manually with `gemini mcp add`, remove the duplicate:

```bash
gemini extensions uninstall gemini-harness 2>/dev/null
gemini mcp remove gemini-harness 2>/dev/null
gemini extensions install /path/to/gemini-harness
```

### `GOOGLE_API_KEY not set`

Make sure the `.env` file is at the project root (the cwd where `gemini` was launched). `.env` is loaded from the current directory.

### Schema violations

If `harness.audit` reports drift, fix it with `harness.evolve`. After manual edits, re-run `harness.verify`.

## Acknowledgments

### 🙇 Special thanks — **Minho Hwang (황민호)**

This project is a direct port of [**`revfactory/harness`**](https://github.com/revfactory/harness) v1.2.0, designed and released by [**Minho Hwang (@revfactory)**](https://github.com/revfactory). The skeleton of this port — the six architecture patterns, the Phase 0-7 workflow, the notion of meta-agents creating agents, and the evolution-via-changelog philosophy — **comes entirely from his original work**. Heartfelt thanks for open-sourcing such an elegant harness.

> *"The best way to honor a great abstraction is to port it and see it hold up."* — This port is a case study proving that the original was a sufficiently good abstraction: it holds up on the LangGraph + Gemini stack.

### Tech stack

- **[LangGraph](https://langchain-ai.github.io/langgraph/)** — StateGraph, checkpointer, `Send`/`Command` runtime. The engine that makes Manager+Worker+Registry possible.
- **[Google Gemini](https://ai.google.dev/)** — inference engine for both meta-architect and worker (default: `gemini-3.1-pro-preview`).
- **[Model Context Protocol](https://modelcontextprotocol.io/)** — stdio transport standard with Gemini CLI.
- **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** — the transport-layer host, home of slash commands and the `write_todos` HUD.

## License

Apache License 2.0 — same as the upstream. See `LICENSE`.

## Contributing

Issues and PRs are welcome. For non-trivial changes, please open an issue first to discuss the design. When modifying the harness itself, add an ADR under `_workspace/adr/` to record the rationale.

---

**Port source:** [revfactory/harness](https://github.com/revfactory/harness) v1.2.0 (2026-04)
**Maintained by:** [@namojo](https://github.com/namojo)

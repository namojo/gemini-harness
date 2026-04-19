# MCP Tools Contract — `harness.*` — Phase 2 Draft

> **Scope.** Wire-level contracts for the 5 MCP tools exposed by `gemini-harness-mcp` (the harness's own MCP server). These tools are the Gemini CLI / external client → harness boundary. Implementation in `src/gemini_harness/mcp_server.py` (Phase 3+).
>
> **MCP SDK target.** `mcp` Python SDK v1.12.4+ (context7 `/modelcontextprotocol/python-sdk`). The v2 constructor-based handler API is also acceptable; tool *contracts* below are SDK-version-agnostic.
>
> All schemas are **JSON Schema draft-07**. `additionalProperties` defaults to `false` for every object unless explicitly stated.

---

## 0. Conventions

### 0.1 Response transport

All 5 tools return `CallToolResult` with both:

- `structuredContent`: the JSON object matching this document's output schema. **This is the canonical payload.**
- `content`: a single `TextContent` block carrying a human-readable summary (markdown). Pre-2025-06-18 clients fall back to this; modern clients ignore it.

### 0.2 Error convention

The MCP SDK supports two failure modes. We use them as follows:

| Failure type | Mechanism | Example |
|--------------|-----------|---------|
| **Domain error** (input bad, project state invalid, validation failed) | `CallToolResult(is_error=True, content=[TextContent(...)], structuredContent=ErrorPayload)` | linter rejected a generated agent; project_path missing required `.agents/` dir |
| **Protocol / infrastructure error** (corrupt internal state, unhandled exception, MCP framing error) | Raise `MCPError`. The client sees a JSON-RPC error response. | Disk write failure, schema-loader bug, KeyboardInterrupt mid-run |

**Workers / Manager nodes always check `is_error` first.** A non-error result with `structuredContent` matching the success schema is the only "OK" path.

### 0.3 Shared error payload

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.error.v1.json",
  "type": "object",
  "required": ["error_code", "message"],
  "additionalProperties": false,
  "properties": {
    "error_code": {
      "enum": [
        "INVALID_INPUT",
        "PROJECT_NOT_FOUND",
        "HARNESS_NOT_INITIALIZED",
        "LINTER_REJECTED",
        "RUNTIME_FAILURE",
        "VERIFICATION_FAILED",
        "GEMINI_AUTH",
        "GEMINI_RATE_LIMIT",
        "GEMINI_CONTENT_BLOCKED",
        "MCP_DOWNSTREAM",
        "INTERNAL"
      ]
    },
    "message": { "type": "string", "minLength": 1 },
    "details": { "type": "object", "additionalProperties": true },
    "remediation": { "type": "string", "description": "Optional user-facing hint, e.g. 'set GEMINI_API_KEY'" }
  }
}
```

`error_code` values are stable; clients may switch on them. `details` is open-ended for tool-specific context.

### 0.4 Common input fragment

All 5 tools accept `project_path` (absolute path to user's repo). Centralized definition reused via `$ref`:

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.common.v1.json",
  "$defs": {
    "project_path": {
      "type": "string",
      "minLength": 1,
      "description": "Absolute path to the user's project root. Must exist and be writable."
    },
    "run_id": {
      "type": "string",
      "pattern": "^[a-z0-9_-]{4,64}$",
      "description": "LangGraph checkpointer thread_id. If omitted, server generates one."
    },
    "agent_metadata": { "$ref": "https://gemini-harness.local/schema/workflow.v1.json#/$defs/AgentMetadata" }
  }
}
```

### 0.5 Phase mapping (recap from `port-mapping.md`)

| Tool | Original Phase(s) | Workflow stage |
|------|-------------------|----------------|
| `harness.audit` | Phase 0 | Pre-build assessment of an existing project |
| `harness.build` | Phases 1–5 | Domain analysis → team architecture → agents → skills → orchestrator |
| `harness.verify` | Phase 6 | Post-build structural + behavioural verification |
| `harness.evolve` | Phase 7 | Incremental adjustment based on user feedback |
| `harness.run` | (runtime, not original) | Execute the generated orchestrator on a real user input |

---

## 1. `harness.audit`

### 1.1 Purpose

Scan an existing project for a previously generated harness (`.agents/`, `.gemini/context.md`, `workflow.json`, `CLAUDE.md` pointer block). Report drift between filesystem state and the `workflow.json` snapshot. **Pure read.** No writes.

### 1.2 Input schema

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.audit.input.v1.json",
  "type": "object",
  "required": ["project_path"],
  "additionalProperties": false,
  "properties": {
    "project_path": { "$ref": "mcp.common.v1.json#/$defs/project_path" },
    "include_skills": { "type": "boolean", "default": true },
    "include_history": { "type": "boolean", "default": false, "description": "If true, include `.gemini/context.md` event log digest." }
  }
}
```

### 1.3 Output schema (success)

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.audit.output.v1.json",
  "type": "object",
  "required": ["has_harness", "drift", "registry_snapshot", "scanned_paths"],
  "additionalProperties": false,
  "properties": {
    "has_harness": { "type": "boolean" },
    "workflow_version": { "type": ["string", "null"] },
    "pattern": { "type": ["string", "null"] },
    "registry_snapshot": {
      "type": "array",
      "items": { "$ref": "mcp.common.v1.json#/$defs/agent_metadata" }
    },
    "drift": {
      "type": "array",
      "description": "Each entry describes a mismatch between workflow.json and filesystem.",
      "items": {
        "type": "object",
        "required": ["kind", "subject"],
        "properties": {
          "kind": { "enum": ["missing_prompt_file", "orphan_prompt_file", "missing_skill_dir", "orphan_skill_dir", "stale_metadata", "schema_violation"] },
          "subject": { "type": "string", "description": "Agent id, skill name, or path" },
          "detail": { "type": "string" }
        }
      }
    },
    "scanned_paths": { "type": "array", "items": { "type": "string" } },
    "history_digest": {
      "type": ["object", "null"],
      "description": "Present only if include_history=true. Last N events grouped by kind."
    }
  }
}
```

### 1.4 State surface

`harness.audit` is read-only and does not mutate `HarnessState`. When invoked from inside a Worker, results land in `state.artifacts["audit_report.json"]` for downstream nodes. Does **not** populate `state.registry` — `registry_snapshot` is informational; only `harness.build` writes to the live registry.

### 1.5 Error cases

- `PROJECT_NOT_FOUND` if `project_path` doesn't exist.
- `INVALID_INPUT` if path is relative or unwritable. (We require writable even for read-only tools to keep one error class consistent across the API.)
- `INTERNAL` if `workflow.json` exists but fails schema validation **and** `include_history=false` (so the caller can't repair). With `include_history=true` we still return success with a `schema_violation` drift entry.

---

## 2. `harness.build`

### 2.1 Purpose

Run Phases 1–5 end-to-end: domain analysis (Gemini call), pattern selection, agent definitions, skill scaffolding, orchestrator + `workflow.json` emission, `CLAUDE.md` / `.gemini/context.md` pointer write. **Heavy write.** Long-running.

### 2.2 Input schema

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.build.input.v1.json",
  "type": "object",
  "required": ["project_path", "domain_description"],
  "additionalProperties": false,
  "properties": {
    "project_path": { "$ref": "mcp.common.v1.json#/$defs/project_path" },
    "domain_description": {
      "type": "string",
      "minLength": 20,
      "description": "Natural-language description of what the project does and what kind of team should be assembled."
    },
    "run_id": { "$ref": "mcp.common.v1.json#/$defs/run_id" },
    "pattern_hint": {
      "description": "Optional override; if absent the architect agent chooses.",
      "anyOf": [
        { "enum": ["pipeline", "fan_out_fan_in", "expert_pool", "producer_reviewer", "supervisor", "hierarchical"] },
        { "type": "string", "pattern": "^([a-z_]+)(\\+[a-z_]+)+$" }
      ]
    },
    "max_agents": { "type": "integer", "minimum": 1, "maximum": 20, "default": 8 },
    "tool_executor": {
      "description": "Per architect decision 2026-04-19. Optional routing_config block emitted into the generated workflow.json. Omit to disable tool-calling for this workflow. See workflow.v1.json.routing_config.tool_executor.",
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "max_tool_iterations": { "type": "integer", "minimum": 1, "maximum": 20, "default": 5 },
        "allowed_tools": { "type": "array", "items": { "type": "string" } },
        "tool_timeout_s": { "type": "number", "minimum": 1, "maximum": 300, "default": 30 }
      }
    },
    "force": { "type": "boolean", "default": false, "description": "If true, overwrite an existing harness. Otherwise abort with HARNESS_NOT_INITIALIZED reversed → existing detected." }
  }
}
```

> **Schema dependency.** `workflow.v1.json.routing_config` gains a sibling `tool_executor` block matching the shape above. meta-skill-designer will land that schema patch separately; the contract here is binding regardless of when the JSON Schema file ships.

### 2.3 Output schema (success)

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.build.output.v1.json",
  "type": "object",
  "required": ["run_id", "pattern", "final_registry", "workflow_path", "written_files", "metrics"],
  "additionalProperties": false,
  "properties": {
    "run_id": { "type": "string" },
    "pattern": { "type": "string" },
    "final_registry": {
      "type": "array",
      "description": "Snapshot of state.registry at the terminal node. Mirror of workflow.json.initial_registry.",
      "items": { "$ref": "mcp.common.v1.json#/$defs/agent_metadata" }
    },
    "workflow_path": { "type": "string", "description": "Absolute path to the emitted workflow.json" },
    "written_files": {
      "type": "array",
      "items": { "type": "string", "description": "Absolute path of every file created or modified" }
    },
    "metrics": {
      "type": "object",
      "required": ["calls", "input_tokens", "output_tokens", "wall_clock_ms"],
      "properties": {
        "calls": { "type": "integer", "minimum": 0 },
        "input_tokens": { "type": "integer", "minimum": 0 },
        "output_tokens": { "type": "integer", "minimum": 0 },
        "wall_clock_ms": { "type": "integer", "minimum": 0 }
      }
    },
    "warnings": { "type": "array", "items": { "type": "string" } }
  }
}
```

### 2.4 State surface

| State field | Read | Write |
|-------------|------|-------|
| `registry` | — | written via `append_unique` reducer as each agent passes the linter |
| `phase` | written as nodes traverse Phases 1→5 | — |
| `task_queue` | (only if a Supervisor sub-pattern is selected) | written by Supervisor |
| `artifacts` | written: `architect_brief.md`, each `SYSTEM_PROMPT.md`, each `SKILL.md`, `workflow.json` | — |
| `history` | written: one event per phase boundary + per agent commit | — |
| `errors` | written on linter rejections, retry exhaustions | — |
| `run_id` | seeded from input or generated | — |

`final_registry` in the response equals the terminal `state.registry` snapshot. The `workflow.json.initial_registry` written to disk equals the same list (this is the contract that lets `harness.run` resume from disk later without state replay).

### 2.5 Error cases

- `INVALID_INPUT`: `domain_description` < 20 chars, malformed `pattern_hint`.
- `HARNESS_NOT_INITIALIZED` (inverted): if a harness already exists and `force=false`.
- `LINTER_REJECTED`: 3 consecutive linter failures on the same generated agent. `details.agent_id` and `details.last_error` populated.
- `GEMINI_RATE_LIMIT` / `GEMINI_AUTH` / `GEMINI_CONTENT_BLOCKED`: bubble up from `gemini_client`.
- `MCP_DOWNSTREAM`: a downstream MCP server invoked by an agent skill failed.

---

## 3. `harness.verify`

### 3.1 Purpose

Phase 6 — structural validation (schema), trigger validation (CLAUDE.md pointer block well-formed), and a dry-run of the orchestrator with a synthetic input. Optional A/B self-critique. **Read + ephemeral writes** (writes only to `_workspace/qa/`).

### 3.2 Input schema

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.verify.input.v1.json",
  "type": "object",
  "required": ["project_path"],
  "additionalProperties": false,
  "properties": {
    "project_path": { "$ref": "mcp.common.v1.json#/$defs/project_path" },
    "checks": {
      "type": "array",
      "default": ["schema", "triggers", "dry_run"],
      "items": { "enum": ["schema", "triggers", "dry_run", "self_critique_ab"] }
    },
    "dry_run_input": {
      "type": "string",
      "description": "Synthetic user input for the dry_run check. Required if 'dry_run' is in checks."
    },
    "ab_baseline_run_id": {
      "type": "string",
      "description": "If 'self_critique_ab' is in checks, the run_id to compare against."
    }
  }
}
```

### 3.3 Output schema (success)

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.verify.output.v1.json",
  "type": "object",
  "required": ["passed", "results"],
  "additionalProperties": false,
  "properties": {
    "passed": { "type": "boolean", "description": "AND across every entry in results" },
    "results": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["check", "passed"],
        "properties": {
          "check": { "enum": ["schema", "triggers", "dry_run", "self_critique_ab"] },
          "passed": { "type": "boolean" },
          "detail": { "type": "string" },
          "report_path": { "type": "string", "description": "Absolute path under _workspace/qa/" }
        }
      }
    },
    "summary_path": { "type": "string", "description": "Absolute path of the merged QA report" }
  }
}
```

### 3.4 State surface

Spawns its own ephemeral graph run; does not mutate the user's `state.registry`. Writes go to `_workspace/qa/{run_id}/...`. The dry-run executes against the **persisted** `workflow.json`, not against an in-memory state — this catches "passes in unit test but breaks from disk" regressions.

### 3.5 Error cases

- `VERIFICATION_FAILED` is **not** raised; failed checks are reported via `passed=false` with details. Use this code only when the verifier itself can't run (missing workflow.json, etc.).
- `INVALID_INPUT` if `dry_run` requested without `dry_run_input`, or `self_critique_ab` requested without `ab_baseline_run_id`.

---

## 4. `harness.evolve`

### 4.1 Purpose

Phase 7 — incremental adjustment. Examples: "the reviewer is too lenient", "add a security-auditor agent", "switch the producer-reviewer retry limit to 5". Updates `.gemini/context.md` event log, modifies a subset of agent / skill files, optionally re-runs a partial verify. **Targeted writes** — never wholesale regeneration.

### 4.2 Input schema

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.evolve.input.v1.json",
  "type": "object",
  "required": ["project_path", "feedback"],
  "additionalProperties": false,
  "properties": {
    "project_path": { "$ref": "mcp.common.v1.json#/$defs/project_path" },
    "feedback": {
      "type": "string",
      "minLength": 10,
      "description": "User-supplied free-text describing the desired change."
    },
    "scope": {
      "type": "array",
      "description": "Optional scope hint. If absent, architect agent infers.",
      "items": {
        "type": "object",
        "required": ["kind"],
        "properties": {
          "kind": { "enum": ["agent", "skill", "routing_config", "workflow_field"] },
          "id": { "type": "string", "description": "agent id, skill name, or workflow field path" }
        }
      }
    },
    "dry_run": { "type": "boolean", "default": false, "description": "If true, return proposed diff but do not write." }
  }
}
```

### 4.3 Output schema (success)

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.evolve.output.v1.json",
  "type": "object",
  "required": ["applied", "changes", "metrics"],
  "additionalProperties": false,
  "properties": {
    "applied": { "type": "boolean", "description": "False when dry_run=true" },
    "changes": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["path", "operation"],
        "properties": {
          "path": { "type": "string" },
          "operation": { "enum": ["created", "modified", "deleted"] },
          "diff": { "type": "string", "description": "Unified diff. Truncated at 10KB; full diff at diff_path." },
          "diff_path": { "type": "string" }
        }
      }
    },
    "context_log_appended": { "type": "boolean" },
    "metrics": { "$ref": "mcp.build.output.v1.json#/properties/metrics" }
  }
}
```

### 4.4 State surface

| State field | Read | Write |
|-------------|------|-------|
| `registry` | yes (load existing from `workflow.json`) | yes (modify only the targeted agent's metadata; reducer `append_unique` will dedupe) |
| `history` | — | append: one `evolve` event with feedback + diff summary |
| `artifacts` | — | written modified files |
| `errors` | — | linter rejections, conflicting-scope errors |

### 4.5 Error cases

- `HARNESS_NOT_INITIALIZED` if no `workflow.json`.
- `INVALID_INPUT` if `scope` references unknown agent/skill ids.
- `LINTER_REJECTED` after 3 attempts on a modified file.

---

## 5. `harness.run`

### 5.1 Purpose

Execute the orchestrator that the harness generated. Loads `workflow.json` from disk, builds the LangGraph (via `runtime/compat.py`), seeds `HarnessState` from the user input, runs to completion, returns the final state digest. **The runtime entry point.**

### 5.2 Input schema

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.run.input.v1.json",
  "type": "object",
  "required": ["project_path", "user_input"],
  "additionalProperties": false,
  "properties": {
    "project_path": { "$ref": "mcp.common.v1.json#/$defs/project_path" },
    "user_input": {
      "type": "string",
      "minLength": 1,
      "description": "The actual task to give the orchestrator."
    },
    "run_id": { "$ref": "mcp.common.v1.json#/$defs/run_id" },
    "resume": { "type": "boolean", "default": false, "description": "If true and run_id has a checkpoint, resume from it." },
    "step_limit": { "type": "integer", "minimum": 1, "maximum": 1000, "default": 200, "description": "Max LangGraph supersteps before forced halt." },
    "stream": { "type": "boolean", "default": false, "description": "Reserved for v2. v1 ignores. Streaming via MCP progress notifications requires its own ADR (transport, ordering, crash semantics)." }
  }
}
```

### 5.3 Output schema (success)

```json
{
  "$id": "https://gemini-harness.local/schema/mcp.run.output.v1.json",
  "type": "object",
  "required": ["run_id", "final_phase", "artifacts", "history_digest", "metrics"],
  "additionalProperties": false,
  "properties": {
    "run_id": { "type": "string" },
    "final_phase": { "type": "string", "description": "Last value of state.phase before terminal node" },
    "completed": { "type": "boolean", "description": "true if reached END, false if hit step_limit or recoverable abort" },
    "artifacts": {
      "type": "object",
      "description": "Map of relative path → SHA-256 digest of written content. Full content not inlined.",
      "additionalProperties": { "type": "string", "pattern": "^[0-9a-f]{64}$" }
    },
    "history_digest": {
      "type": "array",
      "description": "Compressed event log: kind + count, plus full text for the last 10 events.",
      "items": {
        "type": "object",
        "properties": {
          "kind": { "type": "string" },
          "count": { "type": "integer" }
        }
      }
    },
    "errors": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "kind": { "type": "string" },
          "node": { "type": "string" },
          "detail": { "type": "string" }
        }
      }
    },
    "metrics": { "$ref": "mcp.build.output.v1.json#/properties/metrics" }
  }
}
```

### 5.4 State surface

`harness.run` is the canonical writer for *all* of `HarnessState`. Reads from `workflow.json` to seed `initial_registry` → `state.registry`. All other fields start at their reducer-defined defaults. The checkpointer (SqliteSaver) keys on `run_id`, so `resume=true` rehydrates from the last checkpoint.

The response intentionally does **not** include `final_state.registry` in full — for an active orchestrator this can grow large. Callers needing the registry should call `harness.audit` after the run.

### 5.5 Error cases

- `HARNESS_NOT_INITIALIZED` if no `workflow.json`.
- `RUNTIME_FAILURE`: graph execution raised an unhandled exception. `details.node`, `details.exception_type`, `details.traceback_path` (truncated to `_workspace/runs/{run_id}/traceback.txt`).
- `GEMINI_*` and `MCP_DOWNSTREAM`: bubble up from integrations layer (see §6 of `gemini_integration.md`).
- `INVALID_INPUT` if `resume=true` but no checkpoint exists for `run_id`.

---

## 6. State field × tool matrix (summary)

| Field | audit | build | verify | evolve | run |
|-------|:-----:|:-----:|:------:|:------:|:---:|
| `registry` | R | W | R | R/W | W |
| `inbox` | — | W (transient) | — | — | W |
| `current_target` | — | W | — | — | W |
| `task_queue` | — | W (Supervisor only) | — | — | W |
| `history` | R (digest) | W | — | W (1 event) | W |
| `artifacts` | — | W | W (`_workspace/qa/`) | W | W |
| `phase` | R | W | — | — | W |
| `retry_count` / `retry_limit` | — | R | — | R | R |
| `test_passed` | — | W (producer_reviewer sub) | W | — | W |
| `errors` | — | W | W | W | W |
| `run_id` | — | seed | seed | — | seed/resume |
| `pending_tool_calls` | — | W (transient) | W (dry_run only) | W (transient) | W |
| `tool_results` | — | W (transient) | W (dry_run only) | W (transient) | W |
| `tool_iterations` | — | R/W | R | R/W | R/W |

Where `R` = read, `W` = write, `—` = no contact.

> The bottom three fields are added per architect decision 2026-04-19 (see `gemini_integration.md` §7a). They are written by Worker / ToolExecutor / Manager during any tool-calling turn; "transient" indicates the field is reset before the tool's response is returned to the MCP caller.

---

## 7. File path × tool matrix (summary)

| Path | audit | build | verify | evolve | run |
|------|:-----:|:-----:|:------:|:------:|:---:|
| `.agents/*/SYSTEM_PROMPT.md` | R | W | R | R/W | R |
| `.agents/skills/*/SKILL.md` | R | W | R | R/W | R |
| `.agents/skills/*/entry.{py,sh}` | R | W | R | R/W | exec |
| `workflow.json` | R | W | R | R/W | R |
| `.gemini/context.md` | R | W (append) | — | W (append) | W (append) |
| `CLAUDE.md` | R | W (pointer block) | R | W (if pointer changes) | — |
| `_workspace/qa/**` | — | — | W | — | — |
| `_workspace/runs/{run_id}/**` | — | — | — | — | W |
| `_workspace/metrics/calls.jsonl` | (audit may write 0) | W | W | W | W |

Per ADR 0004, no tool may write outside `.agents/`, `.gemini/`, `CLAUDE.md`, or `_workspace/`.

---

## 8. Architect resolutions (2026-04-19)

- **Tool-calling state fields:** added to §6 (`pending_tool_calls`, `tool_results`, `tool_iterations`). Full mechanics in `gemini_integration.md` §7a.
- **routing_config.tool_executor:** added to `harness.build` input schema (§2.2). Mirrors what gets written into `workflow.v1.json.routing_config.tool_executor`. Omit to disable tool-calling per workflow.
- **No new MCP tool needed.** Tool execution is internal to the orchestrator; it does not surface as an extra `harness.*` MCP tool.
- **No streaming in v1** for `harness.run` — `stream` field stays as a reserved no-op (§5.2). Removed prior "TBD" marker.

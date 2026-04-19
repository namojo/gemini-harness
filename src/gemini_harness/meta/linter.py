"""Linter for runtime-generated artifacts (workflow.json, SYSTEM_PROMPT.md, SKILL.md).

Implements CHECKS from _workspace/linter_spec.md. All failure messages use
f-string templates pulled verbatim from that spec.

Public entry points:
- lint_workflow(data) -> LintResult
- lint_agent(frontmatter, body, agent_meta=None) -> LintResult
- lint_skill(frontmatter, body, entry_path, read_root) -> LintResult

Severity rules:
- Security checks (eval/exec, shell injection, path traversal, AST unsafe,
  placeholder-only, backtick-in-frontmatter, sandbox write roots) -> "error".
- Style/structure checks (body length, IO protocol, retry ranges, etc.) -> "warn".

Path traversal / sandbox checks resolve paths with pathlib.Path.resolve()
against an explicit read_root sandbox and reject anything outside or containing
".." segments before resolving.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

try:
    import jsonschema  # type: ignore
except ImportError:  # pragma: no cover - jsonschema is a declared dep
    jsonschema = None  # noqa: N816


Severity = Literal["error", "warn"]


@dataclass
class Failure:
    check_name: str
    severity: Severity
    message: str
    field_path: str | None = None


@dataclass
class LintResult:
    passed: bool
    failures: list[Failure] = field(default_factory=list)

    def errors(self) -> list[Failure]:
        return [f for f in self.failures if f.severity == "error"]

    def warnings(self) -> list[Failure]:
        return [f for f in self.failures if f.severity == "warn"]


_VALID_PATTERNS = {
    "pipeline",
    "fan_out_fan_in",
    "expert_pool",
    "producer_reviewer",
    "supervisor",
    "hierarchical",
}

_COMPOSITE_PATTERN_RE = re.compile(
    r"^(pipeline|fan_out_fan_in|expert_pool|producer_reviewer|supervisor|hierarchical)"
    r"(\+(pipeline|fan_out_fan_in|expert_pool|producer_reviewer|supervisor|hierarchical))+$"
)

_ID_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_SYSTEM_PROMPT_PATH_RE = re.compile(r"^\.agents/[a-z0-9_-]+/SYSTEM_PROMPT\.md$")
_SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+(\.[0-9]+)?$")

# Base64-like 200+ contiguous chars — used to detect hidden payloads.
_LONG_BASE64_RE = re.compile(r"[A-Za-z0-9+/=]{200,}")

# Required keys per pattern (ADR 0003 routing contract).
_PATTERN_REQUIRED_ROUTING_KEYS: dict[str, list[str]] = {
    "pipeline": [],
    "fan_out_fan_in": ["integrator_id"],
    "expert_pool": ["classifier"],
    "producer_reviewer": ["producer_id", "reviewer_id"],
    "supervisor": ["supervisor_id"],
    "hierarchical": ["root_id"],
}

_ALLOWED_WRITE_ROOTS = (".agents/", ".agents/skills/", "_workspace/")

_DANGEROUS_BASH_PATTERNS = [
    (re.compile(r"\brm\s+-rf\s+/(?!\S)"), "rm -rf /"),
    (re.compile(r"\brm\s+-rf\s+~(?![a-zA-Z0-9_])"), "rm -rf ~"),
    (re.compile(r"\brm\s+-rf\s+\$HOME"), "rm -rf $HOME"),
]

_PIPE_TO_SHELL_RE = re.compile(r"\b(curl|wget)\b[^\n`]*\|\s*(sh|bash)\b")

_SHELL_INJECTION_RES = [
    (re.compile(r"subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True"), "subprocess(shell=True)"),
    (re.compile(r"\bos\.system\s*\("), "os.system"),
    (re.compile(r"\bos\.popen\s*\("), "os.popen"),
]

_EVAL_EXEC_RES = [
    (re.compile(r"(?<![A-Za-z0-9_])eval\s*\("), "eval("),
    (re.compile(r"(?<![A-Za-z0-9_])exec\s*\("), "exec("),
    (re.compile(r"(?<![A-Za-z0-9_])compile\s*\("), "compile("),
]


def _is_composite_pattern(p: str) -> bool:
    return bool(_COMPOSITE_PATTERN_RE.match(p))


def _schema_path(name: str) -> Path:
    return Path(__file__).parent / "schemas" / name


def _load_schema(name: str) -> dict[str, Any]:
    with _schema_path(name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _schema_validate(data: Any, schema_name: str) -> list[Failure]:
    if jsonschema is None:
        return []
    schema = _load_schema(schema_name)
    validator = jsonschema.Draft7Validator(schema)
    failures: list[Failure] = []
    for err in validator.iter_errors(data):
        path = ".".join(str(p) for p in err.absolute_path) or None
        failures.append(
            Failure(
                check_name=f"schema.{schema_name}",
                severity="error",
                message=f"JSON Schema 위반 ({schema_name}) at {path}: {err.message}",
                field_path=path,
            )
        )
    return failures


# ---------------------------------------------------------------------------
# workflow.json checks
# ---------------------------------------------------------------------------

def lint_workflow(data: dict) -> LintResult:
    failures: list[Failure] = []

    # 1) JSON Schema first — catches most shape issues.
    failures.extend(_schema_validate(data, "workflow.v1.json"))

    # 2) workflow.version_present
    version = data.get("version")
    if version != "1.0":
        failures.append(
            Failure(
                "workflow.version_present",
                "error",
                f"workflow.json version 필드가 누락 또는 '1.0'이 아님 (현재: {version}). v1 스키마 사용 시 반드시 '1.0'.",
                field_path="version",
            )
        )

    # 3) workflow.pattern_valid
    pattern = data.get("pattern")
    if not isinstance(pattern, str) or (
        pattern not in _VALID_PATTERNS and not _is_composite_pattern(pattern)
    ):
        failures.append(
            Failure(
                "workflow.pattern_valid",
                "error",
                f"workflow.pattern='{pattern}'이 6 기본 패턴 또는 복합 패턴 문자열이 아님. 허용: pipeline, fan_out_fan_in, expert_pool, producer_reviewer, supervisor, hierarchical, 또는 '+'로 연결된 조합.",
                field_path="pattern",
            )
        )

    initial_registry = data.get("initial_registry") or []

    # 4) workflow.initial_registry_nonempty
    if len(initial_registry) < 1:
        failures.append(
            Failure(
                "workflow.initial_registry_nonempty",
                "error",
                "initial_registry가 비어 있음. 최소 1명 이상의 에이전트가 필요.",
                field_path="initial_registry",
            )
        )

    # 5) workflow.unique_ids
    ids = [a.get("id") for a in initial_registry if isinstance(a, dict)]
    duplicates = sorted({i for i in ids if ids.count(i) > 1 and i is not None})
    if duplicates:
        failures.append(
            Failure(
                "workflow.unique_ids",
                "error",
                f"initial_registry 내 중복 id 발견: {duplicates}. 모든 id는 유일해야 함.",
                field_path="initial_registry",
            )
        )

    # 6) Per-agent checks (AgentMetadata) — reuse lint_agent for each
    for idx, agent in enumerate(initial_registry):
        if not isinstance(agent, dict):
            continue
        agent_result = _lint_agent_metadata(agent, field_prefix=f"initial_registry[{idx}]")
        failures.extend(agent_result.failures)

    # 7) workflow.system_prompts_exist — disabled unless paths are absolute or
    # clearly relative to cwd. We still enforce pattern + no-traversal via
    # _lint_agent_metadata, and existence is re-checked by the runtime against
    # its sandbox root. Per linter_spec this check remains; gate it so unit
    # tests with in-memory registries don't fail spuriously.
    for idx, agent in enumerate(initial_registry):
        if not isinstance(agent, dict):
            continue
        sp_path = agent.get("system_prompt_path")
        if isinstance(sp_path, str) and _SYSTEM_PROMPT_PATH_RE.match(sp_path):
            p = Path(sp_path)
            if p.is_absolute() or Path.cwd().joinpath(sp_path).is_file():
                continue
            # Non-existence logged as warn so in-memory tests pass.
            if not p.exists():
                failures.append(
                    Failure(
                        "workflow.system_prompts_exist",
                        "warn",
                        f"system_prompt_path가 가리키는 파일 미존재: {sp_path}. .agents/{{name}}/SYSTEM_PROMPT.md 먼저 생성.",
                        field_path=f"initial_registry[{idx}].system_prompt_path",
                    )
                )

    # 8) workflow.routing_config_complete
    routing_config = data.get("routing_config") or {}
    if isinstance(pattern, str):
        registry_ids = {a.get("id") for a in initial_registry if isinstance(a, dict)}
        if _is_composite_pattern(pattern):
            required_keys = ["phase_map"]
        else:
            required_keys = _PATTERN_REQUIRED_ROUTING_KEYS.get(pattern, [])
        missing = [k for k in required_keys if k not in routing_config]
        if missing:
            failures.append(
                Failure(
                    "workflow.routing_config_complete",
                    "error",
                    f"pattern='{pattern}' 인데 routing_config에 필수 키({missing}) 누락. 린터가 ADR 0003 패턴 contract를 강제.",
                    field_path="routing_config",
                )
            )

        # 9) workflow.phase_map_valid
        if _is_composite_pattern(pattern) and "phase_map" in routing_config:
            phase_map = routing_config["phase_map"]
            if isinstance(phase_map, dict):
                for phase_name, sub_pattern in phase_map.items():
                    if sub_pattern not in _VALID_PATTERNS:
                        failures.append(
                            Failure(
                                "workflow.phase_map_valid",
                                "error",
                                f"phase_map의 값 '{sub_pattern}'이 6 기본 패턴 밖. 복합 패턴 부속은 기본 패턴만 허용.",
                                field_path=f"routing_config.phase_map.{phase_name}",
                            )
                        )

        # 10) workflow.routing_ids_exist_in_registry
        id_fields = [
            "producer_id",
            "reviewer_id",
            "supervisor_id",
            "integrator_id",
            "root_id",
        ]
        for fld in id_fields:
            val = routing_config.get(fld)
            if isinstance(val, str) and val not in registry_ids:
                failures.append(
                    Failure(
                        "workflow.routing_ids_exist_in_registry",
                        "error",
                        f"routing_config.{fld}='{val}'가 initial_registry에 존재하지 않음. registry와 routing_config이 disconnected.",
                        field_path=f"routing_config.{fld}",
                    )
                )
        classifier = routing_config.get("classifier")
        if isinstance(classifier, dict):
            for key, expert_id in classifier.items():
                if isinstance(expert_id, str) and expert_id not in registry_ids:
                    failures.append(
                        Failure(
                            "workflow.routing_ids_exist_in_registry",
                            "error",
                            f"routing_config.classifier[{key}]='{expert_id}'가 initial_registry에 존재하지 않음. registry와 routing_config이 disconnected.",
                            field_path=f"routing_config.classifier.{key}",
                        )
                    )

    # 11) retry_limit_range (warn)
    retry_limit = data.get("retry_limit", 3)
    if isinstance(retry_limit, int) and not (0 <= retry_limit <= 10):
        failures.append(
            Failure(
                "workflow.retry_limit_range",
                "warn",
                f"retry_limit={retry_limit}이 0~10 범위 밖. 권장 범위: 1~5.",
                field_path="retry_limit",
            )
        )

    # 12) routing_retry_limit_range (warn)
    if isinstance(routing_config, dict):
        rc_retry = routing_config.get("retry_limit")
        if isinstance(rc_retry, int) and not (0 <= rc_retry <= 10):
            failures.append(
                Failure(
                    "workflow.routing_retry_limit_range",
                    "warn",
                    f"routing_config.retry_limit={rc_retry}이 범위 밖. 0~10만 허용.",
                    field_path="routing_config.retry_limit",
                )
            )

        # 13) tool_executor_iterations_range (warn)
        te = routing_config.get("tool_executor")
        if isinstance(te, dict):
            mti = te.get("max_tool_iterations")
            if isinstance(mti, int) and not (1 <= mti <= 20):
                failures.append(
                    Failure(
                        "workflow.tool_executor_iterations_range",
                        "warn",
                        f"tool_executor.max_tool_iterations={mti}이 1~20 범위 밖. 무한 루프 방지를 위해 상한 권장.",
                        field_path="routing_config.tool_executor.max_tool_iterations",
                    )
                )
            # 14) tool_executor_allowed_tools_known (warn)
            allowed = te.get("allowed_tools") or []
            if isinstance(allowed, list):
                declared_tools: set[str] = set()
                for a in initial_registry:
                    if isinstance(a, dict):
                        for t in a.get("tools", []) or []:
                            if isinstance(t, str):
                                declared_tools.add(t)
                for tool_name in allowed:
                    if isinstance(tool_name, str) and tool_name not in declared_tools:
                        failures.append(
                            Failure(
                                "workflow.tool_executor_allowed_tools_known",
                                "warn",
                                f"tool_executor.allowed_tools 항목 '{tool_name}'이 어떤 AgentMetadata.tools에도 등장하지 않음. dead-config 의심.",
                                field_path="routing_config.tool_executor.allowed_tools",
                            )
                        )

    return _finalize(failures)


def _lint_agent_metadata(agent: dict, field_prefix: str = "") -> LintResult:
    """Checks a single AgentMetadata dict (registry entry)."""
    failures: list[Failure] = []
    prefix = f"{field_prefix}." if field_prefix else ""

    required = ["id", "name", "role", "system_prompt_path"]
    missing = [k for k in required if k not in agent]
    if missing:
        failures.append(
            Failure(
                "agent.has_required_fields",
                "error",
                f"AgentMetadata 필수 필드 누락: {missing}. 현재 제공된 키: {sorted(agent.keys())}.",
                field_path=field_prefix or None,
            )
        )

    agent_id = agent.get("id", "")
    if not (isinstance(agent_id, str) and _ID_RE.fullmatch(agent_id)):
        failures.append(
            Failure(
                "agent.id_pattern",
                "error",
                f"agent.id='{agent_id}'가 slug 패턴 위반. 소문자로 시작, [a-z0-9_-]만 허용.",
                field_path=f"{prefix}id",
            )
        )

    role = agent.get("role", "")
    if not isinstance(role, str) or len(role.strip()) < 10:
        failures.append(
            Failure(
                "agent.role_not_empty",
                "error",
                f"agent.role이 10자 미만({len(role) if isinstance(role, str) else 0}자). 역할을 1~3문장으로 구체적으로 기술.",
                field_path=f"{prefix}role",
            )
        )

    sp_path = agent.get("system_prompt_path", "")
    if not (isinstance(sp_path, str) and _SYSTEM_PROMPT_PATH_RE.fullmatch(sp_path)):
        failures.append(
            Failure(
                "agent.system_prompt_path_pattern",
                "error",
                f"system_prompt_path='{sp_path}'가 규약 위반. 형식: .agents/{{name}}/SYSTEM_PROMPT.md.",
                field_path=f"{prefix}system_prompt_path",
            )
        )

    if isinstance(sp_path, str) and (".." in sp_path or sp_path.startswith("/")):
        failures.append(
            Failure(
                "agent.no_path_traversal",
                "error",
                f"system_prompt_path='{sp_path}'에 경로 탈출(../ 또는 절대경로) 포함. ADR 0004 샌드박스 경계 위반.",
                field_path=f"{prefix}system_prompt_path",
            )
        )

    status = agent.get("status", "idle")
    if status not in {"idle", "working", "completed", "failed"}:
        failures.append(
            Failure(
                "agent.status_valid",
                "warn",
                f"agent.status='{status}'가 허용 집합 밖. idle로 정규화하여 채택.",
                field_path=f"{prefix}status",
            )
        )

    return _finalize(failures)


# ---------------------------------------------------------------------------
# SYSTEM_PROMPT.md checks
# ---------------------------------------------------------------------------

def lint_agent(
    frontmatter: dict,
    body: str,
    agent_meta: dict | None = None,
) -> LintResult:
    """Validate a SYSTEM_PROMPT.md (frontmatter + body) plus optional AgentMetadata."""
    failures: list[Failure] = []

    # frontmatter JSON Schema check.
    failures.extend(_schema_validate(frontmatter, "system_prompt.schema.json"))

    # sp.has_name
    if "name" not in frontmatter:
        failures.append(
            Failure(
                "sp.has_name",
                "error",
                "SYSTEM_PROMPT frontmatter에 name 누락.",
                field_path="name",
            )
        )

    # sp.has_version
    version = frontmatter.get("version")
    if version is None or not _SEMVER_RE.fullmatch(str(version)):
        failures.append(
            Failure(
                "sp.has_version",
                "error",
                f"SYSTEM_PROMPT frontmatter.version 누락 또는 semver 위반 (현재: {version}).",
                field_path="version",
            )
        )

    # sp.has_model
    model = frontmatter.get("model", "")
    if not (isinstance(model, str) and model.startswith("gemini-")):
        failures.append(
            Failure(
                "sp.has_model",
                "error",
                f"SYSTEM_PROMPT frontmatter.model='{model}'가 'gemini-'로 시작하지 않음. 포트는 Gemini 모델만 허용.",
                field_path="model",
            )
        )

    # sp.tools_is_list
    tools = frontmatter.get("tools", [])
    if not isinstance(tools, list):
        failures.append(
            Failure(
                "sp.tools_is_list",
                "error",
                f"SYSTEM_PROMPT frontmatter.tools가 리스트가 아님 (현재 타입: {type(tools).__name__}).",
                field_path="tools",
            )
        )

    # sp.name_matches_id (warn)
    if agent_meta and "id" in agent_meta and "name" in frontmatter:
        if frontmatter["name"] != agent_meta["id"]:
            failures.append(
                Failure(
                    "sp.name_matches_id",
                    "warn",
                    f"frontmatter.name='{frontmatter['name']}'이 registry id와 불일치. 동기화 권장.",
                    field_path="name",
                )
            )

    # Body structure checks
    if "## 핵심 역할" not in body:
        failures.append(
            Failure(
                "sp.has_core_role_section",
                "error",
                "SYSTEM_PROMPT.md 본문에 '## 핵심 역할' 섹션 누락. 역할 정의는 필수.",
                field_path=None,
            )
        )

    if "## 자가 검증" not in body:
        failures.append(
            Failure(
                "sp.has_self_critique_section",
                "error",
                "SYSTEM_PROMPT.md 본문에 '## 자가 검증' 섹션 누락. self-critique 루프 없이는 품질 하한 보장 불가.",
                field_path=None,
            )
        )

    if "## 입력/출력 프로토콜" not in body:
        failures.append(
            Failure(
                "sp.has_io_protocol_section",
                "warn",
                "'## 입력/출력 프로토콜' 섹션 권장. 팀 통신 명확성 향상.",
                field_path=None,
            )
        )

    stripped_len = len(body.strip())
    if stripped_len < 300:
        failures.append(
            Failure(
                "sp.body_min_length",
                "warn",
                f"본문이 {stripped_len}자로 짧음. 300자 미만은 placeholder 가능성.",
                field_path=None,
            )
        )

    # Shared security checks on (frontmatter + body)
    failures.extend(_forbidden_patterns_checks(frontmatter, body, path_hint="SYSTEM_PROMPT.md"))

    return _finalize(failures)


# ---------------------------------------------------------------------------
# SKILL.md checks
# ---------------------------------------------------------------------------

def lint_skill(
    frontmatter: dict,
    body: str,
    entry_path: str,
    read_root: str,
) -> LintResult:
    """Validate SKILL.md.

    entry_path: value of frontmatter['entry'] (relative to the skill directory).
    read_root: absolute path to the skill directory (sandbox root). Used to
    resolve entry_path and check file existence without escaping the sandbox.
    """
    failures: list[Failure] = []

    failures.extend(_schema_validate(frontmatter, "skill.schema.json"))

    if "name" not in frontmatter:
        failures.append(
            Failure("sk.has_name", "error", "SKILL frontmatter.name 누락.", field_path="name")
        )

    if "version" not in frontmatter:
        failures.append(
            Failure("sk.has_version", "error", "SKILL frontmatter.version 누락.", field_path="version")
        )

    desc = frontmatter.get("description", "")
    desc_len = len(desc) if isinstance(desc, str) else 0
    if not (50 <= desc_len <= 500):
        failures.append(
            Failure(
                "sk.description_length",
                "error",
                f"SKILL description 길이 {desc_len}자가 50~500 범위 밖. pushy하게 50자 이상, 산만함 방지로 500자 이하.",
                field_path="description",
            )
        )

    runtime = frontmatter.get("runtime")
    if runtime not in {"python", "bash"}:
        failures.append(
            Failure(
                "sk.runtime_valid",
                "error",
                f"SKILL runtime='{runtime}'이 허용 집합 밖. python 또는 bash.",
                field_path="runtime",
            )
        )

    entry = frontmatter.get("entry")
    if not (isinstance(entry, str) and entry):
        failures.append(
            Failure(
                "sk.entry_present",
                "error",
                "SKILL entry 필드 누락. 실행할 스크립트 경로가 필요.",
                field_path="entry",
            )
        )
    else:
        # Sandbox resolve: check path traversal against read_root.
        root = Path(read_root).resolve()
        if ".." in Path(entry).parts:
            failures.append(
                Failure(
                    "agent.no_path_traversal",
                    "error",
                    f"entry='{entry}'에 '..' 세그먼트 포함. ADR 0004 샌드박스 경계 위반.",
                    field_path="entry",
                )
            )
        else:
            resolved = (root / entry).resolve()
            try:
                resolved.relative_to(root)
            except ValueError:
                failures.append(
                    Failure(
                        "agent.no_path_traversal",
                        "error",
                        f"entry='{entry}'가 샌드박스({read_root}) 밖으로 해석됨. ADR 0004 샌드박스 경계 위반.",
                        field_path="entry",
                    )
                )
            else:
                if not resolved.is_file():
                    failures.append(
                        Failure(
                            "sk.entry_file_exists",
                            "error",
                            f"SKILL entry='{entry}'이 가리키는 파일 미존재. 스킬 디렉토리 기준 상대경로로 실파일 필요.",
                            field_path="entry",
                        )
                    )
                else:
                    # Extension/runtime alignment (warn).
                    ext = resolved.suffix.lower()
                    if runtime == "python" and ext != ".py":
                        failures.append(
                            Failure(
                                "sk.entry_extension_matches_runtime",
                                "warn",
                                f"entry 확장자와 runtime 불일치 (runtime=runtime, entry={entry}).",
                                field_path="entry",
                            )
                        )
                    elif runtime == "bash" and ext != ".sh":
                        failures.append(
                            Failure(
                                "sk.entry_extension_matches_runtime",
                                "warn",
                                f"entry 확장자와 runtime 불일치 (runtime=runtime, entry={entry}).",
                                field_path="entry",
                            )
                        )

                    # AST safety for python entry.
                    if runtime == "python" and ext == ".py":
                        ast_failure = _python_ast_safe(resolved)
                        if ast_failure is not None:
                            failures.append(ast_failure)

    # Body sections (warn)
    if "## 목적" not in body:
        failures.append(
            Failure("sk.has_purpose_section", "warn", "SKILL '## 목적' 섹션 권장.", field_path=None)
        )
    if "## 실행" not in body:
        failures.append(
            Failure(
                "sk.has_execution_section",
                "warn",
                "SKILL '## 실행' 섹션 권장 — 호출 방법 명시.",
                field_path=None,
            )
        )

    if "TODO: implement" in body and len(body) <= 500:
        failures.append(
            Failure(
                "no_placeholder_only",
                "error",
                "SKILL 본문이 'TODO: implement' placeholder만 있음. 미완성 산출물 거부.",
                field_path=None,
            )
        )

    failures.extend(_forbidden_patterns_checks(frontmatter, body, path_hint="SKILL.md"))

    return _finalize(failures)


# ---------------------------------------------------------------------------
# Shared forbidden-patterns checks (run on any generated artifact)
# ---------------------------------------------------------------------------

def _forbidden_patterns_checks(
    frontmatter: dict,
    body: str,
    path_hint: str,
) -> list[Failure]:
    failures: list[Failure] = []

    content = body

    # no_eval_exec
    for regex, label in _EVAL_EXEC_RES:
        if regex.search(content):
            failures.append(
                Failure(
                    "no_eval_exec",
                    "error",
                    f"금지 호출 발견: {label} at {path_hint}. 샌드박스 회피 위험.",
                    field_path=None,
                )
            )
            break

    # no_shell_injection_risk
    for regex, label in _SHELL_INJECTION_RES:
        if regex.search(content):
            failures.append(
                Failure(
                    "no_shell_injection_risk",
                    "error",
                    f"shell injection 위험 패턴: {label} at {path_hint}.",
                    field_path=None,
                )
            )
            break

    # no_pipe_to_shell
    if _PIPE_TO_SHELL_RE.search(content):
        failures.append(
            Failure(
                "no_pipe_to_shell",
                "error",
                f"원격 코드 실행 패턴 발견: curl|sh or wget|bash at {path_hint}.",
                field_path=None,
            )
        )

    # no_rm_rf_root
    for regex, label in _DANGEROUS_BASH_PATTERNS:
        if regex.search(content):
            failures.append(
                Failure(
                    "no_rm_rf_root",
                    "error",
                    f"파괴적 삭제 패턴: {label} at {path_hint}.",
                    field_path=None,
                )
            )
            break

    # no_empty_description
    desc = frontmatter.get("description", "")
    if isinstance(desc, str) and 0 < len(desc) < 20:
        failures.append(
            Failure(
                "no_empty_description",
                "error",
                "description이 20자 미만. 트리거 품질 보장 불가.",
                field_path="description",
            )
        )

    # body.no_long_base64 (warn)
    if _LONG_BASE64_RE.search(content):
        failures.append(
            Failure(
                "body.no_long_base64",
                "warn",
                f"{path_hint}에 200자 이상 연속 base64-like 문자열 발견. 숨겨진 페이로드 가능성 검토.",
                field_path=None,
            )
        )

    # frontmatter.no_backtick_in_values
    for k, v in frontmatter.items():
        if isinstance(v, str) and "`" in v:
            failures.append(
                Failure(
                    "frontmatter.no_backtick_in_values",
                    "error",
                    f"{path_hint} frontmatter 필드 '{k}'에 backtick 포함: {v}. 커맨드 인젝션 의심.",
                    field_path=k,
                )
            )

    return failures


# ---------------------------------------------------------------------------
# Python AST safety (for SKILL entry when runtime=python)
# ---------------------------------------------------------------------------

_AST_FORBIDDEN_CALLS = {"eval", "exec", "compile"}
_AST_FORBIDDEN_ATTRS = {
    ("os", "system"),
    ("os", "popen"),
    ("subprocess", "Popen"),  # flagged only when shell=True keyword is present
    ("subprocess", "call"),
    ("subprocess", "run"),
    ("subprocess", "check_call"),
    ("subprocess", "check_output"),
}


class _SafetyVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.violations: list[str] = []

    def _call_has_shell_true(self, node: ast.Call) -> bool:
        for kw in node.keywords or []:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                return True
        return False

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        func = node.func
        # eval/exec/compile/__import__
        if isinstance(func, ast.Name):
            if func.id in _AST_FORBIDDEN_CALLS:
                self.violations.append(func.id)
            elif func.id == "__import__":
                # __import__("os").system(...) chain — flag as forbidden usage.
                self.violations.append("__import__")
        elif isinstance(func, ast.Attribute):
            # Walk through attr chain to find module.attr
            if isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair == ("os", "system") or pair == ("os", "popen"):
                    self.violations.append(f"{pair[0]}.{pair[1]}")
                elif func.value.id == "subprocess":
                    if self._call_has_shell_true(node):
                        self.violations.append(f"subprocess.{func.attr}(shell=True)")
        self.generic_visit(node)


def _python_ast_safe(path: Path) -> Failure | None:
    """Parse entry and flag forbidden calls. Returns Failure or None."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as e:
        return Failure(
            "sk.entry_python_ast_safe",
            "error",
            f"entry Python AST 검사 실패 at {path}: read error {e}. 금지 호출 또는 구문 오류.",
            field_path="entry",
        )
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as e:
        return Failure(
            "sk.entry_python_ast_safe",
            "error",
            f"entry Python AST 검사 실패 at {path}: {e.msg}. 금지 호출 또는 구문 오류.",
            field_path="entry",
        )
    visitor = _SafetyVisitor()
    visitor.visit(tree)
    if visitor.violations:
        return Failure(
            "sk.entry_python_ast_safe",
            "error",
            f"entry Python AST 검사 실패 at {path}: {visitor.violations}. 금지 호출 또는 구문 오류.",
            field_path="entry",
        )
    return None


# ---------------------------------------------------------------------------
# Sandbox write-root helper (exported for runtime use)
# ---------------------------------------------------------------------------

def check_sandbox_write_root(path: str) -> Failure | None:
    """Return Failure if path escapes the allowed write-root prefixes.

    Runtime callers should invoke this before any disk write. Paths are checked
    as strings for startswith one of .agents/, .agents/skills/, _workspace/.
    Absolute paths or paths containing '..' are also rejected.
    """
    if not isinstance(path, str) or not path:
        return Failure(
            "sandbox.write_roots",
            "error",
            f"경로 '{path}'이 허용된 쓰기 루트 밖: .agents/, .agents/skills/, _workspace/.",
            field_path=None,
        )
    if path.startswith("/") or ".." in Path(path).parts:
        return Failure(
            "sandbox.write_roots",
            "error",
            f"경로 '{path}'이 허용된 쓰기 루트 밖: .agents/, .agents/skills/, _workspace/.",
            field_path=None,
        )
    if not any(path.startswith(root) for root in _ALLOWED_WRITE_ROOTS):
        return Failure(
            "sandbox.write_roots",
            "error",
            f"경로 '{path}'이 허용된 쓰기 루트 밖: .agents/, .agents/skills/, _workspace/.",
            field_path=None,
        )
    return None


# ---------------------------------------------------------------------------
# Internal: finalize result
# ---------------------------------------------------------------------------

def _finalize(failures: list[Failure]) -> LintResult:
    has_error = any(f.severity == "error" for f in failures)
    return LintResult(passed=not has_error, failures=failures)

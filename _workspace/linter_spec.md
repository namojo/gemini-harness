# meta_linter.py — CHECKS 명세 (Phase 1, v1)

이 문서는 **구현이 아닌 명세**다. `meta_linter.py`의 `CHECKS` 리스트가 만족해야 할 계약을 기술한다. 각 항목은 `(name, predicate, failure_message_template, severity)` 4-튜플 형태로 구현되어야 한다.

## 스코프

검증 대상은 런타임이 생성하는 3종 산출물:

1. `workflow.json` (초기 registry 스냅샷)
2. `.agents/{name}/SYSTEM_PROMPT.md` (frontmatter + body)
3. `.agents/skills/{name}/SKILL.md` (frontmatter + entry file)

추가로, Worker 응답의 `create_agents` / `create_skills` 필드는 디스크 쓰기 **이전**에 동일한 AgentMetadata/SkillMetadata 체크를 통과해야 한다.

## 공통 규약

- **severity**: `error` | `warn`. `error` 1건이라도 있으면 산출물 거부. `warn`은 로깅만.
- **failure_message_template**: Python f-string 포맷. `{path}`, `{field}`, `{value}`, `{expected}` 플레이스홀더 사용.
- **재생성 피드백**: 실패 시 런타임은 메시지 템플릿을 포맷하여 생성 에이전트의 inbox에 Message로 전달.

## CHECKS — workflow.json

| name | predicate | severity | failure_message_template |
|---|---|---|---|
| `workflow.version_present` | `w.get("version") == "1.0"` | error | `workflow.json version 필드가 누락 또는 '1.0'이 아님 (현재: {value}). v1 스키마 사용 시 반드시 '1.0'.` |
| `workflow.pattern_valid` | `w["pattern"] in VALID_PATTERNS or _is_composite(w["pattern"])` | error | `workflow.pattern='{value}'이 6 기본 패턴 또는 복합 패턴 문자열이 아님. 허용: pipeline, fan_out_fan_in, expert_pool, producer_reviewer, supervisor, hierarchical, 또는 '+'로 연결된 조합.` |
| `workflow.initial_registry_nonempty` | `len(w["initial_registry"]) >= 1` | error | `initial_registry가 비어 있음. 최소 1명 이상의 에이전트가 필요.` |
| `workflow.unique_ids` | `len({a["id"] for a in w["initial_registry"]}) == len(w["initial_registry"])` | error | `initial_registry 내 중복 id 발견: {value}. 모든 id는 유일해야 함.` |
| `workflow.system_prompts_exist` | `all(Path(a["system_prompt_path"]).is_file() for a in w["initial_registry"])` | error | `system_prompt_path가 가리키는 파일 미존재: {value}. .agents/{{name}}/SYSTEM_PROMPT.md 먼저 생성.` |
| `workflow.routing_config_complete` | 패턴별 필수 키 존재 (ADR 0003 계약). `fan_out_fan_in` → `integrator_id`. `expert_pool` → `classifier`. `producer_reviewer` → `producer_id`,`reviewer_id`. `supervisor` → `supervisor_id`. `hierarchical` → `root_id`. 복합 패턴 → `phase_map`. `pipeline`은 routing_config 없어도 OK. | error | `pattern='{value}' 인데 routing_config에 필수 키({expected}) 누락. 린터가 ADR 0003 패턴 contract를 강제.` |
| `workflow.phase_map_valid` | 복합 패턴(`+` 포함)이면 phase_map의 모든 값이 6 기본 패턴 중 하나 | error | `phase_map의 값 '{value}'이 6 기본 패턴 밖. 복합 패턴 부속은 기본 패턴만 허용.` |
| `workflow.routing_ids_exist_in_registry` | routing_config의 모든 `*_id` 값이 initial_registry의 id 집합에 포함. **classifier가 맵이면 맵의 모든 값(expert id들)도 포함 검사** — expert_pool에서 dangling expert 참조 방지. | error | `routing_config.{field}='{value}'가 initial_registry에 존재하지 않음. registry와 routing_config이 disconnected.` |
| `workflow.retry_limit_range` | `0 <= w.get("retry_limit", 3) <= 10` | warn | `retry_limit={value}이 0~10 범위 밖. 권장 범위: 1~5.` |
| `workflow.routing_retry_limit_range` | `routing_config.retry_limit`이 있으면 `0 <= v <= 10`. override 규칙: 최상위 retry_limit이 전역 기본, routing_config.retry_limit이 있으면 현재 패턴에서 override. 복합 패턴의 phase별 세분화는 v1 미지원. | warn | `routing_config.retry_limit={value}이 범위 밖. 0~10만 허용.` |
| `workflow.tool_executor_iterations_range` | `routing_config.tool_executor.max_tool_iterations`이 있으면 `1 <= v <= 20`. tool_executor 블록 자체는 optional(없으면 tool-calling 비활성). | warn | `tool_executor.max_tool_iterations={value}이 1~20 범위 밖. 무한 루프 방지를 위해 상한 권장.` |
| `workflow.tool_executor_allowed_tools_known` | `routing_config.tool_executor.allowed_tools`의 각 항목이 initial_registry의 어떤 agent의 `tools` 필드에라도 등장. 어디에도 없으면 dead-config. | warn | `tool_executor.allowed_tools 항목 '{value}'이 어떤 AgentMetadata.tools에도 등장하지 않음. dead-config 의심.` |

## CHECKS — AgentMetadata (registry 항목 + Worker 응답의 create_agents[])

| name | predicate | severity | failure_message_template |
|---|---|---|---|
| `agent.has_required_fields` | `all(k in a for k in ["id","name","role","system_prompt_path"])` | error | `AgentMetadata 필수 필드 누락: {expected}. 현재 제공된 키: {value}.` |
| `agent.id_pattern` | `re.fullmatch(r"^[a-z][a-z0-9_-]*$", a["id"])` | error | `agent.id='{value}'가 slug 패턴 위반. 소문자로 시작, [a-z0-9_-]만 허용.` |
| `agent.role_not_empty` | `len(a.get("role","").strip()) >= 10` | error | `agent.role이 10자 미만({value}자). 역할을 1~3문장으로 구체적으로 기술.` |
| `agent.system_prompt_path_pattern` | `re.fullmatch(r"^\\.agents/[a-z0-9_-]+/SYSTEM_PROMPT\\.md$", a["system_prompt_path"])` | error | `system_prompt_path='{value}'가 규약 위반. 형식: .agents/{{name}}/SYSTEM_PROMPT.md.` |
| `agent.no_path_traversal` | `".." not in a["system_prompt_path"] and not a["system_prompt_path"].startswith("/")` | error | `system_prompt_path='{value}'에 경로 탈출(../ 또는 절대경로) 포함. ADR 0004 샌드박스 경계 위반.` |
| `agent.status_valid` | `a.get("status","idle") in {"idle","working","completed","failed"}` | warn | `agent.status='{value}'가 허용 집합 밖. idle로 정규화하여 채택.` |

## CHECKS — SYSTEM_PROMPT.md frontmatter

| name | predicate | severity | failure_message_template |
|---|---|---|---|
| `sp.has_name` | `"name" in fm` | error | `SYSTEM_PROMPT frontmatter에 name 누락.` |
| `sp.has_version` | `"version" in fm and re.fullmatch(r"^[0-9]+\\.[0-9]+(\\.[0-9]+)?$", str(fm["version"]))` | error | `SYSTEM_PROMPT frontmatter.version 누락 또는 semver 위반 (현재: {value}).` |
| `sp.has_model` | `fm.get("model","").startswith("gemini-")` | error | `SYSTEM_PROMPT frontmatter.model='{value}'가 'gemini-'로 시작하지 않음. 포트는 Gemini 모델만 허용.` |
| `sp.tools_is_list` | `isinstance(fm.get("tools",[]), list)` | error | `SYSTEM_PROMPT frontmatter.tools가 리스트가 아님 (현재 타입: {value}).` |
| `sp.name_matches_id` | `fm["name"] == agent_id_from_path` | warn | `frontmatter.name='{value}'이 registry id와 불일치. 동기화 권장.` |

## CHECKS — SYSTEM_PROMPT.md body (구조 검사)

| name | predicate | severity | failure_message_template |
|---|---|---|---|
| `sp.has_core_role_section` | `"## 핵심 역할" in body` | error | `SYSTEM_PROMPT.md 본문에 '## 핵심 역할' 섹션 누락. 역할 정의는 필수.` |
| `sp.has_self_critique_section` | `"## 자가 검증" in body` | error | `SYSTEM_PROMPT.md 본문에 '## 자가 검증' 섹션 누락. self-critique 루프 없이는 품질 하한 보장 불가.` |
| `sp.has_io_protocol_section` | `"## 입력/출력 프로토콜" in body` | warn | `'## 입력/출력 프로토콜' 섹션 권장. 팀 통신 명확성 향상.` |
| `sp.body_min_length` | `len(body.strip()) >= 300` | warn | `본문이 {value}자로 짧음. 300자 미만은 placeholder 가능성.` |

## CHECKS — SKILL.md frontmatter

| name | predicate | severity | failure_message_template |
|---|---|---|---|
| `sk.has_name` | `"name" in fm` | error | `SKILL frontmatter.name 누락.` |
| `sk.has_version` | `"version" in fm` | error | `SKILL frontmatter.version 누락.` |
| `sk.description_length` | `50 <= len(fm.get("description","")) <= 500` | error | `SKILL description 길이 {value}자가 50~500 범위 밖. pushy하게 50자 이상, 산만함 방지로 500자 이하.` |
| `sk.runtime_valid` | `fm.get("runtime") in {"python","bash"}` | error | `SKILL runtime='{value}'이 허용 집합 밖. python 또는 bash.` |
| `sk.entry_present` | `"entry" in fm and isinstance(fm["entry"], str)` | error | `SKILL entry 필드 누락. 실행할 스크립트 경로가 필요.` |
| `sk.entry_file_exists` | `(skill_dir / fm["entry"]).is_file()` | error | `SKILL entry='{value}'이 가리키는 파일 미존재. 스킬 디렉토리 기준 상대경로로 실파일 필요.` |
| `sk.entry_extension_matches_runtime` | `runtime=='python' → .py / runtime=='bash' → .sh` | warn | `entry 확장자와 runtime 불일치 (runtime={field}, entry={value}).` |
| `sk.entry_python_ast_safe` | runtime=python이면 `ast.parse(entry)` 성공 + 호출 노드가 화이트리스트 밖이 아님(eval/exec/compile/os.system/subprocess.Popen with shell=True 탐지). ADR 0004 AST 기반 검사. | error | `entry Python AST 검사 실패 at {path}: {value}. 금지 호출 또는 구문 오류.` |

## CHECKS — SKILL.md body

| name | predicate | severity | failure_message_template |
|---|---|---|---|
| `sk.has_purpose_section` | `"## 목적" in body` | warn | `SKILL '## 목적' 섹션 권장.` |
| `sk.has_execution_section` | `"## 실행" in body` | warn | `SKILL '## 실행' 섹션 권장 — 호출 방법 명시.` |
| `sk.body_not_placeholder_only` | `"TODO: implement" not in body or len(body) > 500` | error | `SKILL 본문이 'TODO: implement' placeholder만 있음. 미완성 산출물 거부.` |

## CHECKS — 금지 패턴 (모든 파일)

| name | predicate | severity | failure_message_template |
|---|---|---|---|
| `no_eval_exec` | 파일 내용에 `eval(`, `exec(`, `compile(` 중 어느 것도 등장하지 않음 | error | `금지 호출 발견: {value} at {path}. 샌드박스 회피 위험.` |
| `no_shell_injection_risk` | `subprocess.*shell=True` 패턴 없음, `os.system`, `os.popen` 없음 | error | `shell injection 위험 패턴: {value} at {path}.` |
| `no_pipe_to_shell` | `curl ... \| sh` 또는 `wget ... \| bash` 패턴 없음 | error | `원격 코드 실행 패턴 발견: {value} at {path}.` |
| `no_rm_rf_root` | `rm -rf /`, `rm -rf ~`, `rm -rf $HOME` 패턴 없음 | error | `파괴적 삭제 패턴: {value} at {path}.` |
| `no_placeholder_only` | 본문 500자 이상이거나 'TODO: implement'만 있지 않음 | error | `placeholder-only 본문. 최소 내용 필요.` |
| `no_empty_description` | frontmatter.description 길이 >= 20 (SKILL의 50 제한보다 완화된 공통 하한) | error | `description이 20자 미만. 트리거 품질 보장 불가.` |
| `body.no_long_base64` | 파일 본문에 연속 base64-like 문자열(`[A-Za-z0-9+/=]`) 200자 이상이 등장하지 않음. ADR 0004 페이로드 은닉 방지. false positive 가능성 있어 warn. | warn | `{path}에 200자 이상 연속 base64-like 문자열 발견. 숨겨진 페이로드 가능성 검토.` |
| `frontmatter.no_backtick_in_values` | frontmatter의 어떤 값 문자열에도 `` ` ``(backtick) 문자가 없음. ADR 0004: backtick injection은 YAML 파싱 후 커맨드 치환 시도의 흔한 시그널. | error | `{path} frontmatter 필드 '{field}'에 backtick 포함: {value}. 커맨드 인젝션 의심.` |
| `sandbox.write_roots` | 파일 경로가 `.agents/`, `.agents/skills/`, `_workspace/` 3개 루트 중 하나의 하위. ADR 0004 샌드박스 경계. | error | `경로 '{value}'이 허용된 쓰기 루트 밖: .agents/, .agents/skills/, _workspace/.` |

## 실행 흐름 (참고, 구현 아님)

```
1. 런타임이 파일 생성 직후 meta_linter.run(path) 호출
2. 런타임이 파일 타입(workflow/system_prompt/skill) 판별 후 해당 CHECKS 서브셋 적용
3. error severity 1건이라도 발생 → 산출물 reject + 생성 에이전트 inbox에 실패 Message 전달
4. 생성 에이전트는 메시지를 읽고 재생성 → 3회 실패 시 Manager가 escalate
5. warn만 있으면 산출물 채택, 로그에 경고 기록
```

## 재생성 피드백 메시지 템플릿 (런타임 사용)

```
LINT_FAIL: {path}
Failed checks ({error_count} errors, {warn_count} warnings):
- [{severity}] {name}: {formatted_message}
- ...

재생성 시 반드시 위 에러를 모두 해결. 스키마 참조: _workspace/schema/{workflow.v1,system_prompt,skill}.json
```

## 버전 전략

- v1 범위: 위 CHECKS 고정. 필드 **추가**만 허용(하위 호환).
- v2 bump 조건: 필드 삭제·의미 변경·runtime enum 축소/확장 중 하나라도 해당.
- v2 진행 시 `migrations/v1_to_v2.py` 필수 제공.

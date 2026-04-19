---
name: meta-agent-templates
description: Gemini-Harness 런타임이 동적 생성하는 SYSTEM_PROMPT.md와 SKILL.md의 템플릿·JSON Schema·linter 규칙. workflow.json 스키마 v1 정의, 필수/선택 필드, 금지 패턴, 버전 마이그레이션 전략. 메타 레벨 생성물 형식·검증 작업 시 반드시 참조.
---

# Meta-Agent Templates (Runtime-Generated Artifacts)

Gemini-Harness 런타임이 만들어내는 산출물의 **형식을 규정**한다. 이 스킬은 메타 레벨(런타임의 생성물)을 다룬다 — 빌드 팀 `.claude/*`와 구분.

## 레이어 구분 (중요)

| 레이어 | 경로 | 누가 작성 | 형식 기준 |
|------|------|---------|---------|
| 빌드 팀 | `.claude/agents/`, `.claude/skills/` | Claude Code + 본 하네스 | Claude Code 스킬 규약 |
| 런타임 생성물 | `.agents/{name}/SYSTEM_PROMPT.md`, `.agents/skills/{name}/SKILL.md` | Gemini-Harness 런타임 | **본 스킬의 템플릿** |

## workflow.json 스키마 (v1) — Registry Snapshot

**중요:** workflow.json은 "그래프 정의"가 **아니다**. Manager+Worker+Registry 아키텍처에서 그래프는 고정 3노드이므로, workflow.json은 **초기 agent_registry 스냅샷 + 패턴 메타**만 담는다. 런타임에 추가되는 에이전트는 registry 이벤트로 기록되고 `.gemini/context.md`에 append된다.

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "type": "object",
  "required": ["version", "pattern", "initial_registry"],
  "properties": {
    "version": {"const": "1.0"},
    "pattern": {
      "enum": ["pipeline", "fan_out_fan_in", "expert_pool",
               "producer_reviewer", "supervisor", "hierarchical"],
      "description": "Manager 라우팅 로직 선택자. 복합 패턴은 문자열 조합 허용 (예: fan_out_fan_in+producer_reviewer)"
    },
    "retry_limit": {"type": "integer", "default": 3, "minimum": 0, "maximum": 10},
    "initial_registry": {
      "type": "array",
      "minItems": 1,
      "items": {"$ref": "#/$defs/AgentMetadata"}
    },
    "routing_config": {
      "type": "object",
      "description": "패턴별 추가 설정 (선택). 예: producer_reviewer의 producer/reviewer id 지정",
      "additionalProperties": true
    }
  },
  "$defs": {
    "AgentMetadata": {
      "type": "object",
      "required": ["id", "name", "role", "system_prompt_path"],
      "properties": {
        "id": {"type": "string", "pattern": "^[a-z][a-z0-9_-]*$"},
        "name": {"type": "string"},
        "role": {"type": "string", "minLength": 10},
        "system_prompt_path": {
          "type": "string",
          "description": ".agents/{name}/SYSTEM_PROMPT.md 경로"
        },
        "skills": {"type": "array", "items": {"type": "string"}},
        "tools": {"type": "array", "items": {"type": "string"},
                  "description": "file-manager, google-search, mcp:{name} 등"},
        "group": {"type": "string", "description": "팀/그룹 태그 (선택)"},
        "status": {"enum": ["idle", "working", "completed", "failed"], "default": "idle"},
        "created_at": {"type": "string", "format": "date-time"},
        "created_by": {"type": "string", "description": "\"user\" 또는 메타 에이전트 id"}
      }
    }
  }
}
```

**검증 포인트:**
- `initial_registry[].id`가 모두 유일한지
- `system_prompt_path`가 실존 파일인지
- `pattern`이 6 패턴 또는 복합 중 하나인지
- `producer_reviewer` 패턴이면 `routing_config.producer_id`, `routing_config.reviewer_id` 필수
- `supervisor` 패턴이면 `routing_config.supervisor_id` 필수

**런타임 확장 (registry append 이벤트):** 메타 에이전트가 새 에이전트를 만들면 디스크 workflow.json은 **자동 갱신하지 않는다**(초기 스냅샷 유지). 대신 `.gemini/context.md`에 이벤트 로그로 기록하고, 실행 종료 시 `_workspace/final_registry.json`에 최종 registry를 저장한다. 사용자가 "이 구성을 초기 스냅샷으로 고정하고 싶다"고 하면 그때 workflow.json을 덮어쓴다.

## SYSTEM_PROMPT.md 템플릿 (런타임이 생성)

```markdown
---
name: {agent_name}
version: 1.0
model: gemini-3.1-pro-preview
tools: [file-manager, google-search]
---

# {Role Title}

## 핵심 역할
{1~3문장. Why 중심.}

## 작업 원칙
1. {원칙과 이유}
2. ...

## 입력/출력 프로토콜
**입력:** ...
**출력:** ...

## 에러 핸들링
...

## 자가 검증 (self-critique)
작업 완료 후 반드시:
1. {체크 항목 1}
2. {체크 항목 2}

실패 시 fix 모드로 재시작.
```

**필수 필드 (린터가 검증):**
- frontmatter: `name`, `version`, `model`
- 본문 섹션: `## 핵심 역할`, `## 자가 검증`

## SKILL.md 템플릿 (런타임이 생성하는 동적 스킬)

```markdown
---
name: {skill_name}
version: 1.0
description: {구체적·pushy한 트리거 설명 — 50~500자}
runtime: python | bash
entry: scripts/main.py
---

# {Skill Title}

## 목적
Why

## 사용
언제 호출되는가, 입력/출력 형식

## 실행
{runtime에 따라 호출 방식 기술}

## 검증
출력 assertion 방법 (가능하면 스크립트 제공)
```

**필수:** `entry` 필드가 가리키는 파일이 실제 존재해야 한다. 린터가 확인.

## meta_linter.py 검증 규칙

런타임이 파일 생성 직후 즉시 실행, 통과해야만 채택. **메타 에이전트 응답의 `create_agents`/`create_skills` 필드도 같은 린터를 통과해야 registry append 허용**.

```python
from dataclasses import dataclass

@dataclass
class LintResult:
    passed: bool
    failures: list[str]  # 구체적 실패 사유

CHECKS = [
    # --- workflow.json (초기 registry 스냅샷) ---
    ("workflow.version_present",
     lambda w: w.get("version") == "1.0"),
    ("workflow.pattern_valid",
     lambda w: w["pattern"] in VALID_PATTERNS or _is_composite(w["pattern"])),
    ("workflow.unique_ids",
     lambda w: len({a["id"] for a in w["initial_registry"]}) == len(w["initial_registry"])),
    ("workflow.system_prompts_exist",
     check_all_system_prompts_exist),
    ("workflow.routing_config_complete",
     check_routing_config_for_pattern),

    # --- AgentMetadata (registry 항목 + 런타임 생성) ---
    ("agent.has_required_fields",
     lambda a: all(k in a for k in ["id", "name", "role", "system_prompt_path"])),
    ("agent.role_not_empty",
     lambda a: len(a.get("role", "")) >= 10),
    ("agent.id_pattern",
     lambda a: re.match(r"^[a-z][a-z0-9_-]*$", a["id"])),

    # --- SYSTEM_PROMPT.md frontmatter ---
    ("sp.has_name", lambda fm: "name" in fm),
    ("sp.has_version", lambda fm: "version" in fm),
    ("sp.has_model", lambda fm: fm.get("model", "").startswith("gemini-")),

    # --- SYSTEM_PROMPT.md body ---
    ("sp.has_core_role_section",
     lambda body: "## 핵심 역할" in body),
    ("sp.has_self_critique_section",
     lambda body: "## 자가 검증" in body),

    # --- SKILL.md ---
    ("sk.description_length",
     lambda fm: 50 <= len(fm.get("description", "")) <= 500),
    ("sk.entry_file_exists", check_entry_file),
    ("sk.runtime_valid",
     lambda fm: fm.get("runtime") in {"python", "bash"}),

    # --- 금지 패턴 (all files) ---
    ("no_eval_exec", check_no_dangerous_calls),
    ("no_shell_injection_risk", check_no_shell_true),
    ("no_placeholder_only",
     lambda body: "TODO: implement" not in body or len(body) > 500),
]
```

린터 실패 시 런타임은 해당 산출물을 **거부**하고 생성 에이전트의 inbox에 실패 필드·사유를 담은 Message를 넣어 재생성 요청. retry_count 증가. 3회 실패 시 Manager가 escalate 처리.

## 금지 패턴 (생성 산출물)

| 패턴 | 이유 |
|------|------|
| `eval`, `exec`, `compile` | 런타임 샌드박스 회피 |
| `subprocess(..., shell=True)` | command injection |
| `os.system`, `popen` | 위와 동일 |
| `rm -rf /`, `rm -rf ~` | 명백한 위험 |
| `curl ... \| sh`, `wget ... \| bash` | 원격 코드 실행 |
| 빈 description (<20자) | 트리거 품질 저하 |
| "TODO: implement"만 있는 본문 | 미완성 산출물 |

## 예시 (good/bad)

`examples/` 디렉토리에 올바른 예시와 잘못된 예시 쌍을 배치:
- `examples/good_system_prompt.md` / `examples/bad_system_prompt_missing_critique.md`
- `examples/good_workflow.json` / `examples/bad_workflow_disconnected.json`

잘못된 예시마다 **어떤 린터 규칙에 걸리는가**를 파일 맨 위 주석으로 명시.

## 버전 마이그레이션

v1 → v2 진행 시:
1. `migrations/v1_to_v2.py` 작성 — workflow.json 변환기
2. deprecation period 명시 (예: v1 2개월 유지)
3. 런타임 시작 시 `version` 필드 읽고 자동 마이그레이션 시도 → 실패 시 사용자 승인 요청
4. SYSTEM_PROMPT.md / SKILL.md도 각각 마이그레이터 제공

**원칙:** 필드 **추가**만이면 v1 유지(하위 호환). 필드 **삭제·의미 변경**이면 v2로 bump.

## 왜 이렇게 빡빡한가

런타임은 Gemini가 즉흥적으로 작성한 산출물을 채택한다. 가드레일 없이 채택하면:
- 필수 정보 누락 → 런타임이 None 참조로 크래시
- 형식 불일치 → 파서가 깨져 에이전트 로딩 실패
- 보안 취약 코드 생성 → 런타임 실행 중 호스트 오염

린터는 Gemini 품질의 **하한선**이다. Self-critique 루프와 함께 "형식 드리프트"를 차단한다.

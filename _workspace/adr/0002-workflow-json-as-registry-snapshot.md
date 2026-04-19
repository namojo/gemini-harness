---
id: 0002
title: workflow.json v1은 그래프 정의가 아닌 초기 registry 스냅샷
status: accepted
date: 2026-04-19
sources:
  - ~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/skills/harness/SKILL.md
  - .claude/skills/meta-agent-templates/SKILL.md
  - _workspace/adr/0001-architecture-manager-worker-registry.md
---

## Context

ADR 0001에서 그래프 토폴로지를 3노드로 고정했으므로 전통적인 "워크플로우 정의 파일"(노드·엣지 DSL)은 필요하지 않다. 그러나 원본 하네스의 Phase 5는 "오케스트레이터 스킬 + 초기 팀 구성"을 생성하며, 포트에서도 **초기 에이전트 구성을 디스크에 고정**하여 재현성·감사 추적을 제공해야 한다. 동시에 런타임에 추가되는 에이전트(메타 생성물, ADR 0004)를 이 파일에 매번 덮어쓰면 초기 구성이 오염되어 "처음부터 다시 실행" 시나리오가 깨진다.

## Decision

**`workflow.json`은 v1부터 `{version, pattern, retry_limit, initial_registry, routing_config}` 구조의 초기 registry 스냅샷으로 정의한다.**

- `pattern`: 6개 값 + 복합 문자열(`"fan_out_fan_in+producer_reviewer"`). Manager `_route` 분기 선택자 (ADR 0003).
- `initial_registry`: 사용자(또는 Phase 2 설계)가 확정한 초기 에이전트 메타데이터 배열. Worker가 실행 시점에 `system_prompt_path`를 읽는다.
- `routing_config`: 패턴별 필수 파라미터(`producer_id`, `reviewer_id`, `supervisor_id`, `integrator_id` 등).
- **런타임 증가분은 디스크에 반영하지 않는다**. 메타 생성 이벤트는 `.gemini/context.md`에 로그로 append되고, 실행 종료 시 `_workspace/final_registry.json`에 최종 상태가 기록된다. 사용자가 "이 구성을 초기값으로 고정"을 명시적으로 요청할 때만 `workflow.json`을 덮어쓴다.
- 스키마는 `$schema: draft-07`을 선언하고 `meta_linter`가 로드 시점에 validation한다(Task #2).

## Consequences

### Positive

- **재현성**: 같은 `workflow.json`으로 반복 실행 시 초기 상태가 동일. A/B 측정(`self-critique-verification` 스킬)의 전제.
- **감사 추적**: 초기/최종을 분리 저장하므로 "어떤 에이전트가 언제 메타 생성됐는가"를 diff로 볼 수 있다.
- **스키마 린터로 품질 상한선 확보**: `initial_registry[].system_prompt_path`가 실존 파일인지 자동 확인. 원본에서는 누락 검증이 없어 드리프트 원인이었다.
- **마이그레이션 전략 단순**: 그래프 DSL이 아니므로 `version` 필드만 보고 필드 매퍼(v1→v2)를 작성하면 충분.

### Negative / Risks

- **"런타임 추가 에이전트 재사용"의 수동성**: 사용자가 좋은 메타 생성물을 고정하려면 `_workspace/final_registry.json`에서 엔트리를 복사해 `workflow.json`에 붙여넣어야 한다. 자동 병합 CLI는 v2 스코프로 유보.
- **패턴 복합 문자열 파싱 비용**: 복합 패턴 확장 시 문자열 파서 유지 필요. JSON 객체 형태(`{primary, secondary}`)로 바꾸려면 v2 bump.
- **오케스트레이터 MCP와의 중복 우려**: MCP `harness.run`의 입력도 결국 이 파일이라 "어디가 single source of truth인가" 혼동 가능. → MCP는 파일을 **읽기만** 하고 갱신은 하네스 런타임만 한다는 규칙으로 해소.

## Alternatives Considered

| 대안 | 기각 사유 |
|------|---------|
| **그래프 DSL(노드·엣지 명시)** | ADR 0001의 정적 3노드 전제와 충돌. 사용자에게 토폴로지 이해 부담 전가 |
| **YAML 포맷** | JSON Schema 생태계(검증기·IDE 지원)가 더 성숙. 주석 불가 이슈는 설명 필드(`description`)로 보완 |
| **런타임이 매번 workflow.json 덮어쓰기** | 초기 구성 오염 → "처음부터 다시" 시나리오 파괴. 감사 추적도 소실 |
| **SQLite만 사용 (파일 없음)** | Git diff·코드 리뷰 불가. 원본의 `.claude/agents/*.md` 파일 기반 UX와도 이탈 |

## Upstream Alignment

- 원본 Phase 3은 `.claude/agents/{name}.md`를 **파일로 직접 생성**한다. 포트도 `.agents/{name}/SYSTEM_PROMPT.md` 파일을 유지하고, `workflow.json.initial_registry[].system_prompt_path`는 그 파일을 가리키는 포인터. 형식 보존.
- 원본 Phase 2에서 결정되는 "팀 아키텍처 패턴"은 포트의 `pattern` 필드에 그대로 매핑. 복합 패턴 처리도 원본의 `references/agent-design-patterns.md` "복합 패턴" 표와 1:1 대응.
- 원본 Phase 7의 "변경 이력" 테이블(CLAUDE.md)은 포트에서도 동일 포맷 유지. workflow.json 자체는 이력을 담지 않으며, 이력은 `.gemini/context.md` + CLAUDE.md에서 관리.

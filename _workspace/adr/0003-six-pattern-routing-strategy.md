---
id: 0003
title: 6 아키텍처 패턴의 Manager._route 매핑 전략
status: accepted
date: 2026-04-19
sources:
  - ~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/skills/harness/references/agent-design-patterns.md
  - .claude/skills/langgraph-patterns/SKILL.md
  - _workspace/adr/0001-architecture-manager-worker-registry.md
  - _workspace/adr/0002-workflow-json-as-registry-snapshot.md
---

## Context

원본 harness는 6개 패턴(Pipeline, Fan-out/Fan-in, Expert Pool, Producer-Reviewer, Supervisor, Hierarchical)을 모두 지원하며 "누락 금지"가 `harness-port-spec`의 명시 제약이다. ADR 0001은 단일 그래프를 고정했으므로, 패턴 차이는 Manager의 라우팅 로직으로만 표현되어야 한다. 패턴별 라우팅 계약과 종료 조건을 미리 고정하지 않으면 구현자가 임의 해석할 위험이 있고, QA의 동등성 시나리오도 재현 불가.

## Decision

**Manager는 `state["workflow"]["pattern"]`을 보고 `_route_{pattern}` 함수로 분기한다. 6개 라우팅 함수의 계약을 아래와 같이 고정한다.**

| 패턴 | 라우팅 계약 | 종료 조건 | 필수 routing_config |
|------|-----------|---------|-------------------|
| **pipeline** | `registry`를 순서대로 스캔, 첫 non-completed id 반환 | 전원 completed | — |
| **fan_out_fan_in** | 미실행 워커에 대해 `[Send("worker", sub_state) …]` 반환. 전원 completed면 `integrator_id` 반환, integrator도 completed면 END | integrator completed | `integrator_id` |
| **expert_pool** | `state["current_task"]` 내용을 분류기 함수(`routing_config.classifier`)로 분류 → 매칭되는 expert id 반환. 한 번에 하나 | current_task 소진 | `classifier`(LLM 라우터 프롬프트 or 키워드 맵) |
| **producer_reviewer** | last_completed가 없으면 producer → 있으면 producer면 reviewer → reviewer면 `test_passed`면 END, 아니면 `retry_count < retry_limit` 조건에 따라 producer(+1) 또는 END | test_passed 또는 retry 초과 | `producer_id`, `reviewer_id` |
| **supervisor** | 먼저 supervisor가 실행되지 않았거나 task_queue가 stale이면 `supervisor_id` 반환. 그 다음 runnable task + idle worker 매칭으로 worker id 반환 | task_queue 전원 done | `supervisor_id` |
| **hierarchical** | 최상위(`root_id`)부터 실행. 상위가 `create_agents`로 하위를 만들면 하위 실행. 하위 전원 completed면 상위 재실행 | root completed + 모든 자손 completed, 최대 `max_depth=2` | `root_id`, `max_depth` |

복합 패턴은 `"fan_out_fan_in+producer_reviewer"` 같은 `+` 연결 문자열. Manager는 `state["phase"]` 필드를 보고 현재 활성 sub-pattern을 선택 후 해당 `_route_*`에 위임. `phase` 전환 규칙은 `routing_config.phase_map`에 선언 (예: `{"collect": "fan_out_fan_in", "review": "producer_reviewer"}`).

라우터 함수 반환값 타입은 `str | None | list[Send]`로 통일. None은 END, Send 리스트는 병렬 dispatch(LangGraph의 Command에 담아 반환).

## Consequences

### Positive

- **원본과의 동등성 시나리오 재현 가능**: `harness-port-spec`의 5개 검증 시나리오(리서치=fan_out, 웹툰=producer_reviewer, 코드 마이그레이션=supervisor, 풀스택=hierarchical, 고객 문의=supervisor+expert_pool)가 모두 이 표로 커버.
- **테스트 매트릭스가 명확**: 각 `_route_*`를 단위 테스트로 격리. State 픽스처 × 기대 Command 매트릭스.
- **복합 패턴 확장성**: `+` 연결 문자열 + `phase_map`으로 N-단 합성 지원. v2에서 JSON 객체화할 때도 하위 호환 레이어 단순.

### Negative / Risks

- **retry_count 전파 버그 위험**: producer_reviewer에서 retry 증가를 reducer(+1)로 안전하게 하지 않으면 병렬 실행 중 증가분이 유실. → State 스펙에서 retry_count는 단일 writer(Manager)만 쓰도록 규약화.
- **expert_pool 분류기 품질 의존**: `classifier`가 LLM 프롬프트면 비용·지연 증가. 키워드 맵 fallback 필수.
- **hierarchical 깊이 제한**: 2단계 초과는 불가. 원본도 권장 깊이 2이므로 동등. 사용자가 3단 요청 시 flatten 제안.
- **Send API 시그니처 드리프트**: LangGraph 버전별로 Send 인자 형태 차이. `runtime/compat.py`에서만 import (ADR 0005).

## Alternatives Considered

| 대안 | 기각 사유 |
|------|---------|
| **패턴마다 별도 StateGraph 빌드** | ADR 0001 단일 그래프 전제 위반. 체크포인터 분산 |
| **`_route`를 하나로 만들고 if/elif 체인** | 함수 길이 폭발, 테스트 분리 어려움. `_route_{pattern}` 분리가 표준 |
| **LangGraph 내장 `Supervisor`/`Swarm` 프리셋 직접 사용** | 원본 6패턴 중 일부만 매칭. 복합 패턴 표현 불가 |
| **런타임에 패턴 동적 전환** | `state["phase"]` 전환으로 충분. 전체 pattern swap은 재실행 시나리오로 처리(= 새 workflow.json) |

## Upstream Alignment

- 원본 `references/agent-design-patterns.md`의 6 패턴 정의·예시와 1:1 매핑. 용어("팬아웃/팬인", "생성-검증" 등) 한국어 보존.
- 원본 복합 패턴 표(팬아웃+생성-검증, 파이프라인+팬아웃, 감독자+전문가 풀)가 `+` 문자열로 그대로 표현 가능함을 QA 시나리오로 검증(Task #6 예정).
- 원본 Phase 2의 "아키텍처 패턴 선택"은 포트에서 Phase 1 도메인 분석 결과를 입력 삼아 `pattern` 필드를 결정(`harness-architect`가 수행). 결정 근거는 `_workspace/adr/`에 ADR로 남긴다는 원본 원칙 계승.

## Addendum — 2026-04-19 (QA-MI-2)

**복합 패턴의 phase 전환 책임:** `pattern`이 `"+"`를 포함할 때, Manager는 `state.phase` + `routing_config.phase_map`으로 활성 sub-pattern을 결정한다. **`state.phase` 값을 설정·전환하는 것은 Worker의 책임**이다:

- Worker는 구조화 응답에 `status_update.phase: "{new_phase}"` 필드를 포함하여 Manager에게 phase 전환을 통보
- Manager는 phase를 **읽기만** 하며, 자동 전진 로직 없음 (단일 writer 원칙, ADR 0001 연장)
- phase가 전환되지 않으면 `phase_map[phase]`가 가리키는 동일 sub-pattern이 계속 적용

`runtime/worker.py` docstring "Composite-pattern phase responsibility" 섹션에 동일 내용 명시됨.

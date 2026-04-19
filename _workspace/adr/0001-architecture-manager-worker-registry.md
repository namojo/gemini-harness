---
id: 0001
title: Manager + Worker + Registry 아키텍처 채택 (Swarm-style)
status: accepted
date: 2026-04-19
sources:
  - ~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/skills/harness/SKILL.md
  - ~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/skills/harness/references/agent-design-patterns.md
  - .claude/skills/langgraph-patterns/SKILL.md
  - .claude/skills/harness-port-spec/SKILL.md
---

## Context

revfactory/harness v1.2.0의 핵심 UX는 **메타 에이전트**(에이전트가 에이전트를 만든다)와 **6가지 아키텍처 패턴**이다. 원본은 Claude Code의 `TeamCreate`·`Agent` 원시형으로 런타임에 프로세스를 스폰한다. LangGraph + Gemini로 포팅할 때 선택지는 두 갈래다.

1. **정적 그래프 + 런타임 `add_node`**: 새 에이전트가 생기면 StateGraph에 노드를 추가하고 재컴파일. LangGraph에서 `StateGraph.compile()` 이후 노드 변경은 지원되지 않으며, 재컴파일 시 체크포인터 상태가 깨진다. 메타 에이전트 요구사항과 충돌.
2. **정적 3노드 그래프 + State.registry**: 그래프는 `manager → worker → manager` 루프로 고정. 에이전트는 State의 `registry: list[AgentMetadata]`에 엔트리로 존재하고, Worker는 `state["current_target"]`으로 전달된 registry 엔트리를 조회해 Gemini를 호출한다. 새 에이전트 생성은 registry append(append_unique reducer)로 구현.

원본 스펙의 "세션당 1팀 활성 제약"은 프로세스 모델의 특성이지 기능 요구가 아니므로, 포트에서는 논리적 그룹(태그)로 대체하며 **다수 에이전트가 registry에 공존**한다.

## Decision

**Manager + Worker + Registry (Swarm-style) 아키텍처를 기본으로 채택한다.**

- 그래프 토폴로지는 `START → manager → worker → manager → ... → END`로 고정. Manager는 `Command(goto="worker" | END, update={"current_target": ...})`를 반환.
- 모든 에이전트는 State의 `registry` 리스트에 `AgentMetadata` 엔트리로 존재. 런타임 생성은 registry append로만 표현.
- Worker는 단일 dispatcher 노드. `current_target`의 system_prompt_path를 읽어 Gemini를 1회 호출하고, 응답의 `create_agents` / `send_messages` / `artifacts` 필드를 State 업데이트로 반환.
- 6개 패턴은 모두 같은 그래프 위에서 Manager의 `_route_*` 함수 차이로 구현 (ADR 0003).

## Consequences

### Positive

- **메타 에이전트 요구 충족**: 런타임에 새 에이전트를 만들어도 그래프 재컴파일 불필요. 다음 Manager 호출이 즉시 새 registry 엔트리를 참조.
- **체크포인터 호환**: 그래프가 고정이므로 `SqliteSaver`로 thread_id 재개가 안정적.
- **테스트 경량화**: Manager는 순수 State → Command 함수. Gemini mock 없이 단위 테스트 가능.
- **6 패턴 통일**: 새 패턴 추가도 `_route_*` 함수 하나만 쓰면 된다. 구조 변경 없음.

### Negative / Risks

- **Worker가 단일 노드라 병렬 실행이 기본이 아님**. Fan-out은 LangGraph의 `Send` 원시형으로 해결하지만, Send의 시그니처가 LangGraph 버전별로 다를 수 있어 `runtime/compat.py` 어댑터 필수 (ADR 0005).
- **State가 비대해질 수 있음**. registry·inbox·history·artifacts가 한 TypedDict에 모인다. 체크포인터 I/O 비용을 줄이려면 artifacts는 경로만 들고 실제 파일은 `_workspace/`에 저장.
- **디버깅 경험 저하**: LangGraph Studio의 노드 그래프 시각화가 "2노드 루프"로만 보여 실제 에이전트 흐름을 드러내지 못함. `.gemini/context.md` 이벤트 로그로 보완.

## Alternatives Considered

| 대안 | 기각 사유 |
|------|---------|
| **런타임 `add_node` + 재컴파일** | LangGraph public API로는 compile 후 변경 불가. 내부 모듈 접근은 ADR 0005 정책 위반 |
| **NODE_FACTORY 빌드타임 방식** | `langgraph-patterns` 부록에 명시된 대로 고정 파이프라인용. 메타 에이전트(런타임 생성) 지원 불가 — harness의 핵심 가치 상실 |
| **Supervisor 패턴(langgraph 내장)을 직접 사용** | 패턴 하나만 커버. 6 패턴 통일 요건 미충족 |
| **에이전트별 독립 그래프 + 외부 dispatcher** | LangGraph checkpointer·streaming 이점을 포기. 체크포인트 분산 관리 복잡도 증가 |

## Upstream Alignment

- 원본 `skills/harness/SKILL.md` Phase 3은 `.claude/agents/*.md`를 **파일로** 생성하고 `Agent` 도구로 호출한다. 포트에서는 `.agents/{name}/SYSTEM_PROMPT.md`로 파일을 보존(동일 UX), 호출은 Worker가 대행.
- 원본 Phase 2의 6 패턴 선택은 보존한다. 포트는 `workflow.json.pattern` 필드 값으로 Manager 라우팅을 분기(ADR 0003).
- 원본의 "팀" 개념은 registry의 `group` 태그로 표현(`meta-agent-templates` 스키마 `group` 필드). 세션당 1팀 제약은 포트 레이어에서 불필요해 폐기하되, UX상 사용자에게는 여전히 "하나의 하네스"로 제시.

---
name: harness-port-spec
description: revfactory/harness v1.2.0을 Gemini + LangGraph 스택으로 포팅할 때 보존해야 할 사양과 원시형 매핑. Claude Code 원시형(TeamCreate/SendMessage/TaskCreate/Agent)과 LangGraph 구현의 대응, 6개 아키텍처 패턴의 LangGraph 변환 힌트, 출력 포맷(`.agents/`, workflow.json) 명세. 포팅 결정·원본 스펙 확인·구현 일관성 검토 시 반드시 참조. "원본과 동등한가" 검증 기준 포함.
---

# Harness Port Spec (revfactory/harness → Gemini + LangGraph)

Gemini-Harness는 **revfactory/harness v1.2.0의 직접 포트**다. 구조·출력·철학을 보존하고 런타임을 Claude Code → Gemini 3.1 Pro Preview + LangGraph로 교체한다.

## 원본 스펙 위치 (항상 포팅 결정 전 먼저 읽어라)

**로컬 플러그인 캐시:**
```
~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/
├── .claude-plugin/plugin.json          — 메타
└── skills/harness/
    ├── SKILL.md                        — 전체 워크플로우 (Phase 0~7)
    └── references/
        ├── agent-design-patterns.md    — 6 패턴 + 모드 선택
        ├── orchestrator-template.md    — 3 오케스트레이터 템플릿 (A/B/C)
        ├── team-examples.md            — 실제 팀 예시
        ├── qa-agent-guide.md           — QA 에이전트 가이드
        ├── skill-writing-guide.md      — 스킬 작성 패턴
        └── skill-testing-guide.md      — 스킬 테스트 방법론
```

**원격:** https://github.com/revfactory/harness

**원칙:** 포팅 결정을 내리기 전 관련 원본 문서를 **반드시** Read. 기억·추측으로 진행 금지. 원본이 진화하면 이 경로를 재스캔.

## 포트 목표: 무엇을 보존하고 무엇을 교체하는가

### 보존 (Preserve) — 동일한 사용자 경험

| 항목 | 원본 | Gemini 포트 |
|------|------|-----------|
| 트리거 | "하네스 구성해줘" (한/영/일) | 동일 |
| 입력 | 도메인 한 문장 | 동일 |
| 출력 구조 | `.claude/agents/`, `.claude/skills/` | `.agents/`, `.agents/skills/` (Gemini CLI 규약) |
| 워크플로우 | Phase 0~7 | 동일 (순서·의미 보존) |
| 6 아키텍처 패턴 | Pipeline, Fan-out/Fan-in, Expert Pool, Producer-Reviewer, Supervisor, Hierarchical Delegation | 6개 모두 지원 (누락 금지) |
| 진화 철학 | 변경 이력 테이블, 피드백 반영, 재실행 지원 | 동일 |
| CLAUDE.md 등록 | 포인터 + 변경 이력 | 동일 |

### 교체 (Replace) — 런타임과 원시형만

| 원본 (Claude Code) | 포트 (LangGraph + Gemini, Manager+Worker+Registry) |
|------------------|---------------------------------------------|
| `TeamCreate({members})` | `state.registry` 필드에 `AgentMetadata` 리스트 append (reducer: `append_unique`) |
| `SendMessage({to, content})` | Worker 응답의 `send_messages` → `state.inbox[to]`에 append |
| `TaskCreate({tasks})` | Supervisor 패턴의 `state.task_queue` 필드 + Manager의 동적 할당 로직 |
| `Agent` 도구 (서브 에이전트) | Worker 노드에서 Gemini 1회 호출, 또는 하위 StateGraph를 invoke (Hierarchical) |
| 세션당 1팀 활성 제약 | **없음** — 다수 에이전트가 State registry에 공존. "팀" 개념은 registry의 태그(status, group) |
| Claude 모델 (opus) | `gemini-3.1-pro-preview` |
| Skill 도구 호출 | `.agents/skills/{name}/SKILL.md`를 읽어 시스템 프롬프트에 inline, 또는 `entry:` 스크립트 subprocess 실행 |
| Claude Code UI | Gemini CLI v0.28.0+ 익스텐션 + MCP 서버 (상세: `gemini-cli-extension-packaging` 스킬) |

**핵심 차이:** 원본의 `TeamCreate`는 프로세스(실제 에이전트 스폰)를 의미하지만, 포트의 "팀"은 **State의 논리적 그룹**이다. Worker 노드는 단 하나이며 dispatcher 역할 — 다양한 에이전트는 registry의 메타데이터로 표현되고 Worker가 current_target에 따라 선택 실행. 상세 아키텍처: `langgraph-patterns` 스킬.

## Claude Code 원시형 → LangGraph 매핑 (Manager+Worker+Registry 기반)

### TeamCreate → registry append + workflow.json(초기 스냅샷)

```json
// workflow.json (초기 registry 스냅샷 + 패턴 메타)
{
  "version": "1.0",
  "pattern": "fan_out_fan_in",
  "initial_registry": [
    {"id": "researcher-a", "name": "researcher-a", "role": "공식 채널 조사",
     "system_prompt_path": ".agents/researcher-a/SYSTEM_PROMPT.md",
     "skills": ["web-research"], "tools": ["google-search"]},
    {"id": "researcher-b", "name": "researcher-b", "role": "커뮤니티 조사",
     "system_prompt_path": ".agents/researcher-b/SYSTEM_PROMPT.md",
     "skills": ["web-research"], "tools": ["google-search"]}
  ]
}
```

런타임에 `TeamCreate` 효과는 Worker 응답의 `create_agents` 필드 → registry append로 구현. **그래프 재컴파일 불필요, 메타 에이전트가 즉시 팀 확장**.

### SendMessage → inbox 업데이트

Worker가 응답에 `send_messages: [{to, content}, ...]`를 포함하면, 해당 수신자의 inbox에 append. 다음 Manager 사이클에서 수신자를 current_target으로 지정하면 Worker가 자기 inbox를 읽는다.

```python
# Worker 출력 (예)
return {
    "inbox": {"researcher-b": [Message(from_id="researcher-a", content="공식 발표 공유: ...")]},
    "history": [...],
}
```

### TaskCreate → Supervisor 패턴의 task_queue

Fan-out 같은 단순 병렬은 Manager가 Send로 직접 dispatch하면 충분. 동적 할당이 필요한 **Supervisor 패턴**에서만 `state.task_queue` 필드 사용:

```python
class SupervisorState(HarnessState):
    task_queue: list[Task]   # {id, title, status, assignee, depends_on, result}

def manager_supervisor(state) -> Command:
    # 감독자 에이전트 실행 시 task_queue를 갱신
    # 그 후 available worker + runnable task를 매칭하여 dispatch
    ...
```

### Agent 도구 → Worker 단일 노드

원본은 에이전트마다 별도 프로세스 스폰. 포트는 **모든 에이전트가 Worker 노드 하나를 공유**하며 `current_target`으로 구분. 계층적 위임(Hierarchical)의 경우 Worker 안에서 하위 StateGraph를 invoke.

### 메타 에이전트 (에이전트가 에이전트 생성)

원본 하네스의 핵심 특징. 포트 구현:
1. Worker가 Gemini 호출 → 응답에 `create_agents: [{name, role, ...}]` 포함
2. Worker가 `meta_linter.py`로 메타데이터 검증
3. 통과하면 `.agents/{name}/SYSTEM_PROMPT.md` 디스크 작성 + State registry append
4. 다음 Manager 사이클에서 새 에이전트가 current_target으로 선택 가능

상세 구현: `langgraph-patterns` 스킬의 "Worker 노드: Dispatcher".

## Feature Parity Checklist (모든 특징 구현)

사용자가 요구한 "모든 특징"을 12개 feature로 인벤토리화. 각 항목은 포트에서 작동해야 "동등하다"고 주장 가능.

| # | 원본 feature | 포트 구현 경로 | 담당 |
|---|-------------|-------------|------|
| 1 | Phase 0 현황 감사 | MCP `harness.audit` + `.agents/` 스캔 노드 | langgraph-developer |
| 2 | Phase 1 도메인 분석 + 숙련도 감지 | 초기 Gemini 호출에서 domain extractor + skill-level detector | harness-architect |
| 3 | Phase 2 팀 아키텍처 (6 패턴) | Manager 라우팅의 `_route_*` 함수 6개 | harness-architect + langgraph-developer |
| 4 | Phase 3 에이전트 정의 생성 | Worker의 `create_agents` 처리 → SYSTEM_PROMPT.md 생성 + `meta_linter` | meta-skill-designer |
| 5 | Phase 4 스킬 생성 (Progressive Disclosure) | Worker의 `create_skills` 처리 → SKILL.md + `entry:` 파일 + references 분리 | meta-skill-designer |
| 6 | Phase 5 오케스트레이터 스킬 생성 | 생성된 오케스트레이터 SKILL.md가 MCP `harness.run` 도구로 실행 | meta-skill-designer |
| 7 | Phase 6 검증 (구조·트리거·드라이런) | MCP `harness.verify` 도구, near-miss 트리거 테스트 포함 | harness-qa |
| 8 | Phase 7 진화 (변경 이력·피드백) | MCP `harness.evolve` 도구, CLAUDE.md 자동 갱신 | harness-architect |
| 9 | **Meta agent** (에이전트가 에이전트 생성) | Worker 응답의 `create_agents` 필드 → registry append, 린터 통과 필수 | langgraph-developer + meta-skill-designer |
| 10 | **Agent team** (팀 구성·멤버십) | registry의 태그 필드(group, status) + Manager 라우팅이 그룹별 dispatch | langgraph-developer |
| 11 | **Inter-agent communication** (SendMessage) | Worker 응답의 `send_messages` → inbox reducer로 merge, Manager 경유 루프 | langgraph-developer |
| 12 | **QA 방법론** (경계면 교차 비교, incremental) | harness-qa 본인 방법론 계승 + MCP `harness.verify`의 교차 검증 노드 | harness-qa |

각 체크박스는 검증 가능한 산출물(MCP 도구, 테스트, 린터)로 증명되어야 한다. 증명 없이 "동등하다" 주장 금지.

## 6 아키텍처 패턴의 LangGraph 매핑

**모든 패턴은 동일한 Manager+Worker+Registry 그래프 위에서** Manager의 `_route` 로직 차이로 구현한다. 새 노드 추가 없음.

| 패턴 | Manager 라우팅 요약 |
|------|-----------------|
| **1. Pipeline** | registry 순서 유지, 이전 에이전트 status="completed"면 다음 id 반환 |
| **2. Fan-out/Fan-in** | 미실행 에이전트들에 대해 `Send`로 병렬 dispatch → 전부 completed면 integrator id 반환 |
| **3. Expert Pool** | 현재 task 내용을 라우터 함수가 분류 → 해당 전문가 id 반환 (한 번에 하나) |
| **4. Producer-Reviewer** | producer → reviewer → (test_passed?) END / (retry<limit) producer(retry++) |
| **5. Supervisor** | state.task_queue를 감독자 에이전트가 갱신 → runnable task + idle worker 매칭 |
| **6. Hierarchical** | 상위 에이전트의 `create_agents` → registry append → 하위 완료 후 상위 재선택. 깊이 2 권장 |

**복합 패턴** (fan-out + producer-reviewer, pipeline + fan-out 등)은 `_route`가 `state.phase` 필드를 보고 분기. 같은 그래프 그대로, 라우팅만 합성.

상세 구현: `langgraph-patterns` 스킬의 "6 아키텍처 패턴 구현".

## 출력 포맷

### `.agents/{name}/SYSTEM_PROMPT.md`
원본 Claude Code 에이전트 정의와 유사하나:
- `model: gemini-3.1-pro-preview`
- `tools:` 필드는 Gemini CLI 익스텐션 이름 (`file-manager`, `google-search`, `mcp:{name}`)
- 본문 구조는 원본과 동일 (핵심 역할 · 작업 원칙 · 입출력 · 팀 통신 · 에러 · 자가 검증)

상세 스키마는 `meta-agent-templates` 스킬.

### `.agents/skills/{name}/SKILL.md`
- `runtime: python | bash` 필수
- `entry:` 필수 — 실제 파일이 존재해야 린터 통과

### `workflow.json`
LangGraph 그래프 정의. 스키마는 `meta-agent-templates` 스킬 참조.

### 프로젝트 `CLAUDE.md` 포인터
원본 하네스가 CLAUDE.md에 쓰는 것과 동일한 포인터 블록. 변경 이력 테이블 형식도 동일.

## Phase별 포팅 체크리스트

| Phase | 원본 | 포트 시 유의 |
|-------|------|-----------|
| 0. 현황 감사 | `.claude/agents/` 스캔 | `.agents/` 스캔, `workflow.json` 존재 여부 |
| 1. 도메인 분석 | Claude 직접 대화 | Gemini 호출, 숙련도 감지 포함 |
| 2. 팀 아키텍처 설계 | 6 패턴 중 선택 | 동일. 선택 근거를 `_workspace/adr/`에 기록 |
| 3. 에이전트 정의 | `.claude/agents/*.md` 생성 | `.agents/*/SYSTEM_PROMPT.md` 생성, 린터 통과 필수 |
| 4. 스킬 생성 | `.claude/skills/{name}/SKILL.md` | `.agents/skills/{name}/SKILL.md`, `entry` 파일 실제 생성 |
| 5. 오케스트레이션 | 오케스트레이터 스킬 | 오케스트레이터 스킬 + `workflow.json` 쌍 |
| 6. 검증 | 구조·트리거·드라이런 | 동일 + Self-critique A/B 정량 측정 |
| 7. 진화 | 변경 이력 테이블 | 동일. `.gemini/context.md`에 세션 로그 |

## 포팅 시 금지 사항

1. **원본 의도 왜곡 금지** — "LangGraph가 더 강력하니 패턴을 재설계하자" 같은 월권 금지. 먼저 정확히 복제한 뒤 확장 여부를 별도 ADR로 판단.
2. **출력 포맷 임의 변경 금지** — 원본 필드 삭제·이름 변경은 드리프트. 추가만 허용하며 추가도 ADR 기록.
3. **Phase 순서 변경 금지** — Phase 0→7은 사용자 경험의 일부. 순서 바꾸면 회귀.
4. **6 패턴 중 누락 금지** — 모든 패턴을 지원해야 "harness"라 부를 수 있다. Supervisor/Hierarchical이 LangGraph로 까다롭다고 빼지 말 것. 구현 난이도는 ADR에 기록하되 기능은 제공.
5. **Phase 1의 숙련도 감지 생략 금지** — 원본의 중요 UX. 커뮤니케이션 톤 조절은 포트에서도 동일하게 동작해야 함.

## 동등성 검증 시나리오

원본·포트 동시 실행 비교로 포트 품질 확인:

| 시나리오 | 기대 패턴 | 검증 포인트 |
|---------|---------|-----------|
| "리서치 하네스 만들어줘 (주제: LangGraph 생태계)" | Fan-out/Fan-in | 3~4개 조사 전문가 생성, 통합 단계 존재 |
| "웹툰 제작 하네스 만들어줘" | Producer-Reviewer | 생성자↔검토자 쌍, retry 루프 |
| "코드 마이그레이션 하네스 만들어줘" | Supervisor | 중앙 task queue + 워커 풀 |
| "풀스택 앱 개발 하네스" | Hierarchical Delegation | 2단계 위임 (프론트팀 + 백엔드팀) |
| "고객 문의 처리 하네스" | Supervisor + Expert Pool | 감독자가 전문가 풀을 동적 호출 |

**통과 기준:** 같은 입력에 **동일 패턴 선택** + 유사 역할 분해. Gemini의 리즈닝이 다른 결정을 내리면 원인 분석(프롬프트 튜닝 필요 여부). 패턴 선택이 체계적으로 빗나가면 `harness-architect`의 시스템 프롬프트 또는 패턴 설명이 원본과 같은 품질로 전달되지 않는다는 신호.

## 참고

- 원본 전체 워크플로우: 로컬 캐시 `skills/harness/SKILL.md`
- 원본 reference 문서: 로컬 캐시 `skills/harness/references/*.md`
- 포팅 구현 패턴: `langgraph-patterns`, `gemini-cli-integration`, `meta-agent-templates`, `self-critique-verification` 스킬

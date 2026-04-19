# Port Mapping: Claude Code 원시형 → LangGraph + Gemini

원본 revfactory/harness v1.2.0 (Claude Code 기반) → Gemini + LangGraph 포트의 원시형 1:1 매핑. 상세 근거는 각 ADR / 스킬 참조.

## 코어 원시형 매핑

| Claude Code 원시형 | 포트 구현 | 상태/경로 | 근거 |
|-----------------|----------|----------|------|
| `TeamCreate({name, members})` | `state.registry`에 `AgentMetadata` 리스트 append (reducer: `append_unique`) + `workflow.json.initial_registry` 스냅샷 | `state["registry"]`, `.agents/{name}/SYSTEM_PROMPT.md` 파일 | ADR 0001, 0002 |
| `TeamDelete(name)` | 포트에서 개념 소멸. "팀"은 `registry[].group` 태그이므로 group 필터로 종료 상태 표시 | `state["registry"][*].status = "completed"` | ADR 0001 |
| `SendMessage({to, content})` | Worker 응답의 `send_messages: [{to, content, kind}]` → `state.inbox[to]`에 append (reducer: `merge_inboxes`) | `state["inbox"]` | `langgraph-patterns` |
| `TaskCreate({tasks})` | Supervisor 패턴에서 `state.task_queue` 필드 갱신. 그 외 패턴은 Manager가 직접 dispatch | `state["task_queue"]` (Supervisor만) | ADR 0003 |
| `TaskUpdate(id, status)` | Worker 응답의 `status_update` 필드 → `state.task_queue[i].status` 갱신 | 동상 | ADR 0003 |
| `Agent(prompt, subagent_type, run_in_background=true)` | Manager가 `Send("worker", sub_state)` 리스트 반환 (LangGraph fan-out). 직렬 호출은 `Command(goto="worker", update={"current_target": id})` | `worker_node` 단일 dispatcher | ADR 0001, 0003 |
| `Skill 도구 호출 (/skill-name)` | `.agents/skills/{name}/SKILL.md`를 Worker가 Read → system_prompt에 inline, 또는 `entry:` 지정 스크립트를 subprocess 실행 | `.agents/skills/` | `harness-port-spec` |

## Phase 산출물 매핑

| 원본 Phase | 원본 산출물 | 포트 산출물 | 담당 |
|----------|-----------|-----------|------|
| 0. 현황 감사 | `.claude/agents/` 스캔 | MCP `harness.audit` + `.agents/` 스캔 노드 | langgraph-developer |
| 1. 도메인 분석 | Claude 대화 | Gemini 호출 + 숙련도 감지 prompt | harness-architect |
| 2. 팀 아키텍처 | 6 패턴 선택 | `workflow.json.pattern` 결정 + Manager `_route_*` | harness-architect + langgraph-developer |
| 3. 에이전트 정의 | `.claude/agents/*.md` | `.agents/{name}/SYSTEM_PROMPT.md` + 린터 | meta-skill-designer |
| 4. 스킬 생성 | `.claude/skills/{name}/SKILL.md` | `.agents/skills/{name}/SKILL.md` + `entry` 파일 | meta-skill-designer |
| 5. 오케스트레이터 | 오케스트레이터 스킬 | 오케스트레이터 스킬 + `workflow.json` 쌍 + MCP `harness.run` | meta-skill-designer |
| 6. 검증 | 구조·트리거·드라이런 | MCP `harness.verify` + self-critique A/B | harness-qa |
| 7. 진화 | CLAUDE.md 변경 이력 | MCP `harness.evolve` + `.gemini/context.md` + CLAUDE.md | harness-architect |

## 출력 포맷 매핑

| 원본 파일 | 포트 파일 | 주요 차이 |
|----------|----------|--------|
| `.claude/agents/{name}.md` frontmatter `model: opus` | `.agents/{name}/SYSTEM_PROMPT.md` frontmatter `model: gemini-3.1-pro-preview` | 모델 ID만 교체, 본문 구조 보존 |
| `.claude/skills/{name}/SKILL.md` | `.agents/skills/{name}/SKILL.md` | `runtime: python \| bash` + `entry:` 필수 (원본은 권장) |
| 오케스트레이터 스킬 단독 | 오케스트레이터 스킬 + `workflow.json` | 스킬이 MCP `harness.run`을 호출하여 workflow.json 실행 |
| CLAUDE.md 포인터 블록 | 동일 포맷 | 트리거는 Gemini CLI 익스텐션 호출로 변경 |

## State 필드 (LangGraph HarnessState)

| 필드 | 타입 | reducer | 용도 |
|------|------|---------|------|
| `registry` | `list[AgentMetadata]` | `append_unique` | 활성 에이전트 집합 |
| `inbox` | `dict[str, list[Message]]` | `merge_inboxes` | 에이전트 간 통신 |
| `current_target` | `str \| None` | 덮어쓰기 | Worker dispatch 대상 |
| `task_queue` | `list[Task]` | 덮어쓰기 (Supervisor 단독 writer) | Supervisor 패턴 |
| `history` | `list[Event]` | `add` | 이벤트 로그 |
| `artifacts` | `dict[path, content]` | 병합 | Worker 산출물 경로 |
| `phase` | `str` | 덮어쓰기 | 복합 패턴 sub-pattern 선택자 |
| `retry_count`, `retry_limit` | `int` | Manager 단독 | retry 상한 |
| `test_passed` | `bool` | 덮어쓰기 | Producer-Reviewer 종료 |
| `errors` | `list[str]` | `add` | 린터/실행 실패 기록 |
| `run_id` | `str` | 고정 | 체크포인터 thread_id |
| `pending_tool_calls` | `list[ToolCall]` | 덮어쓰기 (Worker 단독) | Gemini가 emit한 함수 호출, tool_executor 처리 대기 |
| `tool_results` | `dict[call_id, McpToolResult \| dict]` | 병합 | tool_executor 결과, Worker가 다음 turn에 컨텍스트 주입 |
| `tool_iterations` | `int` | 덮어쓰기 (Manager 증가) | Worker↔ToolExecutor 라운드트립 카운터, `max_tool_iterations` 상한 |

**그래프 토폴로지 (Phase 2 확정):** `START → manager → {worker | tool_executor | END}` 분기. ADR 0001의 "고정 노드" 원칙은 유지(빌드타임 고정), 도구 호출 지원을 위해 `tool_executor` 노드 추가. 런타임 `add_node`는 여전히 금지.

## 금지 사항 (포팅 시)

1. `add_node` 런타임 호출 금지 (ADR 0001)
2. LangGraph 원시형 직접 import 금지 — `runtime/compat.py` 경유 (ADR 0005)
3. 6 패턴 중 누락 금지 (`harness-port-spec`)
4. 린터 통과 전 registry append 금지 (ADR 0004)
5. `.agents/`, `.agents/skills/`, `_workspace/` 외 경로 쓰기 금지 (ADR 0004)

## 참고

- ADR 0001–0005: `_workspace/adr/`
- 스킬: `.claude/skills/{harness-port-spec, langgraph-patterns, meta-agent-templates}/`
- 원본: `~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/`

---
name: langgraph-patterns
description: Gemini-Harness의 핵심 아키텍처 패턴. **Manager + Worker + Registry (Swarm-style)** — 정적 그래프 위에서 State의 agent registry로 런타임 동적 에이전트를 표현하고, 단일 Worker(dispatcher) 노드가 registry 엔트리를 선택 실행한다. Command/Send API, 에이전트 간 통신(inbox), 6 아키텍처 패턴 구현, self-critique 루프, LangGraph 버전 호환 어댑터. harness_runtime.py 설계·구현 시 반드시 참조.
---

# LangGraph Patterns for Gemini-Harness

**원칙:** 그래프 토폴로지는 정적이고 작다(Manager + Worker + END). 에이전트는 State의 `registry`에 메타데이터로 존재한다. 새 에이전트 생성 = State append. 이것이 harness의 meta-agent 요구사항(런타임에 만든 에이전트를 즉시 사용)을 충족하는 유일한 방법이다.

## 버전 호환 정책 (forward compat)

LangGraph는 빠르게 진화한다. 다음 규칙으로 업데이트에 대비:

1. **호환 범위 핀**: `pyproject.toml`에 `langgraph>=X.Y,<next-major` (허용 마이너 업데이트, 메이저 핀). 실제 범위는 context7 MCP로 최신 안정 버전 확인 후 결정.
2. **어댑터 레이어**: LangGraph 원시형(`StateGraph`, `Command`, `Send`, `START`, `END`, 체크포인터)은 `runtime/compat.py` 단일 모듈에서만 import. 애플리케이션 코드는 어댑터만 사용.
3. **context7 MCP 의무화**: 구현 전 `langgraph` 라이브러리 최신 문서 조회. 이 파일의 시그니처는 참고용이며 실제 API와 충돌하면 최신 API 우선.
4. **CI 버전 매트릭스**: `langgraph` 최신 패치 + 직전 마이너 2개를 pytest matrix로 실행.
5. **Deprecation-safe**: LangGraph 내부 private 모듈(`_internal`, undocumented) 접근 금지. Public API만 사용.
6. **Feature flag**: 새로운 API(예: Send 개선)를 사용하고 싶으면 `if hasattr(langgraph, "NewThing"):` 또는 `packaging.version.Version` 비교로 분기. fallback 경로 유지.

## 핵심 패턴: Manager + Worker + Registry

```
            ┌────────────────────────┐
            │       STATE            │
            │  registry: [A, B, C…]  │
            │  inbox: {A:[…],…}      │
            │  current_target: A     │
            └───────────▲────────────┘
                        │ update
┌─────────┐  Command   ┌─┴────────┐
│ Manager │ ─goto────→ │  Worker  │ ─→ 결과 State 업데이트 ─┐
└────▲────┘            └──────────┘                        │
     │                                                      │
     └──────────────────── goto=manager ────────────────────┘
```

- **Manager (Router)**: State를 보고 다음 활성 에이전트 선택 또는 END. `Command(goto="worker", update={"current_target": agent_id})` 반환.
- **Worker (Dispatcher)**: 단일 노드. `state["current_target"]`의 registry 엔트리를 읽어 해당 에이전트의 system_prompt + inbox로 Gemini 호출. 결과를 State 업데이트로 반환.
- **Registry**: State 필드. 초기값은 `workflow.json`의 스냅샷. 런타임에 append되면 바로 다음 Manager 호출부터 사용 가능.

## State 설계

```python
from typing import TypedDict, Annotated, Literal
from operator import add

class AgentMetadata(TypedDict):
    id: str                         # unique, e.g. "researcher-01"
    name: str                       # 표시 이름
    role: str                       # 1~2문장 역할
    system_prompt_path: str         # .agents/{name}/SYSTEM_PROMPT.md
    skills: list[str]               # 사용 스킬 이름
    tools: list[str]                # file-manager, google-search, mcp:...
    created_at: str                 # ISO timestamp
    created_by: str                 # "user" | 다른 에이전트 id (meta-agent)
    status: Literal["idle", "working", "completed", "failed"]

class Message(TypedDict):
    from_id: str
    content: str
    kind: Literal["info", "request", "result"]

def append_unique(lhs: list[AgentMetadata], rhs: list[AgentMetadata]):
    seen = {a["id"] for a in lhs}
    return lhs + [a for a in rhs if a["id"] not in seen]

def merge_inboxes(lhs: dict, rhs: dict):
    out = {**lhs}
    for k, msgs in rhs.items():
        out[k] = out.get(k, []) + msgs
    return out

class HarnessState(TypedDict):
    registry: Annotated[list[AgentMetadata], append_unique]
    inbox: Annotated[dict[str, list[Message]], merge_inboxes]
    current_target: str | None
    history: Annotated[list[dict], add]      # 이벤트 로그
    artifacts: dict[str, str]                # path → content
    test_passed: bool
    retry_count: int
    retry_limit: int
    errors: Annotated[list[str], add]
    run_id: str
```

Reducer가 핵심: `append_unique`로 멱등 생성, `merge_inboxes`로 병렬 노드 메시지 충돌 방지.

## 그래프 구성

```python
from runtime.compat import StateGraph, Command, END, SqliteSaver

def build_harness_graph():
    graph = StateGraph(HarnessState)
    graph.add_node("manager", manager_node)
    graph.add_node("worker", worker_node)
    graph.set_entry_point("manager")
    graph.add_edge("worker", "manager")   # Worker 완료 후 항상 Manager로 복귀
    # manager는 Command로 goto=worker 또는 goto=END 반환 — add_edge 불필요
    return graph.compile(checkpointer=SqliteSaver.from_conn_string("_workspace/checkpoints.db"))
```

그래프 토폴로지는 **고정 3노드**. 에이전트를 아무리 많이 생성해도 그래프는 그대로.

## Manager 노드: 라우팅 로직

```python
def manager_node(state: HarnessState) -> Command:
    # 1) 종료 조건: 모든 태스크 완료 또는 최대 재시도 초과
    if _all_done(state) or state["retry_count"] >= state["retry_limit"]:
        return Command(goto=END)

    # 2) 다음 활성 에이전트 결정 — 아키텍처 패턴별 분기
    next_id = _route(state)   # 6 패턴 구현은 아래 섹션

    if next_id is None:
        return Command(goto=END)

    return Command(
        goto="worker",
        update={"current_target": next_id},
    )
```

`_route`는 State의 registry + history + inbox + artifacts를 읽고 결정. 패턴별 구현은 뒤 섹션.

## Worker 노드: Dispatcher

```python
def worker_node(state: HarnessState) -> dict:
    agent_id = state["current_target"]
    agent = _find_agent(state["registry"], agent_id)
    if agent is None:
        return {"errors": [f"missing agent: {agent_id}"]}

    system_prompt = Path(agent["system_prompt_path"]).read_text()
    inbox = state["inbox"].get(agent_id, [])
    context = _compose_context(state, agent)

    response = gemini_client.call(
        system=system_prompt,
        prompt=_compose_prompt(inbox, context),
        temperature=agent.get("temperature", 0.7),
    )

    # 응답은 구조화 (JSON) — 생성, 메시지 발신, 결과 파일 등
    parsed = _parse_structured_response(response)

    update: dict = {
        "inbox": {agent_id: []},   # 자기 inbox 비우기 (merge_inboxes 기준)
        "history": [{"ts": ..., "agent": agent_id, "event": parsed["event_summary"]}],
    }

    # (a) 메타 에이전트: 새 에이전트 생성
    if parsed.get("create_agents"):
        new_agents = _materialize_agents(parsed["create_agents"], created_by=agent_id)
        update["registry"] = new_agents   # append_unique reducer로 병합
        # SYSTEM_PROMPT.md 파일도 실제 디스크에 작성
        for a in new_agents:
            _write_system_prompt_file(a)

    # (b) 에이전트 간 통신
    if parsed.get("send_messages"):
        update["inbox"] = {
            **update.get("inbox", {}),
            **_build_inbox_additions(parsed["send_messages"], from_id=agent_id),
        }

    # (c) 산출물 기록
    if parsed.get("artifacts"):
        path = _save_artifact(parsed["artifacts"], agent_id)
        update["artifacts"] = {**state["artifacts"], **{path: parsed["artifacts"]["content"]}}

    return update
```

**메타 에이전트**: Gemini 응답에 `create_agents` 필드가 있으면 Worker가 `meta_linter.py`로 검증 후 registry에 append + 파일 생성. 즉시 다음 Manager 호출에서 사용 가능.

**에이전트 간 통신**: `send_messages` 필드가 있으면 수신자 inbox에 append. 수신자가 Worker에서 실행될 때 자기 inbox를 읽음.

## Command & Send API

`Command`는 `update` + `goto`를 한 번에 반환 — Manager가 State 쓰고 즉시 다음 노드 지정.
`Send`는 병렬 dispatch용 — Fan-out/Fan-in 패턴의 핵심.

```python
from runtime.compat import Send

def _route_fanout(state) -> list[Send]:
    # 여러 에이전트를 병렬 호출: 각자의 sub-state로 분기
    return [
        Send("worker", {"current_target": a["id"], **_slice_state(state, a)})
        for a in state["registry"] if _needs_run(a)
    ]
```

`Send`는 LangGraph 버전별 시그니처 차이가 있을 수 있음 — 반드시 `compat.py`에서 import하고 context7로 현재 API 확인.

## 6 아키텍처 패턴 구현 (Manager 라우팅)

모든 패턴은 **동일한 그래프** 위에서 Manager의 `_route` 로직 차이로 구현:

```python
def _route(state) -> str | None:
    pattern = state["workflow"]["pattern"]   # workflow.json의 meta 필드
    if pattern == "pipeline":       return _route_pipeline(state)
    if pattern == "fan_out_fan_in": return _route_fanout(state)
    if pattern == "expert_pool":    return _route_expert_pool(state)
    if pattern == "producer_reviewer": return _route_pr(state)
    if pattern == "supervisor":     return _route_supervisor(state)
    if pattern == "hierarchical":   return _route_hierarchical(state)
```

| 패턴 | Manager 로직 요약 |
|------|----------------|
| **Pipeline** | registry를 순서대로 순회. 이전 단계 status="completed"면 다음. |
| **Fan-out/Fan-in** | 초기엔 `Send` 리스트로 병렬 dispatch. 전부 completed면 integrator 에이전트로 goto. |
| **Expert Pool** | State의 현재 task 내용으로 라우터 함수가 전문가 선택. 선택된 에이전트 하나만 실행. |
| **Producer-Reviewer** | producer → reviewer → (status=fail) producer(retry++) / (pass) END. `retry_count` State로 상한. |
| **Supervisor** | State에 task queue. 감독자 에이전트를 먼저 실행하여 queue 갱신 → Worker가 available 워커에게 배당. |
| **Hierarchical** | 상위 에이전트가 `create_agents`로 하위 팀을 registry에 append → 하위 실행 완료 후 상위 재실행 (깊이 2 권장). |

복합 패턴(fan-out + producer-reviewer 등)은 `_route`가 State의 phase 필드를 보고 분기.

## Self-Critique 루프

Producer-Reviewer가 곧 self-critique. 라우팅 로직 예시:
```python
def _route_pr(state) -> str | None:
    last = _last_completed(state)
    if last is None:
        return _first_producer(state)
    if last["agent_type"] == "producer":
        return _reviewer_id(state)
    if last["agent_type"] == "reviewer":
        passed = state["test_passed"]
        if passed:
            return None  # END
        if state["retry_count"] >= state["retry_limit"]:
            return None  # escalate via Manager's terminate check
        # retry_count는 producer로 돌아갈 때 +1
        return _producer_id(state)
```

리뷰어는 **구체적 실패 assertion**을 `artifacts` 또는 inbox로 남겨야 producer가 정조준 수정 가능. 상세는 `self-critique-verification` 스킬.

## 체크포인트와 스트리밍

```python
from runtime.compat import SqliteSaver

config = {"configurable": {"thread_id": run_id}}
for chunk in graph.stream(initial_state, config):
    _append_context_md(chunk, run_id)
```

`thread_id`로 중단 후 재개. `.gemini/context.md`에는 각 노드의 `event_summary`와 registry 변화를 실시간 append (State 전체를 쓰지 말 것 — 파일 폭주).

## 안티패턴

| 안티패턴 | 이유 | 대안 |
|---------|------|------|
| `add_node`를 런타임에 호출 | StateGraph는 compile 후 불변. 재컴파일은 체크포인트 손실 | State의 registry append만 사용 |
| Worker 내부에서 다른 노드 직접 호출 | 토폴로지 불명확, Command/엣지 우회 | Manager 경유 루프 |
| registry에 mutable 객체 직접 할당 | 병렬 노드 충돌, reducer 우회 | `return {"registry": [new_agent]}` — append_unique reducer |
| inbox를 Worker에서 append하며 자기 것 비우지 않음 | 메시지 무한 재처리 | 항상 자기 inbox를 `{}`로 덮어쓰거나 처리 개수만큼 drop |
| LangGraph 내부 모듈 접근 (`_internal`) | 다음 버전에서 깨짐 | Public API + compat.py 어댑터 |
| retry 상한 없음 | 무한 루프 + 비용 폭발 | State `retry_count` + Manager 종료 체크 |
| `current_target` 없이 Worker 진입 | dispatch 불가 | Manager가 `Command(update={current_target:...})` 필수 |

## 테스트 패턴

- **Manager 단위 테스트**: 다양한 State 시나리오 → 기대 Command 출력 확인 (Gemini 호출 없음)
- **Worker 단위 테스트**: mock Gemini client + 구조화 응답 → State update dict 검증
- **End-to-end**: 작은 registry(2~3 에이전트)로 graph.invoke → 최종 State artifacts 확인
- **메타 에이전트 테스트**: Worker가 `create_agents` 응답 처리 → registry가 append되고 다음 iteration에서 새 에이전트 실행되는지
- **통신 테스트**: Agent A가 send_message → 다음 cycle에서 Agent B의 inbox에 도착 → Agent B가 읽고 응답
- **재개 테스트**: 중간에 프로세스 kill → 같은 thread_id로 재시작 → 이어서 실행

## 부록: 정적 파이프라인 (NODE_FACTORY) — 단순 케이스 전용

에이전트가 고정되고 런타임 생성이 전혀 없는 특수 워크플로우(예: 고정된 linter 파이프라인)에는 NODE_FACTORY 빌드타임 방식도 사용 가능:

```python
NODE_FACTORY = {"lint": build_lint_node, "format": build_format_node}
def build_static_graph(seq):
    g = StateGraph(State)
    for name in seq: g.add_node(name, NODE_FACTORY[name]())
    for a, b in zip(seq, seq[1:]): g.add_edge(a, b)
    return g.compile()
```

**단, harness의 meta-agent 요구(런타임 생성)에는 부적합**. 기본은 Manager+Worker+Registry.

## 참고

- LangGraph 최신 API: context7 MCP로 `langgraph` 조회
- Self-critique A/B 측정: `self-critique-verification` 스킬
- workflow.json(초기 registry) 스키마: `meta-agent-templates` 스킬
- 설치/배포: `gemini-cli-extension-packaging` 스킬

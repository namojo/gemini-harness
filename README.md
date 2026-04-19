# gemini-harness

**한국어** · [English](./README_EN.md)

**Gemini CLI용 팀 아키텍처 팩토리.** 도메인 한 문장을 Gemini 에이전트 팀과 그들이 쓸 스킬로 변환합니다 — LangGraph 기반.

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![Tests](https://img.shields.io/badge/tests-215%20passed-brightgreen.svg)](#development)
[![Port of](https://img.shields.io/badge/port_of-revfactory%2Fharness%20v1.2.0-orange.svg)](https://github.com/revfactory/harness)

---

## 한 줄 요약

> **"하네스 구성해줘. 프론트·백·DB·인프라 통합 아키텍처 팀으로"** → Gemini가 6가지 아키텍처 패턴 중 최적을 자동 선택 → 에이전트 팀 + 스킬 + `workflow.json` + 오케스트레이터를 디스크에 생성 → 즉시 실행 가능.

## Overview

`gemini-harness`는 [revfactory/harness](https://github.com/revfactory/harness) v1.2.0 (Claude Code 플러그인)을 **Gemini CLI + LangGraph** 스택으로 포팅한 구현입니다. 원본의 철학·워크플로우·6개 아키텍처 패턴을 1:1로 보존하고, 런타임만 교체했습니다.

### 핵심 특징

- **메타 에이전트 생성기** — 도메인 한 문장에서 최적의 전문가 팀을 자동 설계
- **6가지 아키텍처 패턴 자동 선택**
  - `pipeline` (순차 의존), `fan_out_fan_in` (병렬 후 통합), `expert_pool` (라우터 기반 선택)
  - `producer_reviewer` (생성-검증 루프), `supervisor` (동적 분배), `hierarchical` (계층 위임)
  - 복합 패턴 지원: `"fan_out_fan_in+producer_reviewer"`
- **Manager + Worker + Registry 런타임** — LangGraph StateGraph 기반, 런타임에 에이전트 동적 추가 가능
- **Self-correction 루프** — `producer_reviewer` 패턴으로 결함 감소 (PRD 목표: 단일 모델 대비 80% 감소)
- **샌드박스 경계** — 생성 산출물은 `.agents/`, `_workspace/`, `.gemini/` 3개 루트에만 쓰기 허용
- **설치형 Gemini CLI 익스텐션** — MCP 서버 + 커스텀 슬래시 명령 + 자연어 트리거 모두 지원

### 제공 MCP 도구

| 도구 | 역할 | 원본 Phase |
|------|------|----------|
| `harness.audit` | 현재 프로젝트의 하네스 상태·drift 스캔 | Phase 0 |
| `harness.build` | 도메인 → 팀·스킬·workflow.json 자동 생성 | Phase 1-5 |
| `harness.verify` | 구조·트리거·드라이런 검증 | Phase 6 |
| `harness.evolve` | 피드백 기반 점진적 수정 (unified diff) | Phase 7 |
| `harness.run` | 생성된 workflow.json을 LangGraph로 실제 실행 | — |

## Requirements

- **Python ≥ 3.11**
- **Gemini CLI ≥ 0.28.0** (v0.36+ 권장)
- **Google Gemini API Key** (`GOOGLE_API_KEY` 또는 `GEMINI_API_KEY`)

## Installation

### 1) 패키지 설치

```bash
# PyPI (배포 후)
pip install gemini-harness

# PyPI (아직 배포전이라 이걸 받으세요)
pip install -i https://test.pypi.org/simple/ gemini-harness==0.1.3

# 또는 소스에서
git clone https://github.com/namojo/gemini-harness
cd gemini-harness
pip install -e '.[dev]'
```

### 2) Gemini CLI 익스텐션 등록

```bash
# 리포 루트에서 실행 — gemini-extension.json, GEMINI.md, commands/를 자동 인식
gemini extensions install /path/to/gemini-harness
```

### 3) API 키 설정

프로젝트 루트에 `.env` 파일 작성 (자동 로딩):

```env
GOOGLE_API_KEY=your_api_key_here
```

또는 환경변수로:

```bash
export GOOGLE_API_KEY=your_api_key_here
```

### 4) 최초 1회 — 모델 선택

설치 직후 **한 번만** 실행하여 사용할 Gemini 모델을 선택합니다. API 키로 접근 가능한 모델 목록을 실제로 조회하고 그 중에서 고를 수 있습니다.

```bash
gemini-harness configure
```

예시 출력:
```
Fetching available Gemini models from your API key...

Available models:
 *  1. gemini-3.1-pro-preview
    2. gemini-2.5-pro
    3. gemini-2.5-flash
    4. gemini-2.0-pro
    ...

Current selection: gemini-3.1-pro-preview
Enter the number of the model to use (blank = keep current):
>
```

저장 위치: `$XDG_CONFIG_HOME/gemini-harness/config.json` (기본 `~/.config/gemini-harness/config.json`, 권한 `0600`).

이후 모델을 바꾸고 싶으면 같은 명령을 다시 실행하거나, 한 번만 오버라이드하고 싶으면 환경변수를 쓰세요:

```bash
# 현재 설정 확인
gemini-harness configure --show

# 대화 없이 즉시 변경
gemini-harness configure --model gemini-2.5-pro

# 일회성 오버라이드 (환경변수가 config보다 우선)
LANGCHAIN_HARNESS_MODEL=gemini-2.0-flash gemini-harness run --project . --input "..."
```

### 5) 설치 확인

```bash
gemini   # REPL 진입 후:
> /mcp list
# 기대 출력:
#   🟢 harness (from gemini-harness) — 5 tools
> /commands
# /harness:build, /harness:audit, /harness:verify, /harness:evolve, /harness:run
```

## Usage

### 경로 A — 자연어 발화 (추천)

Gemini CLI 세션 안에서 자연스럽게 말하세요. `GEMINI.md`가 컨텍스트로 로드되어 Gemini가 적절한 MCP 도구를 자동 호출합니다.

```
> 블로그 작성자와 편집자 팀을 만들어줘
  → harness.build 호출 → producer_reviewer 패턴 자동 선택

> 이 하네스로 "AI 트렌드 3줄 요약" 써줘
  → harness.run 호출 → writer → editor → writer 루프 실행

> editor가 너무 관대해, 더 엄격한 검토로 바꿔줘
  → harness.evolve 호출 → editor의 SYSTEM_PROMPT.md만 diff 수정
```

### 경로 B — 커스텀 슬래시 명령

명시적 호출이 필요할 때:

```
/harness:build "Next.js 웹앱 아키텍처 팀 — 프론트·백·DB·인프라 + 통합자"
/harness:audit
/harness:verify
/harness:run "실제 아키텍처 문서 작성"
/harness:evolve "보안 검토자 에이전트 추가"
```

### 경로 C — MCP 도구 직접 호출

다른 MCP 클라이언트나 스크립트에서:

```python
# 예: Python에서 직접 호출
from gemini_harness.runtime.harness_runtime import run_build, run_harness

build = run_build(
    project_path="/path/to/project",
    domain_description="블로그 작성자+편집자 2인 팀 구성",
)
# → pattern: "producer_reviewer", 2 agents, workflow.json 생성

result = run_harness(
    project_path="/path/to/project",
    user_input="AI 트렌드 블로그 써줘",
)
# → .gemini/context.md에 실시간 로그, _workspace/에 산출물
```

## Generated Layout

`harness.build` 실행 후 프로젝트에 생성되는 파일:

```
your-project/
├── workflow.json                           # 초기 registry 스냅샷 + 패턴 메타
├── CLAUDE.md                               # 하네스 포인터 + 변경 이력 (upsert)
├── .agents/
│   ├── {agent-id}/SYSTEM_PROMPT.md         # 에이전트 페르소나 (YAML frontmatter + 본문)
│   └── skills/{skill-name}/
│       ├── SKILL.md                        # 스킬 매니페스트
│       └── scripts/main.py                 # entry 스크립트 (스텁)
├── _workspace/                             # 런타임 산출물
│   ├── adr/                                # Architecture Decision Records
│   ├── checkpoints/                        # LangGraph SqliteSaver DB
│   ├── qa/                                 # harness.verify 리포트
│   └── {agent}/...                         # 각 실행의 파일 산출물
└── .gemini/
    └── context.md                          # 실시간 실행 로그 (스트리밍)
```

## Architecture

우리가 실제로 만든 것은 **두 레이어의 명확한 분리**입니다. "MCP 서버냐 LangGraph냐"는 선택이 아니라 **층위 관계**:

```
┌── Gemini CLI (오케스트레이터) ─────────────────────────┐
│                                                         │
│  사용자 발화 ─── JSON-RPC stdio ──→ 우리 MCP 서버       │
│                                                         │
│  ┌── 우리 MCP 서버 (transport 레이어) ────────────────┐ │
│  │                                                     │ │
│  │  5 tools 노출:                                      │ │
│  │    audit  →  순수 Python 파일 스캔                  │ │
│  │    verify →  스키마·트리거·드라이런 검증           │ │
│  │    build  →  Gemini 1회 호출 (meta-architect)      │ │
│  │    evolve →  Gemini 1회 호출 (feedback → diff)     │ │
│  │    run    ──────────────→ ★ LangGraph 런타임       │ │
│  │                                                     │ │
│  └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
                                │
                                ▼
           ┌─ LangGraph StateGraph (execution 레이어) ─┐
           │                                             │
           │   START → manager ↔ worker ↔ tool_executor │
           │                                             │
           │   • Manager+Worker+Registry (Swarm-style)  │
           │   • 6 패턴 라우팅 + 복합 패턴              │
           │   • Sqlite 체크포인터 (동기/비동기 2경로)  │
           │   • Send 병렬 fan-out (실측 wall-clock 병렬) │
           │                                             │
           └─────────────────────────────────────────────┘
                                │
                                ▼
              도구 호출 우선순위 (재사용 > 재구현)
              ① Gemini CLI 네이티브 (pre-collection)
              ② 사용자 MCP 서버 proxy (tool_discovery)
              ③ 샌드박스 Python 내장 (last-resort)
              ④ 메타 에이전트가 새 에이전트 생성

                Architecture layers at a glance
```

**레이어별 역할과 위치:**

| 레이어 | 역할 | 구현 파일 |
|-------|------|---------|
| **MCP 서버** | Gemini CLI와의 JSON-RPC 통신, 5 tool 노출, progress notification | `src/gemini_harness/mcp_server.py` |
| **Entrypoints** | 각 tool → 내부 Python 함수 디스패치, non-blocking wrapper (`asyncio.to_thread`) | `runtime/harness_runtime.py`, `runtime/_audit.py`, `_verify.py`, `_build.py`, `_evolve.py`, `_run.py` |
| **LangGraph 그래프** | **`harness.run`이 호출될 때만** 가동 — workflow.json을 StateGraph로 컴파일하여 멀티-에이전트 실행 | `runtime/_run.py`, `manager.py`, `worker.py`, `tool_executor.py`, `patterns/*.py`, `compat.py` |
| **Gemini API** | Worker 노드 내부에서 에이전트별 호출, function-calling 지원 | `integrations/gemini_client.py` |
| **체크포인트** | 중단·재개용 SQLite 지속화 — 동기/비동기 두 경로 | `langgraph-checkpoint-sqlite` (외부) + `compat.py` 어댑터 |
| **도구 발견·proxy** | 사용자 MCP 서버 (`~/.gemini/settings.json`) 자동 발견, `mcp_adapter`로 proxy | `runtime/tool_discovery.py`, `integrations/mcp_adapter.py` |
| **내장 fallback** | 파일 작업용 샌드박스 Python 헬퍼 (last-resort) | `runtime/builtin_tools.py` |

### 우리가 제대로 풀어낸 것

1. **원본 6 아키텍처 패턴 무손실 포팅** — Claude Code의 `TeamCreate`/`SendMessage`/`TaskCreate`를 LangGraph State reducer + `Send`/`Command`로 1:1 매핑. 5개 동등성 시나리오 모두 정적 검증 완료.

2. **Manager + Worker + Registry (Swarm-style)** — 정적 그래프(3 노드) 위에서 State의 `registry` 필드로 에이전트를 동적 표현. **메타 에이전트가 런타임에 새 에이전트를 만들어도 그래프 재컴파일 불필요.**

3. **실제 wall-clock 병렬 실행** — sync worker + `.stream()` 조합의 직렬 실행 버그를 발견·수정하여 `AsyncSqliteSaver` + `.astream()` + `asyncio.to_thread`로 전환. 4 workers × 1s 테스트가 3.24s에 완료되어 **물리적 병렬**을 증명.

4. **루프 방어 메커니즘** — 메타 에이전트가 malformed SYSTEM_PROMPT를 반복 생산하는 무한 retry 버그를 해결. Worker가 실패 이유를 다음 프롬프트에 surface하고, Manager는 3회 연속 실패 시 `create_agent_loop_aborted`로 강제 종료.

5. **도구 재사용 우선 철학** — Gemini CLI 내장/사용자 MCP 서버를 **재구현 없이 재사용**. 슬래시 명령이 pre-collection을 유도하고, Worker가 `~/.gemini/settings.json`을 자동 발견하여 사용자의 기존 MCP를 proxy. 내장 Python 헬퍼는 마지막 수단.

6. **LangGraph 버전 격리** — 모든 `langgraph` import를 `runtime/compat.py` 한 곳에만 두어, LangGraph 업데이트 시 단일 파일만 조정.

7. **TestPyPI 배포 + Gemini CLI 익스텐션 설치 가능** — `pip install gemini-harness` + `gemini extensions install .` 두 명령으로 재현 가능한 전체 파이프라인.

### Runtime: Manager + Worker + Registry

LangGraph StateGraph는 **고정 3노드**입니다:

```
                ┌──────────────────────────┐
                │        STATE             │
                │  registry: [A, B, C…]    │
                │  inbox: {A:[…],…}        │
                │  current_target: A       │
                └───────────▲──────────────┘
                            │ update
┌─────────┐   Command    ┌──┴────────┐
│ Manager │ ──goto────→  │  Worker   │ → 결과 state 업데이트 ─┐
└────▲────┘              └───────────┘                        │
     │                                                         │
     └────────────────── goto=manager ──────────────────────── ┘
```

- **Manager (라우터)**: 6 패턴별 `_route_*()` 로직으로 다음 활성 에이전트를 결정하고 `Command(goto=..., update=...)` 반환
- **Worker (디스패처)**: 단일 노드가 `current_target`의 registry 엔트리를 읽어 해당 에이전트의 `system_prompt` + inbox로 Gemini 호출
- **Registry**: `state.registry` 필드. 메타 에이전트가 새 에이전트를 만들면 `append_unique` reducer로 병합되어 **그래프 재컴파일 없이 즉시 사용 가능**

런타임이 동적으로 에이전트를 추가·호출할 수 있어 원본의 "에이전트가 에이전트를 생성"하는 메타 특성을 보존합니다.

### LangGraph Version Compatibility

LangGraph의 모든 import는 `src/gemini_harness/runtime/compat.py` **한 곳**에만 존재합니다. LangGraph 업데이트 시 이 파일만 조정하면 됩니다.

- 현재 지원: `langgraph>=1.0,<2.0` + `langgraph-checkpoint-sqlite>=2.0,<4.0`
- CI matrix: prev minor / pinned / next pre-release

자세한 내용은 `_workspace/adr/0005-langgraph-version-compat-policy.md` 참조.

## Configuration

### 환경변수

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `GOOGLE_API_KEY` / `GEMINI_API_KEY` | — | **필수.** Google AI Studio API key |
| `LANGCHAIN_HARNESS_MODEL` | `gemini-3.1-pro-preview` | Gemini 모델 오버라이드 |
| `LANGCHAIN_HARNESS_WORKSPACE` | `.` | .env 탐색 경로 오버라이드 |

### workflow.json 스키마

초기 registry 스냅샷 + 패턴 선택자. `pattern`에 `"+"`로 복합 구성 가능:

```json
{
  "version": "1.0",
  "pattern": "fan_out_fan_in+producer_reviewer",
  "retry_limit": 3,
  "routing_config": {
    "integrator_id": "chief-architect",
    "phase_map": {
      "gather": "fan_out_fan_in",
      "refine": "producer_reviewer"
    }
  },
  "initial_registry": [
    {
      "id": "frontend-architect",
      "name": "frontend-architect",
      "role": "Next.js·React 아키텍처 설계 담당",
      "system_prompt_path": ".agents/frontend-architect/SYSTEM_PROMPT.md",
      "skills": ["web-research"],
      "tools": ["google-search"]
    }
  ]
}
```

전체 JSON Schema: `src/gemini_harness/meta/schemas/workflow.v1.json`

## Development

### 테스트 실행

```bash
pip install -e '.[dev]'
pytest                              # 전체 (215 tests)
pytest tests/unit/test_build.py     # 특정 파일
pytest -k fan_out_fan_in            # 패턴 매칭
```

### 라이브 Gemini 스모크 테스트

실제 API를 호출하여 build → run 전체 흐름 검증:

```bash
python3 scripts/smoke/live_full.py
# 기대 출력: "success in <seconds>" + artifacts 파일 경로
```

### 빌드

```bash
pip install --user build
python3 -m build
ls dist/    # gemini_harness-0.1.0-py3-none-any.whl, gemini_harness-0.1.0.tar.gz
```

### 프로젝트 구조

```
src/gemini_harness/
├── runtime/            # LangGraph StateGraph + Manager/Worker + 6 pattern routers
│   ├── compat.py       # LangGraph 단일 import 지점
│   ├── _audit.py       # harness.audit 구현
│   ├── _build.py       # harness.build 구현 (meta-architect Gemini 호출)
│   ├── _evolve.py      # harness.evolve 구현
│   ├── _run.py         # harness.run 구현 (+ tool_executor 디스패처)
│   ├── _verify.py      # harness.verify 구현
│   └── patterns/       # 6 pattern routing logic
├── integrations/       # gemini_client, cli_bridge, mcp_adapter
├── meta/               # linter, templates, schemas, examples
├── cli.py              # `gemini-harness` 엔트리 (audit/build/verify/evolve/run)
└── mcp_server.py       # stdio MCP server
commands/harness/*.toml # Gemini CLI 커스텀 슬래시 명령
gemini-extension.json   # Gemini CLI 익스텐션 매니페스트
GEMINI.md               # Gemini CLI 컨텍스트 (자연어 트리거 매핑)
_workspace/adr/         # 5개 ADR (아키텍처 결정 기록)
```

## Troubleshooting

### `🔴 harness - Disconnected`

`gemini-harness-mcp` 스크립트가 PATH에 없어서입니다. `gemini-extension.json`은 `python3 -m gemini_harness.mcp_server`로 호출하므로 이 문제가 해결되지만, 이전 버전을 설치했다면 uninstall 후 재설치:

```bash
gemini extensions uninstall gemini-harness 2>/dev/null
gemini mcp remove gemini-harness 2>/dev/null       # 중복 등록이 있으면
gemini extensions install /path/to/gemini-harness
```

### `GOOGLE_API_KEY not set`

`.env` 파일이 프로젝트 루트(Gemini CLI가 실행된 cwd)에 있는지 확인. `gemini` 세션의 현재 디렉토리에서 `.env`를 찾습니다.

### 스키마 violation

`harness.audit`이 drift를 발견하면 `harness.evolve`로 수정할 수 있습니다. 수동 수정 후에는 `harness.verify`로 재검증 권장.

## Acknowledgments

### 🙇 Special thanks — **황민호 (Minho Hwang)**

이 프로젝트는 [**황민호님(@revfactory)**](https://github.com/revfactory)이 설계·공개한 [`revfactory/harness`](https://github.com/revfactory/harness) v1.2.0를 원본으로 하는 포트입니다. 6개 아키텍처 패턴의 체계, Phase 0~7 워크플로우, 메타 에이전트가 에이전트를 낳는 사고방식, 그리고 변경 이력을 통한 하네스 진화 철학까지 — 이 포트의 뼈대 **전부가 그의 원작에서 나왔습니다**. 멋진 하네스를 오픈소스로 공개해 주셔서 진심으로 감사드립니다.

> *"The best way to honor a great abstraction is to port it and see it hold up."* — 이 포트는 원본이 충분히 좋은 추상화였음을 LangGraph + Gemini 스택에서 재현하여 증명한 사례입니다.

### 기반 기술 스택

- **[LangGraph](https://langchain-ai.github.io/langgraph/)** — StateGraph·체크포인터·Send/Command 런타임. Manager+Worker+Registry를 실현해 준 엔진.
- **[Google Gemini](https://ai.google.dev/)** — meta-architect와 worker를 구동하는 추론 엔진 (기본: `gemini-3.1-pro-preview`).
- **[Model Context Protocol](https://modelcontextprotocol.io/)** — Gemini CLI와의 stdio 통신 표준.
- **[Gemini CLI](https://github.com/google-gemini/gemini-cli)** — transport 레이어이자 슬래시 명령·`write_todos` HUD의 호스트.

## License

Apache License 2.0 — 원본과 동일. `LICENSE` 파일 참조.

## Contributing

Issue · PR 환영합니다. 대규모 변경은 먼저 issue로 설계를 논의해주세요. 하네스 자체를 수정할 때는 `_workspace/adr/`에 ADR을 추가하여 결정 근거를 기록합니다.

---

**Port Source:** [revfactory/harness](https://github.com/revfactory/harness) v1.2.0 (2026-04)
**Maintained by:** [@namojo](https://github.com/namojo)

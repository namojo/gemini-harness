---
name: gemini-harness-builder
description: Gemini-Harness 프로젝트(revfactory/harness v1.2.0의 LangGraph+Gemini 포트)의 설계·구현·통합·검증을 전문가 팀으로 수행하는 오케스트레이터. LangGraph 런타임 작성(harness_runtime.py), workflow.json 스키마 설계, Gemini CLI(v0.28.0+) 통합, gemini-3.1-pro-preview API 래핑, 메타 에이전트/스킬 템플릿 설계, 6 아키텍처 패턴 포트, self-critique 루프 구현, 통합 QA 등 본 프로젝트 관련 작업에 반드시 이 스킬을 사용. 후속 작업(다시 실행, 부분 수정, 재검증, workflow.json 스키마만 수정, 특정 에이전트만 재호출, 이전 결과 개선, 업데이트, 보완, 원본 하네스 재동기화)에도 이 스킬을 사용. 단순 개념 질문(LangGraph란?)은 제외.
---

# Gemini-Harness Builder Orchestrator

**revfactory/harness v1.2.0 → Gemini + LangGraph 포트**를 전문가 팀으로 개발한다.

5명의 팀: **harness-architect** · **langgraph-developer** · **gemini-integrator** · **meta-skill-designer** · **harness-qa**

**포트 원칙:** 원본 구조·출력·철학 보존, 런타임만 교체. 상세는 `harness-port-spec` 스킬 필수 참조.

모든 Agent/TeamCreate 호출 시 `model: "opus"` 파라미터를 명시하라.

## 실행 모드: 하이브리드

- **Phase 1~3**: 에이전트 팀 (TeamCreate) — 설계·구현 단계는 교차 의견과 실시간 조율이 중요
- **Phase 4 (QA)**: 서브 에이전트 (Agent 직접 호출) — 독립적 검증, 객관성 확보

## Phase 0: 컨텍스트 확인

작업 시작 전 기존 산출물 상태 파악:

1. `_workspace/` 디렉토리 존재 여부 확인
2. 이전 산출물 스캔: `harness_runtime.py`, `.agents/`, `_workspace/adr/`, `_workspace/schema/`
3. 사용자 요청 분류:

| 상황 | 판정 | 행동 |
|------|------|------|
| `_workspace/` 없음, 첫 구축 요청 | 초기 실행 | Phase 1부터 전체 |
| `_workspace/` 있음, 특정 영역 수정 요청 | 부분 재실행 | 해당 에이전트만 재호출, 영향 분석 |
| `_workspace/` 있음, 새 요구사항 | 전면 재실행 | 기존 `_workspace/` → `_workspace_prev/` 이동 후 Phase 1 |
| 기존 산출물 검증 요청 | QA 전용 | Phase 4만 실행 |

4. 사용자에게 판정 결과와 실행 계획 한 줄 보고. 모호하면 확인 질문.

## Phase 1: 원본 스펙 내재화 및 설계 계약

**실행 모드:** 에이전트 팀

```
TeamCreate(team_name="design-team", members=["harness-architect", "meta-skill-designer"])
```

작업 순서:

**1-0. 원본 스펙 Read (필수, 생략 금지):**
팀원 모두 `harness-port-spec` 스킬을 로드하고, 그 지시에 따라 로컬 캐시 원본을 Read:
- `~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/skills/harness/SKILL.md` (전체 워크플로우)
- `.../references/agent-design-patterns.md` (6 패턴)
- `.../references/orchestrator-template.md` (3 템플릿)
- `.../references/team-examples.md` (실제 예시)
- 기타 references는 필요 시 조건부 로드

harness-architect는 원본과 포트의 **차이를 유발하는 결정**을 식별하고 그 목록을 작성(어느 Claude Code 원시형이 LangGraph 어떤 구조로 매핑되는가).

**1-1. 요구사항 정리:** harness-architect가 ADR로 → `_workspace/adr/0001-*.md`. ADR마다 원본의 어느 섹션에 대응하는지 명시(예: `source: references/agent-design-patterns.md §1-6`).

**1-2. 스키마 교차 검토:** harness-architect ↔ meta-skill-designer가 SendMessage로 workflow.json v1 스키마와 메타 템플릿 스키마 합의. 6 패턴을 모두 표현 가능한지 검증.

**산출물:**
- `_workspace/adr/` — 포트 결정 (원본 섹션 참조 필수)
- `_workspace/schema/workflow.v1.json` — 6 패턴 모두 표현 가능
- `_workspace/schema/system_prompt.schema.json`, `skill.schema.json`
- `_workspace/port-mapping.md` — Claude Code 원시형 → LangGraph 대응 매핑표

**체크포인트:** 사용자에게 ADR 제목 리스트 + 스키마 핵심 필드 요약을 보여주고 승인받기. 승인 없이 Phase 2 진입 금지.

## Phase 2: 통합 계약 정의

**실행 모드:** 에이전트 팀 (gemini-integrator 추가)

```
TaskCreate: gemini-integrator에 할당
- Gemini CLI v0.28.0+ 내장 스킬 API 조사 (context7 MCP로 최신 문서)
- gemini-3.1-pro-preview Python SDK 시그니처 확인
- MCP Server 통신 프로토콜 정리
```

harness-architect + gemini-integrator가 LangGraph 노드 ↔ Gemini 호출의 인터페이스 계약 합의.

산출물:
- `_workspace/guide/gemini_integration.md` — API 래퍼 인터페이스 명세
- `_workspace/guide/mcp_tools.md` — MCP 도구 입출력 스키마

## Phase 3: 구현

**실행 모드:** 에이전트 팀 (팀 재구성)

이전 팀 해체 → 새 팀 생성:
```
TeamDelete("design-team")
TeamCreate(team_name="impl-team", members=["langgraph-developer", "gemini-integrator", "meta-skill-designer"])
```

TaskCreate로 병렬 작업 분배:

| 담당 | 작업 |
|------|------|
| langgraph-developer | `runtime/compat.py`(LangGraph 어댑터), `runtime/state.py`, `runtime/manager.py`, `runtime/worker.py`, `runtime/patterns/{6개}.py`, `runtime/harness_runtime.py`, self-critique 라우팅 |
| gemini-integrator | `integrations/gemini_client.py`, `integrations/cli_bridge.py`, `integrations/mcp_adapter.py`, `mcp_server.py`, `cli.py`, `pyproject.toml`, `extension/manifest.json` |
| meta-skill-designer | `meta/templates/`, `meta/schemas/`, `meta/linter.py`, `meta/examples/` |

각 팀원 공통 요구:
- 최신 LangGraph API 확인: 구현 착수 전 context7 MCP로 `langgraph` 조회
- 코드에서 `from langgraph ...` 직접 import는 `runtime/compat.py` 한 곳만 허용
- 스모크 테스트 포함, SendMessage로 인터페이스 질의·합의 자체 조율

**설치형 패키지 산출물 (gemini-integrator):**
- `pyproject.toml` — LangGraph 범위 핀 (`>=X.Y,<next-major`), 스크립트 진입점 (`gemini-harness`, `gemini-harness-mcp`)
- `extension/manifest.json` — Gemini CLI 익스텐션 매니페스트 (한/영/일 트리거 포함)
- MCP 서버 — `harness.audit/build/verify/evolve/run` 5개 도구
- 상세 사양: `gemini-cli-extension-packaging` 스킬

## Phase 4: 통합 QA 및 Self-Critique 측정

**실행 모드:** 서브 에이전트 (harness-qa 단독)

```
Agent(subagent_type="general-purpose", model="opus", ...)
```

harness-qa 작업:
1. **경계면 교차 비교** (필수):
   - workflow.json 실제 파일 ↔ 런타임 파서 (필드 매칭)
   - Gemini API 응답 shape ↔ `gemini_client.py` 파싱
   - 생성된 SYSTEM_PROMPT.md ↔ `meta_linter.py` 검증 항목
2. **원본 동등성 검증** (포트 특화):
   - `harness-port-spec` 스킬의 "동등성 검증 시나리오" 섹션의 5개 시나리오를 실행
   - 각 시나리오에서 기대 패턴(Fan-out/Fan-in, Producer-Reviewer, Supervisor 등)이 실제로 생성되는지 확인
   - 원본 하네스와 같은 입력으로 비교 실행하여 팀 분해의 유사성 평가
3. **End-to-end 실행**: PRD 예시 입력("복잡한 Next.js 프로젝트 아키텍처를 짜줘")으로 실행, `.gemini/context.md` 실시간 업데이트 확인
4. **Self-critique A/B 측정** (기본 5개 입력 × 모드 A/B × 3회):
   - A (baseline): self-critique 비활성
   - B (treatment): self-critique 활성
   - 측정: 결함 수, p95 지연, 토큰 비용
   - 목표: `(mean(A.defects) - mean(B.defects)) / mean(A.defects) >= 0.8`
5. **리포트**: `_workspace/qa/report-{date}.md` + `_workspace/qa/metrics/self_critique.csv` + `_workspace/qa/equivalence.md` (원본 동등성)

## Phase 5: 수정 루프 (조건부)

QA 실패 시:
1. 실패 유형 → 책임 에이전트 식별 (QA 리포트에 명시)
2. 해당 에이전트만 서브 에이전트로 호출하여 수정 (팀 재구성 불필요)
3. 수정 후 Phase 4 재실행
4. **최대 3회 루프**, 이후 사용자 에스컬레이션 (`_workspace/qa/escalations/`)

## 데이터 전달 프로토콜

| 전략 | 적용 | 용도 |
|------|------|------|
| SendMessage | 팀 모드 | 인터페이스 합의, 질의응답, 실시간 조율 |
| TaskCreate | 팀 모드 | 병렬 작업 분배 (Phase 2, 3) |
| 파일 기반 (`_workspace/`) | 전체 | 모든 지속적 산출물 |
| 반환값 | 서브 모드 | Phase 4 QA 결과 메인으로 수집 |

**파일명 컨벤션:**
- `_workspace/adr/{NNNN}-{topic}.md`
- `_workspace/schema/{name}.v{N}.json`
- `_workspace/guide/{feature}.md`
- `_workspace/qa/report-{YYYY-MM-DD}.md`
- `_workspace/qa/failures/{id}.json`
- `_workspace/metrics/calls.jsonl`

`_workspace/`는 보존(사후 감사용), 최종 산출물만 프로젝트 루트 또는 지정 경로에 출력.

## 에러 핸들링

- 에이전트 실행 실패: 1회 재시도. 재실패 시 해당 산출물 없이 진행, 최종 리포트에 누락 명시.
- Gemini CLI 버전 불일치: gemini-integrator가 경고, 사용자에게 `gemini --version >= 0.28.0` 업그레이드 권고.
- context7 MCP 문서 조회 실패: 기존 지식으로 진행하되 "최신 검증 필요" 태그, ADR에 기록.
- 팀원 간 의견 충돌: harness-architect가 중재, 결정 사유를 ADR에 기록.
- QA 루프 3회 실패: 사용자에게 전체 로그 대신 **핵심 3줄 요약** + 선택지(재시도/수용/수동개입) 제공.

## 팀 크기

5명 — 중규모 작업 기준에 적합. Phase별 활성 팀원은 2~3명으로 유지(design-team 2, impl-team 3). 조율 오버헤드 최소화.

## 테스트 시나리오

### 시나리오 1 (정상 흐름, 초기 실행)
- 입력: "Gemini-Harness 초기 버전을 구현해줘. PRD는 CLAUDE.md 참조"
- 기대: Phase 0 → 초기 실행 판정 → Phase 1~4 순차 → `harness_runtime.py`, `.agents/` 템플릿, QA 리포트 생성, 80% 결함 감소 측정치 기록.

### 시나리오 2 (부분 재실행)
- 입력: "workflow.json 스키마에 retry_limit 필드를 추가해줘"
- 기대: Phase 0 → 부분 재실행 판정 → harness-architect + meta-skill-designer만 재호출 → langgraph-developer에게 파서 영향 알림 → 필요 시 파서 수정 → QA 재실행(스키마 관련 테스트만).

### 시나리오 3 (QA 실패 → 수정 루프)
- 입력: 시나리오 1 실행 중 `meta_linter.py`가 필수 필드 누락을 감지하지 못함
- 기대: QA 리포트에 재현 스텝 명시 → meta-skill-designer 서브로 호출, 린터 수정 → Phase 4 재실행 → 통과 후 완료.

## 참고

- 프로젝트 개요·레이아웃: `CLAUDE.md`
- 에이전트 정의: `.claude/agents/`
- **포트 원본 스펙 (필독)**: `harness-port-spec` 스킬
- **핵심 아키텍처**: `langgraph-patterns` 스킬 (Manager+Worker+Registry, 6 패턴, 버전 호환)
- 지원 스킬: `gemini-cli-integration`(런타임), `gemini-cli-extension-packaging`(설치·배포), `meta-agent-templates`, `self-critique-verification`
- 원본 캐시: `~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/skills/harness/`

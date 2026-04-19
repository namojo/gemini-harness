# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트: Gemini-Harness

**revfactory/harness v1.2.0의 Gemini + LangGraph 포트**. 원본은 Claude Code 플러그인으로 도메인 한 문장을 에이전트 팀 + 스킬로 변환하는 L3 Meta-Factory. 본 프로젝트는 구조·출력·철학을 보존한 채 런타임만 교체:
- 모델: Claude opus → `gemini-3.1-pro-preview`
- 오케스트레이션: `TeamCreate`/`SendMessage`/`TaskCreate` → LangGraph StateGraph + 공유 State
- 인터페이스: Claude Code → Gemini CLI v0.28.0+
- 출력 디렉토리: `.claude/agents/` → `.agents/` (Gemini CLI 규약)

구조적 self-critique 루프로 Opus 4.7급 정밀도를 목표로 한다.

**포트 원본 위치 (필독):**
- 로컬: `~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/skills/harness/`
- 원격: https://github.com/revfactory/harness

**보존할 핵심:** Phase 0~7 워크플로우, 6 아키텍처 패턴(Pipeline · Fan-out/Fan-in · Expert Pool · Producer-Reviewer · Supervisor · Hierarchical Delegation), CLAUDE.md 포인터 등록 관행, 변경 이력 테이블, 재실행 지원. 상세는 `harness-port-spec` 스킬.

**산출물 레이아웃 (런타임 기준):**
- `harness_runtime.py` — LangGraph StateGraph 동적 빌더
- `.agents/{name}/SYSTEM_PROMPT.md` — 런타임이 생성하는 전문가 페르소나
- `.agents/skills/{name}/SKILL.md` — 런타임이 동적 생성하는 스킬 (Python/Bash)
- `workflow.json` — 에이전트 협업 그래프 정의
- `.gemini/context.md` — 에이전트 간 실시간 공유 메모리
- `_workspace/` — 모든 중간 산출물(ADR, 스키마, QA 리포트, 메트릭)

**Tech Stack:** Python, LangGraph, Gemini CLI (v0.28.0+), gemini-3.1-pro-preview, MCP

**레이어 구분 (혼동 금지):**

| 레이어 | 경로 | 누가 만드나 |
|------|------|-----------|
| 빌드 팀 (본 하네스) | `.claude/agents/`, `.claude/skills/` | Claude Code가 본 레포를 개발할 때 사용 |
| 런타임 생성물 | `.agents/`, `.agents/skills/` | Gemini-Harness 런타임이 실행 중 생성 |

## 하네스: Gemini-Harness Build Team

**목표:** 본 프로젝트의 설계·구현·통합·검증을 전문가 팀으로 수행.

**트리거:** 이 프로젝트의 구현·수정·확장 요청(LangGraph 런타임 작성, 메타 에이전트 설계, Gemini CLI 통합, workflow.json 스키마 변경, self-critique 루프 구현, 통합 QA, 부분 재실행 등) 시 `gemini-harness-builder` 스킬을 사용하라. 단순 개념 질문은 직접 응답 가능.

**변경 이력:**
| 날짜 | 변경 내용 | 대상 | 사유 |
|------|----------|------|------|
| 2026-04-19 | 초기 구성 (5 에이전트 + 5 스킬) | 전체 | PRD 기반 신규 구축 |
| 2026-04-19 | 포트 대상 명시 + `harness-port-spec` 스킬 추가 | CLAUDE.md, 오케스트레이터, harness-architect, 신규 스킬 | revfactory/harness v1.2.0을 포팅 대상으로 확정 |
| 2026-04-19 | 아키텍처 전환: Manager+Worker+Registry(Swarm-style) + 설치형 패키지 + LangGraph 버전 호환 전략 | langgraph-patterns 재작성, harness-port-spec·meta-agent-templates 업데이트, harness-architect·langgraph-developer·gemini-integrator 수정, 신규 `gemini-cli-extension-packaging` 스킬 | 정적 NODE_FACTORY는 런타임 동적 에이전트(메타 에이전트) 요구 미충족. "모든 특징 구현" + "설치형 패키지" 요구 반영 |
| 2026-04-19 | Phase 1-4 실행 완료 (초기 포트 구현) | `_workspace/` 16 파일(ADR 5 + port-mapping + schemas 3 + guides 2 + examples 6 + linter_spec + QA 리포트 3), `src/gemini_harness/` 4,820 LOC, `tests/` 2,694 LOC 139 pass, `pyproject.toml`, `extension/manifest.json`, README/CHANGELOG/LICENSE | design-team(Phase 1-2) + impl-team(Phase 3) + harness-qa(Phase 4) 실행. 0 blocker / 0 major / 3 minor. SHIP-READY 판정 |
| 2026-04-19 | QA minor 3건 수정 | `meta/linter.py`·`tests/unit/test_linter.py`(MI-1 체크명 `no_placeholder_only`로 align), `runtime/worker.py`+ADR 0003 addendum(MI-2 composite phase 책임), `extension/manifest.json`(MI-3 일본어 진화·실행 트리거 추가) | QA 리포트 MI-1/MI-2/MI-3 반영. 139 tests 유지 |
| 2026-04-19 | run_audit + run_verify 구현 (1-a/1-b 단계) | `runtime/_audit.py`(264 LOC) · `runtime/_verify.py`(232 LOC) · `runtime/harness_runtime.py` 재내보내기, `tests/unit/test_audit.py`(10 tests) · `tests/unit/test_verify.py`(11 tests) | 원본 Phase 0(현황 감사) + Phase 6(검증) 기능을 LLM 없는 순수 Python으로 구현. MCP 도구 `harness.audit`/`harness.verify`가 실제 동작. 160 tests pass. run_build/run_harness/run_evolve는 LLM 필요 — 후속 단계 |
| 2026-04-19 | run_build 구현 (Stage 2) + Live Gemini 검증 | `runtime/_build.py` · `runtime/_prompts.py` · `integrations/gemini_client.py`(GOOGLE_API_KEY 허용) · `cli.py`·`mcp_server.py`(.env autoload) · `pyproject.toml`(+python-dotenv), `tests/unit/test_build.py`(9 tests, mock) · `tests/_smoke_live_build.py`(수동) | 메타-architect Gemini 호출로 도메인 한 문장 → 6 패턴 중 선택 → 팀·스킬·workflow.json·CLAUDE.md 포인터 생성. 린터 실패 시 retry 2회. Live 검증: "Next.js 웹앱 아키텍처" 입력 → fan_out_fan_in 자동 선택, 5 에이전트(FE/BE/DB/Infra/Integrator), 43.8s, 1045 input / 2963 output tokens. 169 tests pass. 원본 Phase 1-5 포트 동작 실증 |
| 2026-04-19 | run_harness 구현 (Stage 3-a) + **Full E2E 실증** | `runtime/_run.py`(LangGraph stream + checkpoint + context.md), `tests/unit/test_run.py`(8 tests, scripted gemini), `tests/_smoke_live_full.py`(build→run 통합). 프롬프트에 샌드박스 경로 지시 추가 | 생성된 workflow.json을 StateGraph로 실행. **Full E2E Live**: 한 문장 도메인("블로그 작성자+편집자 팀") → run_build 59.5s / producer_reviewer 자동 선택 → run_harness 67.4s / 12 스텝 / producer→reviewer→producer 루프 / 0 에러 / Gemini가 실제 블로그 포스트 작성 및 `_workspace/blog-writer/*.md` 저장. **177 tests pass**. PRD Acceptance #1~#2 충족 실증 |
| 2026-04-19 | run_evolve 구현 (Stage 3-b) | `runtime/_evolve.py`, `tests/unit/test_evolve.py`(10 tests) | Phase 7 — 피드백 기반 점진적 하네스 수정. Gemini가 현 workflow.json + 에이전트 본문을 읽고 최소 변경(change kinds: agent_update/add, skill_update/add, routing_config, workflow_field) 제안. unified diff 생성, dry_run 지원, 린터 재검증 후 적용, CLAUDE.md 변경 이력 자동 append, `.gemini/context.md` evolve 이벤트 기록. **187 tests pass**. 5개 MCP 핸들러(audit/build/verify/evolve/run) 전부 구현 완료 |
| 2026-04-19 | tool_executor 실제 연결 (stub → MCP/CLI 디스패처) | `runtime/_run.py`(_make_tool_executor), `tests/unit/test_tool_executor_wiring.py`(6 tests) | run_harness의 tool_executor 노드가 더 이상 stub가 아님. `mcp:<server>/<tool>` → `mcp_adapter.call_mcp_tool` 경유, `cli:<skill>` → `cli_bridge.invoke_cli_skill` 경유로 실제 외부 도구 실행. `routing_config.tool_executor.allowed_tools` 화이트리스트 적용, `mcp_servers` 서버 레지스트리 지원. PRD R4(Gemini CLI 내장 스킬 통합) 기초 충족. **193 tests pass** |
| 2026-04-19 | Gemini CLI 익스텐션 매니페스트를 실제 스펙으로 교체 | `gemini-extension.json`(신규, project root) · `GEMINI.md`(컨텍스트·트리거 발화 매핑) · `extension/manifest.json`(제거) · `pyproject.toml`(force-include + sdist 경로 수정, 근거 없는 `gemini.extensions` entry-point 제거) · `runtime/_verify.py`·`tests/unit/test_verify.py`(trigger check 재작성) | 기존 매니페스트는 가정 기반 추측이었음. context7로 실제 Gemini CLI v0.36+ 스키마(`mcpServers`, `contextFileName`) 확인 후 올바른 포맷으로 재작성. 트리거는 locale 배열이 아니라 GEMINI.md 프로즈 기반 — 검증기도 이 방식으로 변경. `gemini extensions install .` 명령이 정상 작동 |
| 2026-04-19 | 커스텀 슬래시 명령 5개 추가 | `commands/harness/{build,audit,verify,evolve,run}.toml` · `pyproject.toml`(sdist include) | Gemini CLI는 skill 개념이 없어 자연어 트리거(GEMINI.md) + 커스텀 명령(TOML) 두 경로가 표준. 각 명령은 `{{args}}`로 사용자 입력을 받아 해당 MCP 도구(`mcp_harness_harness_*`) 호출 지시 프롬프트로 렌더. `/harness:build "도메인"` 같은 명시적 호출 가능. 설치 후 `/commands reload`로 즉시 활성화 |
| 2026-04-19 | MCP 서버 기동 명령을 `python3 -m`으로 변경 | `gemini-extension.json`(command/args 수정) · `README.md`(트러블슈팅 안내) | `pip install -e .`이 스크립트를 `~/Library/Python/3.12/bin/`에 설치하는데 이 경로가 PATH에 없어 `gemini-harness-mcp`로는 disconnected 발생. `python3 -m gemini_harness.mcp_server`는 Python이 PATH에 있고 패키지가 site-packages에 있으면 항상 작동 — 이식성 개선. 수동 `gemini mcp add`는 중복이므로 `gemini mcp remove gemini-harness`로 정리 권장 |

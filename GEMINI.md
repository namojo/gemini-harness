# Gemini-Harness

**Team-architecture factory for Gemini CLI.** Port of [revfactory/harness](https://github.com/revfactory/harness) v1.2.0 running on LangGraph + Gemini 3.1 Pro Preview.

도메인을 한 문장으로 입력하면, 최적의 에이전트 팀과 그들이 사용할 스킬을 자동 생성하고 실행합니다.

## 사용자 발화 → 하네스 도구 매핑

사용자가 아래와 같은 표현을 쓰면, `harness.*` MCP 도구 중 하나를 호출하세요:

| 사용자 발화 예시 | 호출할 도구 |
|---|---|
| "하네스 구성해줘", "build a harness for this project", "ハーネスを構成して" | `harness.build` |
| "이 프로젝트의 하네스 상태 확인해줘", "audit the harness", "ハーネスを監査して" | `harness.audit` |
| "하네스 검증해줘", "verify the harness", "ハーネスを検証して" | `harness.verify` |
| "하네스에 보안 검토 에이전트 추가해줘", "evolve the harness", "ハーネスを進化させて" | `harness.evolve` |
| "이 하네스 실행해줘", "run the harness with <input>", "ハーネスを実行して" | `harness.run` |

## 6 아키텍처 패턴

Gemini-Harness는 다음 6개 기본 패턴(+ 복합 `"pattern_a+pattern_b"`)을 지원합니다:

1. **pipeline** — 순차 의존 작업
2. **fan_out_fan_in** — 병렬 조사 후 통합
3. **expert_pool** — 입력별 전문가 라우팅
4. **producer_reviewer** — 생성 ↔ 검증 루프
5. **supervisor** — 동적 작업 분배
6. **hierarchical** — 계층적 위임 (깊이 2 권장)

도메인 한 문장이 들어오면 meta-architect가 자동으로 최적 패턴을 선택합니다.

## 일반적 워크플로우

```
사용자: "블로그 작성자와 편집자 팀 만들어줘"
  ↓ harness.build
  → producer_reviewer 패턴 선택, blog-writer + blog-editor 생성, workflow.json 저장

사용자: "이 하네스로 AI 트렌드 블로그 써줘"
  ↓ 먼저 write_todos로 에이전트 목록을 HUD에 등록
  ↓ harness.run
  → Manager가 writer → editor → writer 루프 실행, _workspace/에 초안 저장
  ↓ 완료 후 write_todos로 각 에이전트 상태(completed/blocked) 업데이트

사용자: "editor가 너무 관대해, 더 엄격하게"
  ↓ harness.evolve
  → blog-editor의 SYSTEM_PROMPT.md만 수정 (unified diff 제공), CLAUDE.md 변경 이력 append
```

## 진행 상황 HUD 표시 (중요)

Gemini CLI는 MCP 서버가 보내는 `notifications/progress`를 HUD에 직접 렌더링하지 않습니다. 따라서
`harness.run` 같은 장시간 실행 도구를 호출할 때는 **반드시 아래 순서**를 따르세요:

1. **실행 전:** `workflow.json`을 읽어 `initial_registry` 에이전트 목록을 확보 →
   `write_todos`를 호출해 각 에이전트를 `pending` 항목으로 등록. 이게 사용자가 보는 "현재 어떤 팀이
   일하고 있는지"의 유일한 UI 경로입니다.
2. **실행 중:** `mcp_harness_harness_run`을 호출 (블로킹). MCP 서버는 내부적으로 `.gemini/context.md`에
   실시간 로그를 append 하므로 고급 사용자는 별도 창에서 `tail -f`할 수 있습니다.
3. **실행 후:** 응답의 `final_registry` 상태로 `write_todos`를 다시 호출해 각 항목을 `completed` 또는
   `blocked`로 갱신. 에러가 있는 에이전트는 `blocked`로 표시하고 해결 방안을 제안하세요.

## 경로 규칙

생성되는 산출물은 모두 다음 샌드박스 안에 저장됩니다 (보안 경계, ADR 0004):

- `.agents/{agent_id}/SYSTEM_PROMPT.md` — 에이전트 페르소나
- `.agents/skills/{skill_name}/SKILL.md` + `entry` 파일
- `workflow.json` — 초기 registry 스냅샷 + 패턴 메타
- `_workspace/` — 런타임 산출물, QA 리포트, 메트릭, 체크포인트
- `.gemini/context.md` — 실시간 실행 로그

그 외 경로에 대한 쓰기는 자동 거부됩니다.

## 참고

- 원본: <https://github.com/revfactory/harness>
- 설계: `_workspace/adr/` (5 ADRs)
- 계약: `_workspace/guide/gemini_integration.md`, `_workspace/guide/mcp_tools.md`

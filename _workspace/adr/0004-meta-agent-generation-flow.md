---
id: 0004
title: 메타 에이전트 생성 흐름 (create_agents → 린터 → registry append → 디스크 쓰기)
status: accepted
date: 2026-04-19
sources:
  - ~/.claude/plugins/cache/harness-marketplace/harness/1.2.0/skills/harness/SKILL.md
  - .claude/skills/meta-agent-templates/SKILL.md
  - .claude/skills/langgraph-patterns/SKILL.md
  - _workspace/adr/0001-architecture-manager-worker-registry.md
---

## Context

"에이전트가 에이전트를 만든다"는 원본 harness의 핵심 가치이자 포트에서 가장 깨지기 쉬운 부분이다. Gemini는 자유 서술로 SYSTEM_PROMPT.md / SKILL.md / registry 엔트리를 생성할 수 있지만, **무검증으로 채택하면** (a) 필수 필드 누락으로 런타임 None 참조, (b) 악성 쉘/eval 인젝션, (c) 중복 id, (d) 존재하지 않는 `entry:` 파일 참조 등 드리프트가 즉시 발생한다. 포트는 이 경로에 강제 게이트가 필요하다.

또한 Worker 응답은 단일 JSON 객체에 `create_agents`·`create_skills`·`send_messages`·`artifacts`·`status_update`가 혼재할 수 있으므로 처리 순서가 결정적이어야 다음 Manager 사이클의 View가 재현 가능하다.

## Decision

**Worker의 메타 생성 처리 순서를 고정하고, 모든 단계에서 `meta_linter`를 게이트로 세운다.**

```
Worker 1회 호출 결과(JSON) → 파싱 → 순서 고정 처리:
  (1) meta_linter.validate_response(parsed)             # 스키마 + 금지 패턴
      실패 시: registry 변경 금지, 생성자 inbox에 실패 사유 Message append,
              retry_count += 1, Worker 반환(State update만, disk write 없음)
  (2) create_agents 처리
      a. 각 엔트리를 AgentMetadata로 materialize (created_by=current_agent_id, created_at=now)
      b. meta_linter.validate_agent(meta) 개별 검증
      c. id 충돌 검사 (state.registry + 이번 배치 내부)
      d. 통과한 엔트리만 .agents/{name}/SYSTEM_PROMPT.md atomic write (tempfile → rename)
      e. State update의 registry 필드에 append (append_unique reducer)
  (3) create_skills 처리 (entry 파일 실재 검증 포함)
  (4) send_messages → inbox reducer (merge_inboxes)
  (5) artifacts → _workspace/에 저장, state["artifacts"]에 경로 기록
  (6) self inbox를 {} 로 덮어쓰기 (재처리 방지)
  (7) history에 event 한 줄 append
```

- **린터는 순수 함수**. LLM 호출 없음. JSON Schema + 정규식 + 파일 시스템 존재 확인만.
- **금지 패턴 목록(인젝션 방지)**: `eval`, `exec`, `compile`, `os.system`, `subprocess(..., shell=True)`, `popen`, `rm -rf /`, `rm -rf ~`, `curl … | sh`, `wget … | bash`, base64-encoded payload(>200자 연속), backtick in frontmatter values, path traversal(`..`) in system_prompt_path. 상세 목록은 `meta-agent-templates` 스킬이 단일 소스.
- **경로 샌드박스**: 모든 disk write는 `.agents/`, `.agents/skills/`, `_workspace/` 3개 루트 이내로만 허용. Worker는 resolve된 절대경로가 allowed roots 하위인지 `pathlib` 검사.
- **재시도 정책**: 린터 실패 시 생성자 inbox에 `{kind: "result", from_id: "linter", content: {failures: [...]}}` 삽입 후 `retry_count += 1`. `retry_count ≥ retry_limit`이면 Manager가 escalate(종료 또는 사용자 알림).
- **원자성**: disk write는 tempfile → `os.replace`. State 업데이트는 단일 reducer 적용으로 완료. 부분 실패 시 파일과 State 불일치 없음.
- **재현성**: `created_at`은 run_id에 포함된 기준 시각을 사용하여 같은 thread 재개 시 동일 타임스탬프 보장 (체크포인터 replay 안정).

## Consequences

### Positive

- **품질 하한선 확보**: 린터 통과한 산출물만 registry에 존재. 사용자가 보는 `.agents/`는 항상 유효.
- **보안 감사 가능**: 금지 패턴 체크가 코드 한 곳. 추가 규칙 도입이 쉬움.
- **Worker 단위 테스트 용이**: Gemini 응답을 mock으로 넣고 처리 순서·State 업데이트만 검증.
- **Self-critique 연결**: 린터 실패 피드백이 생성자 inbox로 되돌아가므로 producer_reviewer 루프와 자연스럽게 결합.

### Negative / Risks

- **Gemini가 린터를 "속이는" 시도**: 금지 패턴을 우회하려 유니코드 치환·주석 인코딩 시도 가능. 린터는 정규 문자 정규화 + AST 기반 Python 검사(Skill `entry`가 python이면 `ast.parse` 후 호출 이름 화이트리스트)로 대응. 완벽 방어 불가 — 의심 시 사용자 승인 fallback.
- **disk write 실패 처리**: 디스크 풀·권한 오류 시 State만 업데이트되면 불일치. → Worker는 "disk write 성공 확인 후 State update" 순서를 반드시 지킴. 실패 시 해당 엔트리는 append하지 않음.
- **retry_count는 전역 카운터라 한 에이전트의 폭주가 다른 에이전트 retry를 소진** → v2에서 per-agent retry로 확장 가능. v1에서는 전역으로 두고 `retry_limit=3` 기본.
- **체크포인터 복원 시 중복 파일 쓰기 가능성**: replay가 disk write를 두 번 시도할 수 있음. → Worker는 write 전 파일 존재 + content hash 비교, 동일하면 skip.

## Alternatives Considered

| 대안 | 기각 사유 |
|------|---------|
| **린터 없이 "런타임이 파싱 실패하면 거부"** | 파싱은 되지만 의미적으로 깨진 산출물(빈 role, 존재 없는 skill 참조) 차단 불가 |
| **린터를 LLM으로 구현** | 비용·비결정성. 런타임 게이트는 순수 함수여야 재현성 보장 |
| **create_agents / create_skills를 별도 Worker 호출로 분리** | 호출 수 증가, State 트랜잭션 원자성 약화. 한 응답에서 일괄 처리가 안전 |
| **샌드박스 없이 임의 경로 허용** | path traversal·호스트 오염. 명백한 거부 |
| **실패 응답을 버리고 생성자 재실행** | 정보 손실. 실패 사유를 inbox로 돌려주어야 producer가 정조준 수정 가능 |

## Upstream Alignment

- 원본 Phase 3·4의 "에이전트/스킬 정의 파일 생성"을 보존 (파일 형식만 `.md` → `.agents/{name}/SYSTEM_PROMPT.md`로 위치 조정, 원본 UX 유지).
- 원본 Phase 4-3의 "Why를 설명하라" 원칙은 `meta_linter`가 본문에서 `## 핵심 역할` / `## 자가 검증` 필수 섹션 검사로 강제 (원본은 명시 검사 없이 에이전트 품질에 의존했음 — 포트에서 강화).
- 원본 Phase 6-1 구조 검증(frontmatter·entry 존재 등)을 런타임 시점으로 앞당겨 적용. 원본에서는 사후 검증이었지만, 메타 에이전트가 많아질수록 사후 수정 비용이 크므로 생성 직후 게이트로 이동.
- 원본에 명시 없던 금지 패턴 목록은 포트의 추가 규칙. `harness-port-spec` "포팅 시 금지 사항"의 "출력 포맷 임의 변경 금지"는 **규칙 추가만 허용**이므로 원칙 위배 없음.

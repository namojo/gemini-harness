---
id: 0005
title: LangGraph 버전 호환 정책 (runtime/compat.py 단일 지점, pyproject 핀, CI 매트릭스)
status: accepted
date: 2026-04-19
sources:
  - .claude/skills/langgraph-patterns/SKILL.md
  - .claude/skills/harness-port-spec/SKILL.md
  - _workspace/adr/0001-architecture-manager-worker-registry.md
  - _workspace/adr/0003-six-pattern-routing-strategy.md
---

## Context

LangGraph는 빠르게 진화 중이며(예: Send API·Command 시그니처·체크포인터 모듈 구조 변경 이력), 포트의 핵심 경로(Manager 라우팅, Worker dispatch, Send 기반 fan-out, SqliteSaver)가 모두 LangGraph public API에 밀착돼 있다. 업그레이드 시점에 애플리케이션 코드 전반에 수정이 퍼지면 회귀 리스크가 크고, 반대로 너무 오래 고정하면 보안 패치·성능 개선을 놓친다. 포팅 실행 전 호환 정책을 확정해 구현자 전체가 따르게 해야 한다.

## Decision

**LangGraph 접점을 `runtime/compat.py` 단일 모듈로 격리하고, 버전은 마이너 허용·메이저 핀으로 관리한다.**

1. **단일 import 규칙**: `StateGraph`, `START`, `END`, `Command`, `Send`, `SqliteSaver`, reducer 헬퍼 등 LangGraph 원시형은 **오직 `runtime/compat.py`에서만 import**. 애플리케이션 코드는 `from runtime.compat import ...`로만 접근. `ruff` custom rule 또는 간단 grep CI 체크로 위반 차단.
2. **pyproject 핀**: `langgraph>=X.Y,<X+1` 형태(마이너 허용, 메이저 핀). 실제 `X.Y`는 구현 시점에 **context7 MCP로 최신 안정 버전 조회** 후 결정하고 그 결정만 별도 로그로 남긴다. 커뮤니티 체크포인터·SDK도 각자 핀.
3. **CI 버전 매트릭스**: 최신 패치 + 직전 마이너 2개 × Python 3.11/3.12 매트릭스로 `pytest` 실행. 실패 시 릴리스 차단.
4. **feature flag**: 신규 API(예: Send 인자 개선, `Command.update` 타입 확장)는 `runtime/compat.py`에서 `packaging.version.Version` 비교 또는 `hasattr` 체크로 분기, fallback 경로 유지. 애플리케이션 코드는 compat 어댑터만 호출.
5. **Private 모듈 금지**: `langgraph._internal`, undocumented 경로 import는 `ruff`/`grep` CI 위반으로 취급. 예외는 ADR 번복을 통해서만.
6. **Deprecation watch**: 분기마다 LangGraph CHANGELOG를 harness-qa가 스캔, deprecation 항목은 이슈로 등록하고 3개월 내 대응. context7 MCP로 자동 fetch 고려 (Task #7 이후).
7. **업그레이드 워크플로우**: (a) compat.py에서 우선 시도 → (b) 어댑터 테스트 통과 → (c) CI 매트릭스 통과 → (d) 릴리스. 애플리케이션 코드 변경 최소화가 목표. 변경 필요하면 별도 ADR로 기록.

## Consequences

### Positive

- **업그레이드 폭발 반경 축소**: 새 버전에서 깨지더라도 수정 지점이 `compat.py` 1곳.
- **여러 버전 지원이 선택지로 존재**: feature flag로 최소 N-2 minor 지원. 기업 환경에서 LangGraph 특정 버전 고정 고객도 대응.
- **포트 코드의 가독성**: 애플리케이션 코드가 LangGraph 세부를 신경 쓰지 않음. 테스트·리팩토링 쉬워짐.
- **보안 패치 채택 빠름**: 메이저만 핀이므로 마이너 패치는 자동 유입.

### Negative / Risks

- **어댑터 레이어 유지 비용**: compat.py가 복잡해질 수 있음. 함수 수·분기가 20개 넘으면 분할 고려.
- **feature flag 체크 오버헤드**: 매 호출마다 `Version` 비교는 과함 → 모듈 로드 시 1회 해결 후 선택된 구현을 바인딩.
- **context7 MCP 의존**: 최신 버전 확인을 context7에 의존하면 오프라인 개발 시 불편. 로컬 `.versions.lock` 파일로 보완.
- **메이저 bump 시 대규모 작업**: 메이저 경계에서는 여전히 큰 작업이 필요. 그러나 어댑터 덕에 "큰 작업이 어디인지" 미리 알 수 있음.

## Alternatives Considered

| 대안 | 기각 사유 |
|------|---------|
| **LangGraph 고정 버전 핀(정확히 한 버전)** | 보안 패치 놓침. 기업 환경 대응 불가 |
| **LangGraph 접점 다 풀어놓기(직접 import)** | 업그레이드 시 전 코드 스캔·수정. 유지보수 부담 폭발 |
| **LangGraph 포크 유지** | 원본 이점 상실. 보안·커뮤니티 버그픽스 재포트 비용 막대 |
| **MCP 서버로 LangGraph를 완전 추상화** | 과설계. 로컬 파이썬 라이브러리를 RPC로 감쌀 필요 없음 |

## Upstream Alignment

- 원본 harness는 Claude Code 런타임 의존이라 이 문제를 겪지 않았다. 포트 고유의 추가 ADR이며, 원본 UX에 영향 없음.
- `langgraph-patterns` 스킬 "버전 호환 정책" 섹션과 일치. 본 ADR은 스킬 내용을 강제 규범으로 승격.
- `harness-port-spec`의 "Deprecation-safe" 원칙을 CI로 강제하는 실행 계획 역할. 스킬이 원칙을, ADR이 실행을 담당한다는 분업 유지.

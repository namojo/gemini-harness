---
name: self-critique-verification
description: Self-critique 루프의 설계·구현·정량 측정. LangGraph conditional edge 기반 구조적 루프, retry counter, diff 평가, A/B 실험으로 결함 감소율 측정. PRD의 80% 결함 감소 acceptance criteria 검증, self-correction 루프 설계, near-miss 트리거 테스트 시 필수.
---

# Self-Critique Verification

Gemini-Harness의 핵심 차별점은 **구조적 self-critique 루프**다. 이 스킬은 그 루프의 설계·구현·측정 방법론을 정의한다.

## 원칙: 프롬프트가 아닌 구조

**나쁜 자가 교정:** "코드를 열심히 리뷰하고 버그를 찾아라" 프롬프트 지시만.
**좋은 자가 교정:** LangGraph 그래프에서 `generate → test → route` 사이클을 **반드시 거치게** 하고, test 실패 시 fix 노드로 자동 분기.

이유: 프롬프트는 모델이 무시할 수 있지만, 그래프 토폴로지는 회피 불가능.

## 루프 템플릿

```
entry → generate → test_runner ─PASS─→ END
                              │
                              └─FAIL─→ fix_agent → generate (retry++)
                                                 │
                                                 └─(retry >= limit)─→ escalate
```

LangGraph 구현 상세는 `langgraph-patterns` 스킬의 "Self-Critique 조건부 루프" 참조.

## 핵심 구성 요소

### 1. Test Runner 노드

실패를 **구체적으로** 반환해야 fix가 가능하다. "test failed" 한 줄은 쓸모없다.

```python
def test_runner_node(state: HarnessState) -> dict:
    result = run_tests(state["artifacts"])
    return {
        "test_passed": result.success,
        "test_output": result.output,
        "failed_assertions": [
            {"file": f.file, "line": f.line, "expected": f.expected, "actual": f.actual, "message": f.message}
            for f in result.failures
        ],
        "coverage": result.coverage,
    }
```

### 2. Fix Agent 노드

fix 프롬프트에 포함해야 할 것 (하나라도 빠지면 품질 저하):
- 원본 사용자 요구사항 (잊으면 "테스트만 통과하는 쓰레기" 생성)
- 이전 시도의 산출물
- 실패 assertion 목록 (구조화)
- 이전 루프의 시도·실패 이력 (같은 실수 반복 방지)

### 3. Retry Counter

```python
class HarnessState(TypedDict):
    retry_count: int
    retry_limit: int
    retry_history: Annotated[list[dict], add]  # 각 시도의 test_output, diff 요약
```

router에서 `retry_count >= retry_limit` 체크 → escalate.

### 4. Diff 기반 정체 감지 (선택, 권장)

```python
def improved(prev: dict, curr: dict) -> bool:
    return len(curr["failed_assertions"]) < len(prev["failed_assertions"])

def critique_router(state: HarnessState) -> str:
    if state["retry_count"] >= state["retry_limit"]:
        return "escalate"
    if state["test_passed"]:
        return "END"
    # 개선 없이 2회 연속 → 루프 무의미
    history = state["retry_history"]
    if len(history) >= 2 and not improved(history[-2], history[-1]):
        return "escalate"
    return "fix_agent"
```

## 정량 측정 (Acceptance Criteria: 80% 결함 감소)

### A/B 실험 설계

같은 입력 세트를 두 모드로 실행:

| 모드 | 그래프 구조 | 비교 기준점 |
|------|-----------|-----------|
| **A (baseline)** | `generate → END` | self-critique 없음 |
| **B (treatment)** | `generate → test → [fix → generate]* → END` | self-critique 활성 |

**지표:**
- `defects` — 생성 산출물의 결함 수 (자동 판정 — 아래 섹션)
- `latency_s` — end-to-end 지연
- `tokens_in` / `tokens_out` — 누적 토큰 (`_workspace/metrics/calls.jsonl`에서 집계)

### 결함 자동 판정

수동 카운트는 느리고 편향된다. 자동 체크:

| 산출물 유형 | 판정 도구 |
|----------|---------|
| Python 코드 | `ruff check` + `mypy` + `pytest`의 실패 수 |
| workflow.json | `meta_linter.py` 실패 수 |
| SYSTEM_PROMPT.md / SKILL.md | `meta_linter.py` 실패 수 |
| 설계 문서 (Markdown) | 링크 유효성, 필수 섹션 존재, 스펠체크 |

### 리포트 형식

`_workspace/qa/metrics/self_critique.csv`:

```csv
run_id,mode,input_id,defects,latency_s,tokens_in,tokens_out,loops
run_001,A,next-js-arch,12,45,23000,5200,0
run_001,B,next-js-arch,2,180,89000,18400,2
run_001,A,fastapi-backend,8,38,19000,4800,0
run_001,B,fastapi-backend,1,155,76000,16200,2
...
```

### 입력 세트

PRD Acceptance Criteria 기반 5~10개 (다양한 도메인):

- "복잡한 Next.js 프로젝트 아키텍처를 짜줘"
- "FastAPI + PostgreSQL 백엔드를 설계해줘"
- "React Native 앱의 상태 관리 전략을 제안해줘"
- "실시간 채팅 서버의 Redis 스키마를 설계해줘"
- "데이터 파이프라인 Airflow DAG를 설계해줘"

각 입력 × 모드 × **3회 반복** (분산 확인). 단일 샘플은 Gemini stochastic 때문에 신뢰 불가.

### 통과 판정

```python
def acceptance_passed(df: pd.DataFrame) -> bool:
    a = df[df["mode"] == "A"]["defects"].mean()
    b = df[df["mode"] == "B"]["defects"].mean()
    if a == 0:
        return True  # baseline 결함 없음 (드문 경우)
    reduction = (a - b) / a
    return reduction >= 0.8
```

## 루프 디자인 안티패턴

| 안티패턴 | 문제 | 대안 |
|---------|------|------|
| Fix가 원본 요구사항을 잊음 | 테스트 통과만 목표 → 쓰레기 코드 | 매 fix 호출에 원본 spec 포함 |
| Test가 실패 사유 불명확 | Fix가 추측으로 수정 → 악순환 | `failed_assertions`에 구조화 리턴 |
| retry 상한 없음 | 무한 루프, 비용 폭발 | `retry_count` + conditional edge |
| 개선 감지 없음 | 같은 실수 반복 | diff 평가, 정체 시 escalate |
| Self-critique 교훈 미보존 | run 간 학습 없음 | `_workspace/qa/lessons.md`에 요약 누적 |
| A/B 측정 없이 "좋아진 것 같다" | 진짜 개선 여부 불명 | 자동 판정 도구 + 리포트 |

## Near-Miss 트리거 테스트 (오케스트레이터 스킬용)

`gemini-harness-builder`의 description이 의도한 상황에만 트리거되는지 검증.

### Should-trigger 쿼리 (8~10개 예시)
- "Gemini-Harness 런타임 구현해줘"
- "workflow.json 스키마에 retry_limit 추가"
- "self-critique 루프 다시 검증해봐"
- "meta_linter.py 규칙 보완해줘"
- "harness-qa가 발견한 버그 수정해줘"
- "이전 QA 결과 기반으로 langgraph-developer만 재호출"
- "Gemini CLI 통합 부분 업데이트해줘"
- "MCP adapter 재실행해서 통합 테스트"

### Should-NOT-trigger 쿼리 (near-miss)
- "LangGraph 일반 개념 설명해줘" → 개념 질문, 본 프로젝트 구현 아님
- "다른 프로젝트의 Gemini API 호출 예시 보여줘" → 다른 프로젝트
- "agent-research 스킬로 LangGraph 트렌드 조사" → 리서치 스킬이 적절
- "이 .agents/ 디렉토리 내용 그냥 읽어줘" → 단순 파일 읽기

경계가 모호한 near-miss가 좋은 테스트 케이스다. "피보나치 구현" 같은 명백 무관 쿼리는 테스트 가치 낮음.

## Escalate 처리

3회 실패 또는 정체 감지 시:
1. `_workspace/qa/escalations/{run_id}.md`에 사유·이력 기록 (입력, 각 시도의 test_output diff, 최종 상태)
2. 사용자에게 **전체 로그가 아니라 핵심 3줄 요약** 제공
3. 사용자 선택지:
   - (a) 다른 입력으로 재시도
   - (b) 최선 시도 산출물을 수용
   - (c) 수동 개입 (에이전트 정의·스킬 수정 후 재실행)

## 왜 구조적 루프가 필요한가

단일 Gemini 호출은 확률적이다. 1M 컨텍스트와 강력한 추론도 **검증 없이는** 신뢰할 수 없다.
- **단일 호출**: 결함 $d$ 개 (평균)
- **Self-critique 1회**: $d \cdot p$ 개 ($p$ = 검출되지 않을 확률, 보통 0.3~0.5)
- **Self-critique 2회**: $d \cdot p^2$ 개

PRD의 80% 감소 목표는 $p \approx 0.45$ 기준 루프 1회로 달성 가능하나, 실측으로 검증 필수. A/B 결과가 목표 미달이면 Test Runner 또는 Fix Agent를 튜닝.

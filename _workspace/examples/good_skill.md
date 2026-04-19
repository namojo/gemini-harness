<!--
LINT: GOOD. 통과하는 체크:
- sk.description_length: 108자 (50~500 범위)
- sk.runtime_valid: runtime=python
- sk.entry_file_exists: entry가 scripts/main.py를 가리키고 해당 파일이 실제로 번들에 존재한다고 가정
- no_eval_exec, no_shell_injection_risk, no_placeholder_only

왜 좋은가: description이 pushy — 하는 일(웹 검색 + 크롤링 + 요약)과 트리거 상황('공식 자료 조사',
'리서치', 'researcher-* 에이전트')을 모두 명시하여 트리거 누락을 방지한다. runtime과 entry가 쌍으로
맞춰져 있고 본문에 '목적/사용/실행/검증' 4개 섹션이 갖춰져 있어 Gemini가 스킬을 호출할 때 필요한
맥락을 Progressive Disclosure로 얻을 수 있다.
-->
---
name: web-research
version: 1.0
description: 주어진 주제에 대해 공식 채널 웹 검색·크롤링·요약을 수행한다. '공식 자료 조사', '리서치', 'researcher-* 에이전트'가 호출되면 반드시 이 스킬을 사용할 것.
runtime: python
entry: scripts/main.py
inputs:
  - "topic: 검색 주제 문자열"
  - "depth: shallow|deep (기본 shallow)"
outputs:
  - "_workspace/research/{agent_id}.md 파일 경로"
created_at: 2026-04-19T02:00:00Z
created_by: meta-skill-designer
---

# Web Research

## 목적

공식 1차 자료를 수집하여 마크다운 불릿 리스트로 반환한다. 커뮤니티 의견·2차 해석은 범위 밖.

## 사용

- 호출 주체: researcher-a, researcher-b 등 조사 전문가
- 입력: `{topic, depth}` JSON
- 출력: `_workspace/research/{agent_id}.md` — 불릿 + URL + 날짜

## 실행

`python scripts/main.py --topic "..." --depth shallow --out _workspace/research/researcher-a.md`

## 검증

출력 파일이 다음을 만족하는지 entry 스크립트가 self-check:
- 최소 3개 불릿
- 모든 불릿에 `http` URL 포함
- 날짜가 역순 정렬
실패 시 exit code 1 + stderr에 실패 사유.

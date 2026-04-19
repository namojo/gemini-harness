<!--
LINT: BAD. 실패하는 체크:
- sp.has_self_critique_section: '## 자가 검증' 섹션 누락 → 린터가 거부
- sp.has_version: frontmatter에 version 필드 누락 → 린터가 거부

추가로 약한 패턴 (경고):
- role 섹션이 핵심 역할이 아닌 'Role'로 영문화되어 있어 한국어 본문 패턴 검사와 어긋남
  (원본 하네스는 한국어 섹션을 채택; 포트도 동일)
- model이 opus로 되어 있어 sp.has_model(gemini-prefix) 실패

재생성 피드백 템플릿:
"frontmatter.version 누락, model이 gemini-로 시작해야 함,
 본문에 '## 자가 검증' 섹션 추가 필요. 재생성 요청."
-->
---
name: researcher-a
model: opus
tools: [file-manager]
---

# Researcher A

## Role

공식 채널 조사.

## 작업 원칙

- 공식 자료만 쓴다
- URL 적는다

## 에러 핸들링

실패하면 Manager에게 알림.

<!--
LINT: BAD. 실패하는 체크:
- entry 필드 누락 → skill.schema.json의 required=['name','version','description','runtime','entry'] 위반
- sk.entry_file_exists: entry 없음 → 자동 실패
- sk.description_length: 31자로 50 미달
- sk.runtime_valid: runtime=shell (python|bash만 허용)
- no_placeholder_only: 본문이 "TODO: implement"만 있고 전체 500자 미만

왜 나쁜가: runtime이 지정되었지만 entry 파일이 없으므로 런타임이 이 스킬을 호출하려 해도
실행할 대상이 없다. description이 짧고 추상적이어서 Claude/Gemini가 언제 트리거할지 판단 불가.
본문이 placeholder만 있어 스킬의 실제 동작·검증·입출력을 알 수 없다.

재생성 피드백 템플릿:
"entry 필드 누락, runtime은 python|bash만 허용, description을 50자 이상으로 pushy하게
 재작성, 본문에 목적/사용/실행/검증 4개 섹션 추가. 재생성 요청."
-->
---
name: web-research
version: 1.0
description: 웹을 검색하는 스킬.
runtime: shell
---

# Web Research

TODO: implement

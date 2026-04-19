---
name: code-linter
version: "1.0"
description: "대상 코드 파일에 대해 정적 분석 및 린팅을 수행하여 잠재적인 오류와 컨벤션 위반 사항을 추출합니다. 리뷰 전 기초 데이터 확보 시 호출합니다."
runtime: python
entry: scripts/lint.py
---

# Title
Code Linter and Static Analyzer

## 목적
대상 소스 코드 파일에 대해 정적 분석(Static Analysis) 및 린팅(Linting)을 수행하여, 문법 오류, 안티 패턴, 보안 취약점 가능성, 성능 저하 유발 코드 등을 자동으로 추출합니다. 리뷰어 에이전트들이 코드를 분석하기 전 기초 데이터를 제공하는 역할을 합니다.

## 사용
- 보안, 성능, 가독성 리뷰어가 코드의 전반적인 상태를 빠르게 파악하고자 할 때 호출합니다.
- 특정 파일이나 디렉토리 경로를 인자로 전달하여 실행합니다.

## 실행
```bash
python scripts/lint.py --target <file_or_directory_path> --format json
```
- `scripts/lint.py`는 내부적으로 대상 언어에 맞는 린터(예: pylint, eslint, flake8 등)를 래핑하여 실행합니다.
- 실행 결과는 JSON 형태로 반환되어 에이전트가 쉽게 파싱하고 분석할 수 있도록 합니다.

## 검증
- [ ] 지정된 경로의 파일이 실제로 존재하는지 검사하는 로직이 포함되어 있는가?
- [ ] 린터 실행 중 발생하는 표준 에러(stderr)를 적절히 포착하여 에이전트에게 실패 원인을 전달하는가?
- [ ] 반환되는 JSON 포맷이 일관된 스키마(예: line, column, severity, message)를 유지하는가?

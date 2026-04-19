---
name: gemini-cli-extension-packaging
description: Gemini-Harness를 Gemini CLI v0.28.0+ 사용자 환경에 설치 가능한 Python 패키지로 배포하는 방법. pyproject.toml 구조, 익스텐션 매니페스트, MCP 서버와 슬래시/트리거 명령 병행 제공, LangGraph 버전 호환 핀 전략, 진입점, 사용자 설치·업그레이드·제거 흐름, CI 배포. 배포 아티팩트 작성, 설치 경험 설계, LangGraph 업데이트 대비 의존성 관리 시 반드시 참조.
---

# Gemini CLI Extension Packaging

Gemini-Harness가 `pip install gemini-harness` 한 번으로 Gemini CLI 사용자 환경에 녹아들고, LangGraph 업데이트에도 안정적으로 동작하도록 패키징한다.

## 최신 API 확인

구현 전 context7 MCP로 다음 라이브러리의 최신 문서를 조회하라:
- `langgraph` — 호환 범위 결정에 필요
- `google-genai` — API 안정성
- `mcp` — 서버 SDK 규약
- Gemini CLI 익스텐션 API — 이 문서의 매니페스트 예시는 참고용, 실제 스키마는 CLI 공식 문서 기준

## 배포 모델: 병행 제공

Gemini CLI에서 하네스를 호출하는 두 채널을 **모두** 제공한다:

| 채널 | 설치 | 호출 | 역할 |
|------|------|------|------|
| **MCP 서버** | `gemini mcp add gemini-harness --command gemini-harness-mcp` | LLM이 MCP 도구(`harness.build` 등) 호출 | 표준·범용. 다른 MCP 클라이언트에서도 사용 |
| **Gemini CLI 익스텐션** | `gemini extensions install gemini-harness` | "하네스 구성해줘" 자연어 트리거 → 익스텐션이 Python 진입점 실행 | 원본 하네스 UX 재현 |

두 채널 모두 **동일한 Python 진입점**을 호출한다. 한쪽만 유지보수하면 다른 쪽도 자동 반영.

## Python 패키지 구조

```
gemini-harness/
├── pyproject.toml
├── README.md
├── LICENSE                              — Apache-2.0 (원본과 호환)
├── CHANGELOG.md
├── src/gemini_harness/
│   ├── __init__.py                      — __version__
│   ├── cli.py                           — `gemini-harness` 진입점
│   ├── mcp_server.py                    — `gemini-harness-mcp` 진입점 (MCP stdio)
│   ├── runtime/
│   │   ├── compat.py                    — LangGraph 어댑터 (버전 호환 유일 지점)
│   │   ├── harness_runtime.py           — build_harness_graph
│   │   ├── state.py                     — HarnessState, reducers
│   │   ├── manager.py                   — Manager 라우팅 (6 패턴)
│   │   ├── worker.py                    — Worker dispatcher
│   │   └── patterns/                    — 패턴별 라우팅 로직
│   │       ├── pipeline.py
│   │       ├── fan_out_fan_in.py
│   │       ├── expert_pool.py
│   │       ├── producer_reviewer.py
│   │       ├── supervisor.py
│   │       └── hierarchical.py
│   ├── integrations/
│   │   ├── gemini_client.py
│   │   ├── cli_bridge.py
│   │   └── mcp_adapter.py
│   ├── meta/
│   │   ├── templates/                   — SYSTEM_PROMPT.md, SKILL.md 템플릿
│   │   ├── schemas/                     — JSON Schema
│   │   └── linter.py                    — meta_linter
│   └── assets/                          — 번들 (6 패턴 설명, 예시 workflow.json)
├── extension/
│   └── manifest.json                    — Gemini CLI 익스텐션 매니페스트
└── tests/
    ├── unit/
    ├── integration/
    └── compat/                          — LangGraph 버전 매트릭스 테스트
```

## pyproject.toml 핵심

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "gemini-harness"
version = "0.1.0"
description = "Port of revfactory/harness to Gemini CLI + LangGraph"
readme = "README.md"
license = "Apache-2.0"
requires-python = ">=3.11"

dependencies = [
  "langgraph>={pinned_min},<{next_major}",    # 예: ">=0.2.30,<1.0" — context7로 확인 후 결정
  "google-genai>={pinned_min}",
  "mcp>={pinned_min}",
  "tenacity>=9.0",
  "pydantic>=2.6",
  "pyyaml>=6.0",
  "packaging>=24.0",
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "mypy", "ruff"]
# 버전 매트릭스 테스트용
compat-prev = ["langgraph=={prev_minor}"]
compat-next = ["langgraph=={next_minor_prerelease}"]

[project.scripts]
gemini-harness = "gemini_harness.cli:main"
gemini-harness-mcp = "gemini_harness.mcp_server:main"

[project.entry-points."gemini.extensions"]
harness = "gemini_harness.cli:extension_entry"

[tool.hatch.build.targets.wheel]
packages = ["src/gemini_harness"]
# 익스텐션 매니페스트와 assets를 함께 번들
include = ["extension/**", "src/gemini_harness/assets/**"]
```

## LangGraph 버전 호환 전략

1. **핀 범위**: 현재 안정 버전부터 다음 메이저까지. 매 릴리스 전 context7로 호환성 재확인.
2. **어댑터 유일 지점**: `runtime/compat.py`에서만 `from langgraph import ...` 허용. 코드 리뷰 시 다른 파일의 langgraph 직접 import는 거부.
3. **런타임 버전 감지 + feature flag**:
   ```python
   # compat.py
   from packaging.version import Version
   import langgraph
   LG_VERSION = Version(langgraph.__version__)
   HAS_SEND_V2 = LG_VERSION >= Version("0.3.0")   # 가상 예시
   ```
4. **CI 매트릭스**: `compat-prev` / pinned / `compat-next`의 3단계 pytest job. `compat-next` 실패는 경고로 다루되 이슈 트래킹.
5. **SemVer 의무**: 패키지 버전은 워크플로우 스키마나 사용자 CLI 명령이 깨지면 메이저 bump. LangGraph 내부 변경만으로 깨지는 건 패치.

## 익스텐션 매니페스트

`extension/manifest.json` — **실제 스키마는 Gemini CLI 공식 문서 확인**. 참고 골격:

```json
{
  "name": "gemini-harness",
  "version": "0.1.0",
  "description": "Team-architecture factory for Gemini CLI (port of revfactory/harness)",
  "homepage": "https://github.com/<org>/gemini-harness",
  "license": "Apache-2.0",
  "min_gemini_cli_version": "0.28.0",
  "entry": {
    "command": "gemini-harness",
    "args": ["--cli-ext"]
  },
  "triggers": [
    {"phrase": "하네스 구성해줘", "locale": "ko"},
    {"phrase": "ハーネスを構成して", "locale": "ja"},
    {"phrase": "build a harness for this project", "locale": "en"}
  ],
  "mcp_server": {
    "command": "gemini-harness-mcp",
    "description": "Exposes harness.* tools via stdio MCP"
  }
}
```

매니페스트 포맷이 CLI 버전마다 달라지면, 패키지 버전의 **마이너 bump**로 대응 (어댑터처럼 matrix로 관리).

## MCP 도구 면면

`mcp_server.py`에서 노출할 도구 (원본 Phase에 대응):

| 도구 | 입력 | 출력 | 원본 Phase |
|------|------|------|---------|
| `harness.audit` | `{project_path}` | 기존 하네스 감사 리포트 (drift 목록) | 0 |
| `harness.build` | `{project_path, domain_description}` | 에이전트·스킬 생성, workflow.json, CLAUDE.md 업데이트 | 1~5 |
| `harness.verify` | `{project_path}` | 구조·트리거·드라이런 + 원본 동등성 리포트 | 6 |
| `harness.evolve` | `{project_path, feedback}` | 변경 이력 갱신, 에이전트/스킬 부분 수정 | 7 |
| `harness.run` | `{project_path, user_input}` | 생성된 하네스를 실제로 실행(생성된 오케스트레이터가 해당 도메인 작업 수행) | — |

각 도구의 JSON Schema는 `docs/mcp_tools.md`에 명시 (gemini-integrator가 유지보수).

## 진입점 구현 골격

`cli.py`:
```python
import argparse, sys
from .runtime.harness_runtime import build_harness_graph

def main(argv=None):
    parser = argparse.ArgumentParser(prog="gemini-harness")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_audit = sub.add_parser("audit"); p_audit.add_argument("--project", default=".")
    p_build = sub.add_parser("build"); p_build.add_argument("--project", default=".")
    p_build.add_argument("--domain", required=True)
    # verify, evolve, run ...
    args = parser.parse_args(argv)
    return {
        "audit": lambda a: _audit(a.project),
        "build": lambda a: _build(a.project, a.domain),
        # ...
    }[args.cmd](args)

def extension_entry(context):
    # Gemini CLI 익스텐션 훅: context에 user_utterance, project_path 등 포함
    return _dispatch_from_utterance(context)
```

`mcp_server.py`:
```python
from mcp.server import Server
server = Server("gemini-harness")

@server.call_tool("harness.build")
async def harness_build(project_path: str, domain_description: str):
    from .runtime.harness_runtime import run_build
    return await run_build(project_path, domain_description)

def main():
    server.run_stdio()
```

## 사용자 설치·업그레이드·제거 흐름

```bash
# 설치 (옵션 1: MCP만)
pip install gemini-harness
gemini mcp add gemini-harness --command gemini-harness-mcp

# 설치 (옵션 2: 익스텐션까지)
gemini extensions install gemini-harness
# 또는 로컬 개발
pip install -e .
gemini extensions install ./extension

# 검증
gemini-harness --version
gemini extensions list | grep harness
gemini mcp list | grep gemini-harness

# 업그레이드
pip install -U gemini-harness
# 익스텐션은 자동 감지 또는:
gemini extensions upgrade gemini-harness

# 제거
gemini extensions uninstall gemini-harness
gemini mcp remove gemini-harness
pip uninstall gemini-harness
```

**업그레이드 안전성:**
- 사용자 프로젝트의 `.agents/`는 절대 덮어쓰지 않음
- 생성 산출물은 `.agents/skills/{name}/` 안의 `entry:` 파일이 패키지 업그레이드에 영향받지 않도록 프로젝트 로컬에만 저장
- workflow.json 스키마 v1→v2 시에는 `meta-skill-designer`가 설계한 마이그레이터가 사용자 승인 후 변환 — 무언의 auto-migrate 금지

## CI 배포 파이프라인

1. PR 테스트: ruff + mypy + pytest (unit + integration + compat matrix)
2. main 머지: `hatch build`, TestPyPI 업로드
3. 태그 푸시 (v0.1.0): PyPI 공식 업로드, GitHub Release 생성, 익스텐션 번들 업로드

`compat matrix` 실패 시 머지 차단. LangGraph가 새 마이너 릴리스를 내면 자동으로 `compat-next` job이 감지.

## 버저닝 정책

| bump | 사유 |
|------|------|
| Major (1.x.x) | workflow.json 스키마 breaking, CLI 명령 rename/제거, MCP 도구 시그니처 변경 |
| Minor (0.x.0) | 새 패턴 지원, 신규 MCP 도구, 새 `harness.*` 명령, 매니페스트 포맷 업데이트 |
| Patch (0.0.x) | 버그 수정, 프롬프트 튜닝, LangGraph 내부 변경 대응 |

원본 revfactory/harness가 메이저 bump하면 포트도 평가 후 대응 — 원본 동등성 유지가 최우선.

## 테스트

- **패키징**: `pip install dist/*.whl` → `gemini-harness --version`, `gemini-harness-mcp --help` 실행 확인
- **익스텐션 설치**: 임시 Gemini CLI 환경에서 `gemini extensions install ./`
- **MCP**: MCP inspector 또는 별도 MCP 클라이언트에서 `gemini-harness-mcp` 등록 → `harness.audit` 호출 → 유효 응답
- **업그레이드 보존**: 기존 `.agents/` 있는 프로젝트에서 패키지 업그레이드 후 `.agents/`가 그대로인지 확인
- **호환 매트릭스**: LangGraph prev/pin/next 세 버전에서 `build_harness_graph` + e2e 시나리오 통과

## 참고

- LangGraph 어댑터·패턴: `langgraph-patterns` 스킬
- MCP 도구 스키마: `gemini-cli-integration` 스킬
- 메타 템플릿/린터: `meta-agent-templates` 스킬
- 포트 원본: `harness-port-spec` 스킬

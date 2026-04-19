---
name: gemini-cli-integration
description: Gemini CLI(v0.28.0+) 내장 스킬과 gemini-3.1-pro-preview Python SDK, MCP Server를 LangGraph 노드에서 호출하는 패턴. subprocess 브리지, API 래퍼, MCP 어댑터, 타임아웃·재시도·command injection 차단. Gemini 관련 통합 코드 작성·디버깅 시 반드시 참조.
---

# Gemini CLI / API Integration

Gemini-Harness 런타임이 외부(Gemini CLI + API + MCP)와 만나는 경계면의 구현 패턴.

## 버전 확인

런타임 시작 시 Gemini CLI 버전 검사:

```python
import subprocess
from packaging.version import Version

def check_gemini_cli(min_version: str = "0.28.0") -> str:
    result = subprocess.run(
        ["gemini", "--version"],
        capture_output=True, text=True, timeout=5,
    )
    version_str = result.stdout.strip().lstrip("v")
    if Version(version_str) < Version(min_version):
        raise RuntimeError(
            f"Gemini CLI >= {min_version} required, got {version_str}. "
            f"Upgrade: npm install -g @google/gemini-cli@latest"
        )
    return version_str
```

## 3채널 분리

| 채널 | 용도 | 호출 방식 | 파일 |
|------|------|---------|------|
| Gemini CLI | 내장 스킬(file-manager, google-search), 익스텐션 | subprocess | `cli_bridge.py` |
| Gemini API | 생성·추론·1M 컨텍스트 활용 | `google.genai` SDK | `gemini_client.py` |
| MCP Server | 외부 도구(DB, 내부 API) | `mcp` SDK (stdio/http) | `mcp_adapter.py` |

**원칙:** 에러 도메인이 다르므로 파일·클래스를 분리. 섞으면 재시도 정책, 타임아웃, 인증 로직이 뒤엉킨다.

## Python SDK 호출 패턴

최신 API는 context7 MCP로 `google-genai` 조회. 일반 골격:

```python
from google import genai
from tenacity import retry, stop_after_attempt, wait_exponential

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(min=2, max=30),
    reraise=True,
)
def call_gemini(
    prompt: str,
    system: str,
    context: str = "",
    temperature: float = 0.7,
) -> str:
    contents = [context, prompt] if context else [prompt]
    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=contents,
        config=genai.types.GenerateContentConfig(
            system_instruction=system,
            temperature=temperature,
        ),
    )
    _record_metrics(response, node=...)
    return response.text
```

**주의:** `reraise=True` — tenacity 기본은 래핑된 예외를 던지는데, 원본 예외가 필요함.

## CLI 브리지 (내장 스킬 호출)

```python
import shlex

def invoke_cli_skill(
    skill: str,
    args: list[str],
    cwd: str = ".",
    timeout: int = 60,
) -> str:
    # args는 반드시 리스트 형태, shell=False
    cmd = ["gemini", "skill", skill, *args]
    result = subprocess.run(
        cmd,
        capture_output=True, text=True, cwd=cwd, timeout=timeout,
        check=False,  # 수동 처리
    )
    if result.returncode != 0:
        raise GeminiCliError(
            f"skill={skill} exit={result.returncode}: {result.stderr}"
        )
    return result.stdout
```

**command injection 차단:**
- `shell=True` 금지
- 사용자 입력을 args로 받을 땐 `shlex.quote` 또는 그대로 리스트 요소로만

**금지 예시:**
```python
# NEVER
subprocess.run(f"gemini skill {skill} {user_input}", shell=True)
# user_input = "foo; rm -rf /" → 재앙
```

## MCP 통합

MCP Server는 (a) stdio 자식 프로세스 또는 (b) HTTP 엔드포인트로 제공.

```python
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

async def call_mcp_tool(
    server_cmd: list[str],
    tool: str,
    args: dict,
    timeout: float = 30.0,
):
    params = StdioServerParameters(command=server_cmd[0], args=server_cmd[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return result.content
```

**계약 우선:** MCP 도구의 입출력 JSON Schema를 먼저 `_workspace/guide/mcp_tools.md`에 문서화하고 구현. 스키마 없이 구현하면 노드 쪽에서 응답 파싱이 브로큰 필드로 실패.

## 1M 컨텍스트 활용

**좋은 사용:**
- `.gemini/context.md` 전체 + workflow.json + 관련 소스 파일을 **구조화된 헤더**로 구획하여 한 번의 호출로 종합 판단
- 명시적 참조 포함: "see `## workflow` section above for current agents"

**나쁜 사용:**
- 모든 것을 긴 문자열로 concatenate하여 1M을 채움 → 노이즈가 품질 저하
- 동일 정보 반복 (Gemini가 중복을 중요 신호로 오해할 수 있음)

**원칙:** 긴 컨텍스트는 "모델이 관련 정보를 정확히 찾을 수 있을 때"만 이득. 탐색성을 높이는 포맷팅이 핵심.

## 메트릭 수집

모든 호출마다 `_workspace/metrics/calls.jsonl`에 append:

```python
def _record_metrics(response, node: str, run_id: str):
    record = {
        "ts": datetime.now().isoformat(),
        "model": "gemini-3.1-pro-preview",
        "input_tokens": response.usage_metadata.prompt_token_count,
        "output_tokens": response.usage_metadata.candidates_token_count,
        "latency_ms": ...,
        "node": node,
        "run_id": run_id,
    }
    with open("_workspace/metrics/calls.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
```

self-critique A/B 측정이 이 파일에 의존한다.

## 에러 처리 매트릭스

| 에러 유형 | 행동 | 재시도 여부 |
|---------|------|-----------|
| 429 rate limit | tenacity 지수 백오프 | 예 (3회) |
| 500 server error | tenacity | 예 (3회) |
| 인증 실패 (401/403) | 즉시 실패, `GEMINI_API_KEY` 확인 요청 | 아니오 |
| 타임아웃 | 재시도 | 예 (1~2회) |
| CLI 버전 불일치 | 런타임 시작 시 실패 + 업그레이드 가이드 | 아니오 |
| MCP 연결 실패 | 명시적 에러 (silent fallback 금지) | 설정 확인 후 사용자 판단 |
| 유해 콘텐츠 필터 차단 | State의 `errors`에 기록, 상위 노드로 전파 | 아니오 (프롬프트 수정 필요) |

## 테스트

- **API 래퍼**: `responses` 또는 SDK mock으로 429/500/타임아웃 시나리오 강제
- **CLI 브리지**: 작은 헬로월드 스킬로 실제 subprocess 실행 테스트(CI에서는 mock)
- **MCP 어댑터**: 로컬 MCP 서버(에코 서버)로 프로토콜 왕복 확인

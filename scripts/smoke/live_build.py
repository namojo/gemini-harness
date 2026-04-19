"""Live smoke test for run_build against real Gemini. Not part of pytest suite.

Run manually: `python3 tests/_smoke_live_build.py`
Requires GOOGLE_API_KEY or GEMINI_API_KEY in env or project .env.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent.parent.parent / ".env")
    sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

    from gemini_harness.runtime.harness_runtime import BuildError, run_build

    has_key = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    print(f"[smoke] API key present: {has_key}", flush=True)
    if not has_key:
        print("[smoke] no API key — aborting", flush=True)
        return 2

    model = os.environ.get("LANGCHAIN_HARNESS_MODEL", "gemini-3.1-pro-preview")
    print(f"[smoke] model: {model}", flush=True)

    with tempfile.TemporaryDirectory() as tmp:
        print(f"[smoke] project: {tmp}", flush=True)
        print("[smoke] calling architect…", flush=True)
        t0 = time.monotonic()
        try:
            result = run_build(
                project_path=tmp,
                domain_description=(
                    "Next.js 웹앱의 아키텍처 설계 팀을 구성. "
                    "프론트엔드, 백엔드 API, 데이터베이스, 인프라 4개 관점이 필요하고 "
                    "마지막에 4관점을 통합하는 종합 보고서가 필요."
                ),
                max_agents=5,
            )
        except BuildError as exc:
            elapsed = time.monotonic() - t0
            print(f"[smoke] BUILD_ERROR after {elapsed:.1f}s: {exc}", flush=True)
            return 1

        elapsed = time.monotonic() - t0
        print(f"[smoke] success in {elapsed:.1f}s", flush=True)
        print(f"[smoke] pattern: {result['pattern']}", flush=True)
        print(
            f"[smoke] agents: {[a['id'] for a in result['final_registry']]}",
            flush=True,
        )
        print(f"[smoke] metrics: {result['metrics']}", flush=True)
        print(f"[smoke] written: {len(result['written_files'])} files", flush=True)
        print(f"[smoke] workflow_path: {result['workflow_path']}", flush=True)
        wf = json.loads(Path(result["workflow_path"]).read_text(encoding="utf-8"))
        print(f"[smoke] routing_config: {wf.get('routing_config')}", flush=True)
        print("[smoke] OK", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())

"""Full live E2E: run_build → run_harness with real Gemini.

Run manually: `python3 tests/_smoke_live_full.py`
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

    from gemini_harness.runtime.harness_runtime import (
        BuildError,
        RunError,
        run_build,
        run_harness,
    )

    has_key = bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    print(f"[smoke] API key present: {has_key}", flush=True)
    if not has_key:
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        print(f"[smoke] project: {tmp}", flush=True)

        print("[smoke] STEP 1: run_build …", flush=True)
        t0 = time.monotonic()
        try:
            build = run_build(
                project_path=tmp,
                domain_description=(
                    "짧은 블로그 포스트를 작성할 팀을 구성해줘. "
                    "작성자 한 명과 편집자 한 명으로 이루어진 간단한 생성-검증 루프가 필요."
                ),
                max_agents=3,
            )
        except BuildError as e:
            print(f"[smoke] build FAILED: {e}", flush=True)
            return 1
        print(
            f"[smoke] build OK in {time.monotonic() - t0:.1f}s — "
            f"pattern={build['pattern']} agents={[a['id'] for a in build['final_registry']]}",
            flush=True,
        )

        print("[smoke] STEP 2: run_harness …", flush=True)
        t1 = time.monotonic()
        try:
            result = run_harness(
                project_path=tmp,
                user_input="랭체인 하네스의 Phase 1-5 포팅 성공 사례를 3줄 블로그로 써줘.",
                step_limit=12,
            )
        except RunError as e:
            print(f"[smoke] run FAILED: {e}", flush=True)
            return 1

        elapsed = time.monotonic() - t1
        print(f"[smoke] run OK in {elapsed:.1f}s", flush=True)
        print(f"[smoke] steps: {result['steps']}", flush=True)
        print(f"[smoke] errors: {result['errors']}", flush=True)
        print(f"[smoke] artifacts: {len(result['artifacts'])}", flush=True)
        if result["artifacts"]:
            print(
                f"[smoke] sample artifact paths: {result['artifacts'][:3]}",
                flush=True,
            )
        print(f"[smoke] context.md: {result['context_md_path']}", flush=True)
        # Dump first 30 lines of context.md
        ctx_path = Path(result["context_md_path"])
        if ctx_path.exists():
            print("[smoke] --- context.md head ---", flush=True)
            for line in ctx_path.read_text(encoding="utf-8").splitlines()[:30]:
                print(f"  {line}", flush=True)
        # Show an artifact if any
        if result["artifacts"]:
            first = Path(result["artifacts"][0]).name
            for p in Path(tmp).rglob(first):
                if p.is_file():
                    print(f"[smoke] --- {p} ---", flush=True)
                    content = p.read_text(encoding="utf-8")[:800]
                    for line in content.splitlines()[:20]:
                        print(f"  {line}", flush=True)
                    break
        print("[smoke] OK", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())

# Live smoke scripts

Manual verification scripts that call the real Gemini API. **Not part of
`pytest`** (they cost tokens and take tens of seconds).

Run from the repo root with a `GOOGLE_API_KEY` in `.env` or your shell:

```bash
python3 scripts/smoke/live_build.py   # meta-architect only (harness.build)
python3 scripts/smoke/live_full.py    # build + run end-to-end
```

Expected wall-clock: 40-70 seconds each. Output prefixed with `[smoke]`.

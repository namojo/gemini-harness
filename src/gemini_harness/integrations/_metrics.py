"""Best-effort metrics writer for `_workspace/metrics/calls.jsonl`.

Contract: `_workspace/guide/gemini_integration.md` §7.

Writes must never raise into the call site — a metrics failure logs a
warning and returns.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_METRICS_PATH_ENV = "GEMINI_HARNESS_METRICS_PATH"
_DEFAULT_METRICS_PATH = Path("_workspace/metrics/calls.jsonl")


def _metrics_path() -> Path:
    override = os.environ.get(_METRICS_PATH_ENV)
    return Path(override) if override else _DEFAULT_METRICS_PATH


def record_call(record: dict[str, Any]) -> None:
    """Append one JSON object (one line) to the metrics file.

    `record` must already match the schema in §7.1. Writer only adds `ts`
    if missing. Unknown keys are preserved (caller's responsibility to
    stay schema-valid).
    """
    record = {"ts": datetime.now(timezone.utc).isoformat(), **record}
    try:
        path = _metrics_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        _log.warning("metrics write failed: %s", exc)

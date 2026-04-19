"""Unit tests for `gemini_harness.cli`.

Focus: argparse wiring + extension_entry utterance dispatch. Runtime calls
are stubbed via `_load_runtime_fn` so nothing real runs.
"""

from __future__ import annotations

import sys
from io import StringIO

import pytest


@pytest.fixture
def stub_runtime(monkeypatch):
    """Replace cli._load_runtime_fn with a factory that records dispatches."""
    calls: list[dict] = []

    def _fake_loader(attr):
        def _fake_fn(**kwargs):
            calls.append({"attr": attr, "kwargs": kwargs})
            return {"ok": True, "attr": attr, "kwargs": kwargs}

        return _fake_fn

    from gemini_harness import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_load_runtime_fn", _fake_loader)
    monkeypatch.setattr(cli_mod, "check_gemini_cli", lambda *a, **k: "0.28.5")
    return calls


def test_parser_knows_subcommands(stub_runtime):
    from gemini_harness.cli import main

    # No subcommand yields usage + exit 1.
    rc = main([])
    assert rc == 1


def test_audit_dispatch(stub_runtime, capsys):
    from gemini_harness.cli import main

    rc = main(["audit", "--project", "/abs/p"])
    assert rc == 0
    assert len(stub_runtime) == 1
    assert stub_runtime[0]["attr"] == "run_audit"
    assert stub_runtime[0]["kwargs"]["project_path"] == "/abs/p"
    assert stub_runtime[0]["kwargs"]["include_skills"] is True


def test_build_dispatch(stub_runtime):
    from gemini_harness.cli import main

    rc = main([
        "build",
        "--project",
        "/abs/p",
        "--domain",
        "X " * 30,
        "--max-agents",
        "3",
        "--force",
    ])
    assert rc == 0
    call = stub_runtime[0]
    assert call["attr"] == "run_build"
    assert call["kwargs"]["max_agents"] == 3
    assert call["kwargs"]["force"] is True


def test_verify_returns_non_zero_on_fail(monkeypatch):
    from gemini_harness import cli as cli_mod

    def _fail_loader(attr):
        def _fn(**kw):
            return {"passed": False, "results": []}

        return _fn

    monkeypatch.setattr(cli_mod, "_load_runtime_fn", _fail_loader)
    monkeypatch.setattr(cli_mod, "check_gemini_cli", lambda *a, **k: "0.28.5")

    rc = cli_mod.main(["verify", "--project", "/abs/p"])
    assert rc == 2


def test_version_exits_zero(capsys, monkeypatch):
    from gemini_harness.cli import main
    from gemini_harness import __version__

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_cli_version_gate_failure(monkeypatch):
    from gemini_harness import cli as cli_mod
    from gemini_harness.integrations._errors import GeminiCliVersionError

    def _fail(*a, **k):
        raise GeminiCliVersionError("too old")

    monkeypatch.setattr(cli_mod, "check_gemini_cli", _fail)
    rc = cli_mod.main(["audit", "--project", "/abs/p"])
    assert rc == 3


# -------- extension_entry --------


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("하네스 감사해줘", "audit"),
        ("하네스 구성해줘", "build"),
        ("build a harness for this project", "build"),
        ("please verify the harness", "verify"),
        ("evolve this harness", "evolve"),
        ("run the harness now", "run"),
        ("ハーネスを構成して", "build"),
    ],
)
def test_utterance_dispatch(utterance, expected):
    from gemini_harness.cli import _dispatch_from_utterance

    assert _dispatch_from_utterance(utterance) == expected


def test_extension_entry_build_requires_domain(stub_runtime):
    from gemini_harness.cli import extension_entry

    out = extension_entry(
        {
            "utterance": "하네스 구성해줘",
            "project_path": "/abs/p",
        }
    )
    assert out["error_code"] == "INVALID_INPUT"


def test_extension_entry_audit_roundtrip(stub_runtime):
    from gemini_harness.cli import extension_entry

    out = extension_entry(
        {"utterance": "audit the harness", "project_path": "/abs/p"}
    )
    assert out["ok"] is True
    assert stub_runtime[0]["attr"] == "run_audit"


def test_cli_ext_reads_context_from_stdin(stub_runtime, monkeypatch, capsys):
    import json as _json

    from gemini_harness.cli import main

    payload = _json.dumps({"utterance": "audit", "project_path": "/abs/p"})
    monkeypatch.setattr(sys, "stdin", StringIO(payload))
    rc = main(["--cli-ext"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "run_audit" in captured.out

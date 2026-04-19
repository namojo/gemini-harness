"""Unit tests for `gemini_harness.integrations.cli_bridge`."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _reset_version_cache():
    from gemini_harness.integrations import cli_bridge

    cli_bridge._reset_version_cache()
    yield
    cli_bridge._reset_version_cache()


def _fake_completed(*, stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


# -------- version check --------


def test_check_gemini_cli_ok(monkeypatch):
    from gemini_harness.integrations import cli_bridge

    def fake_run(cmd, **kw):
        assert kw["shell"] is False
        assert cmd == ["gemini", "--version"]
        return _fake_completed(stdout="0.28.5\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert cli_bridge.check_gemini_cli() == "0.28.5"


def test_check_gemini_cli_strips_v_prefix(monkeypatch):
    from gemini_harness.integrations import cli_bridge

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _fake_completed(stdout="v0.29.0\n")
    )
    assert cli_bridge.check_gemini_cli() == "0.29.0"


def test_check_gemini_cli_too_old(monkeypatch):
    from gemini_harness.integrations import cli_bridge
    from gemini_harness.integrations._errors import GeminiCliVersionError

    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _fake_completed(stdout="0.27.9\n")
    )
    with pytest.raises(GeminiCliVersionError) as exc_info:
        cli_bridge.check_gemini_cli()
    assert "0.28.0" in str(exc_info.value)


def test_check_gemini_cli_missing_binary(monkeypatch):
    from gemini_harness.integrations import cli_bridge
    from gemini_harness.integrations._errors import GeminiCliVersionError

    def fake_run(*a, **k):
        raise FileNotFoundError("no gemini")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GeminiCliVersionError):
        cli_bridge.check_gemini_cli()


def test_check_gemini_cli_cached(monkeypatch):
    from gemini_harness.integrations import cli_bridge

    calls = {"n": 0}

    def fake_run(*a, **k):
        calls["n"] += 1
        return _fake_completed(stdout="0.28.1\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cli_bridge.check_gemini_cli()
    cli_bridge.check_gemini_cli()
    assert calls["n"] == 1


# -------- invoke_cli_skill --------


def test_invoke_cli_skill_requires_list_args():
    from gemini_harness.integrations import cli_bridge

    with pytest.raises(TypeError):
        cli_bridge.invoke_cli_skill("my-skill", "rm -rf /")  # type: ignore[arg-type]


def test_invoke_cli_skill_happy_path(monkeypatch, tmp_metrics_path):
    from gemini_harness.integrations import cli_bridge

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["shell"] = kw["shell"]
        captured["env"] = kw.get("env")
        return _fake_completed(stdout="hello\n", returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = cli_bridge.invoke_cli_skill(
        "file-manager",
        ["list", "--path", "/tmp/with space"],
        node="worker",
        run_id="t-cli-1",
    )

    assert result.stdout == "hello\n"
    assert result.returncode == 0
    # User input lands as a discrete list element — not interpolated.
    assert captured["cmd"] == [
        "gemini",
        "skill",
        "file-manager",
        "list",
        "--path",
        "/tmp/with space",
    ]
    assert captured["shell"] is False

    lines = tmp_metrics_path.read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["channel"] == "cli"
    assert rec["skill"] == "file-manager"
    assert rec["outcome"] == "ok"


def test_invoke_cli_skill_nonzero_exit_raises(monkeypatch, tmp_metrics_path):
    from gemini_harness.integrations import cli_bridge
    from gemini_harness.integrations._errors import GeminiCliError

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _fake_completed(stderr="bad args", returncode=2),
    )
    with pytest.raises(GeminiCliError) as exc:
        cli_bridge.invoke_cli_skill("x", [], run_id="t-cli-err")
    assert exc.value.returncode == 2

    rec = json.loads(tmp_metrics_path.read_text().splitlines()[0])
    assert rec["outcome"] == "error"
    assert rec["exit_code"] == 2


def test_invoke_cli_skill_rejects_non_string_arg():
    from gemini_harness.integrations import cli_bridge

    with pytest.raises(TypeError):
        cli_bridge.invoke_cli_skill("x", ["ok", 123])  # type: ignore[list-item]


def test_invoke_cli_skill_rejects_semicolon_in_shell_true(monkeypatch):
    """Sanity: even if a user passes command-injection characters, they are
    preserved as a single list element and never interpreted by a shell."""
    from gemini_harness.integrations import cli_bridge

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["shell"] = kw["shell"]
        return _fake_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    cli_bridge.invoke_cli_skill("x", ["foo; rm -rf /"])
    assert captured["cmd"][-1] == "foo; rm -rf /"
    assert captured["shell"] is False


def test_invoke_cli_skill_timeout(monkeypatch, tmp_metrics_path):
    from gemini_harness.integrations import cli_bridge
    from gemini_harness.integrations._errors import GeminiCliError

    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kw.get("timeout", 60))

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(GeminiCliError):
        cli_bridge.invoke_cli_skill("x", ["y"], timeout=1)


def test_invoke_cli_skill_env_merges_without_mutating_parent(monkeypatch):
    from gemini_harness.integrations import cli_bridge

    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw["env"]
        return _fake_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("PARENT_VAR", "parent-value")

    cli_bridge.invoke_cli_skill("x", ["y"], env={"EXTRA": "added"})
    env = captured["env"]
    assert env["PARENT_VAR"] == "parent-value"
    assert env["EXTRA"] == "added"


def test_invoke_cli_extension_command_shape(monkeypatch):
    from gemini_harness.integrations import cli_bridge

    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _fake_completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    cli_bridge.invoke_cli_extension("gemini-harness", "build", ["--project", "/abs"])
    assert captured["cmd"] == [
        "gemini",
        "extensions",
        "run",
        "gemini-harness",
        "build",
        "--project",
        "/abs",
    ]

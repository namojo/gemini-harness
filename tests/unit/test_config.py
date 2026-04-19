"""Unit tests for gemini_harness.config."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gemini_harness import config


def test_default_model_constant():
    assert config.DEFAULT_MODEL == config.PREFERRED_MODELS[0]
    assert config.DEFAULT_MODEL.startswith("gemini-")


def test_get_model_returns_default_without_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LANGCHAIN_HARNESS_MODEL", raising=False)
    assert config.get_model() == config.DEFAULT_MODEL


def test_get_model_env_wins_over_config(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    config.set_model("gemini-1.5-pro")
    monkeypatch.setenv("LANGCHAIN_HARNESS_MODEL", "gemini-2.0-flash")
    assert config.get_model() == "gemini-2.0-flash"


def test_set_then_get_model_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LANGCHAIN_HARNESS_MODEL", raising=False)
    path = config.set_model("gemini-2.5-pro")
    assert path.exists()
    assert config.get_model() == "gemini-2.5-pro"
    # File has 0600 perms and is valid JSON
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["model"] == "gemini-2.5-pro"


def test_set_model_strips_whitespace(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("LANGCHAIN_HARNESS_MODEL", raising=False)
    config.set_model("  gemini-2.5-flash  ")
    assert config.get_model() == "gemini-2.5-flash"


def test_config_path_under_xdg(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert config.config_path() == tmp_path / "gemini-harness" / "config.json"


def test_list_available_models_without_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    # Without API key, falls back to PREFERRED_MODELS
    result = config.list_available_models()
    assert list(result) == list(config.PREFERRED_MODELS)


def test_load_config_returns_empty_when_missing(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # No file yet
    assert config.load_config() == {}


def test_load_config_tolerates_malformed_json(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = tmp_path / "gemini-harness" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{this is not json", encoding="utf-8")
    # Should not raise, just return empty
    assert config.load_config() == {}

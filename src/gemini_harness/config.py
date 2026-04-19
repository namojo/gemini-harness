"""User configuration for gemini-harness.

Stored at ``$XDG_CONFIG_HOME/gemini-harness/config.json`` (falls back to
``~/.config/gemini-harness/config.json``). Currently tracks:

- ``model``: preferred Gemini model name (e.g. ``gemini-3.1-pro-preview``)

Resolution priority (highest wins):
1. ``LANGCHAIN_HARNESS_MODEL`` environment variable
2. User config file
3. Built-in default (``DEFAULT_MODEL``)

The ``configure`` CLI subcommand lists the models the user's API key can
actually access and writes the selection to the config file.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


# Ordered by preference — first entry is the default we reach for.
PREFERRED_MODELS: tuple[str, ...] = (
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.0-pro",
    "gemini-2.0-flash",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
)

DEFAULT_MODEL: str = PREFERRED_MODELS[0]

ENV_MODEL: str = "LANGCHAIN_HARNESS_MODEL"


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "gemini-harness"


def config_path() -> Path:
    return _config_dir() / "config.json"


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(data: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def get_model() -> str:
    """Resolve the model to use. Env wins, then config, then default."""
    env_value = os.environ.get(ENV_MODEL)
    if env_value:
        return env_value.strip()
    cfg = load_config()
    configured = cfg.get("model")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return DEFAULT_MODEL


def set_model(model: str) -> Path:
    """Persist ``model`` to the config file. Returns the config path."""
    data = load_config()
    data["model"] = model.strip()
    return save_config(data)


def list_available_models(api_key: str | None = None) -> list[str]:
    """Ask Gemini which models this API key can use.

    Returns a sorted list of model names (e.g. ``"gemini-2.5-pro"``). On any
    failure returns the built-in ``PREFERRED_MODELS`` list so the CLI still
    has something to show. Only models whose names start with ``gemini`` and
    that list ``generateContent`` as a supported action are returned.
    """
    try:
        from google import genai
    except ImportError:
        return list(PREFERRED_MODELS)

    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return list(PREFERRED_MODELS)
    try:
        client = genai.Client(api_key=key)
        models = client.models.list()
    except Exception:
        return list(PREFERRED_MODELS)

    names: list[str] = []
    for m in models:
        name = getattr(m, "name", "") or ""
        # API returns "models/gemini-2.5-pro" — strip prefix.
        short = name.split("/")[-1]
        if not short.startswith("gemini"):
            continue
        methods = getattr(m, "supported_actions", None) or getattr(
            m, "supported_generation_methods", None
        ) or []
        if methods and "generateContent" not in methods:
            continue
        names.append(short)
    # De-duplicate, sort by preference list then alphabetical.
    seen: set[str] = set()
    unique: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            unique.append(n)
    preferred_index = {m: i for i, m in enumerate(PREFERRED_MODELS)}
    unique.sort(key=lambda n: (preferred_index.get(n, 999), n))
    return unique or list(PREFERRED_MODELS)


__all__ = [
    "DEFAULT_MODEL",
    "ENV_MODEL",
    "PREFERRED_MODELS",
    "config_path",
    "get_model",
    "list_available_models",
    "load_config",
    "save_config",
    "set_model",
]

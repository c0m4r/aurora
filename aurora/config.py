"""Configuration loading — YAML file + environment variable overrides."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional
import yaml

DEFAULT_PATHS = [
    "./config.yaml",
    "~/.config/aurora/config.yaml",
    "/etc/aurora/config.yaml",
]


class _Obj:
    """Recursive dot-access wrapper around a plain dict."""

    def __init__(self, data: dict):
        for k, v in data.items():
            if isinstance(v, dict):
                setattr(self, k, _Obj(v))
            elif isinstance(v, list):
                setattr(self, k, [_Obj(i) if isinstance(i, dict) else i for i in v])
            else:
                setattr(self, k, v)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

    def __repr__(self) -> str:
        return f"_Obj({self.__dict__!r})"


_ENV_MAP: dict[str, list[str]] = {
    "ANTHROPIC_API_KEY":  ["providers", "anthropic", "api_key"],
    "OPENAI_API_KEY":     ["providers", "openai", "api_key"],
    "GEMINI_API_KEY":     ["providers", "gemini", "api_key"],
    "OPSAGENT_API_KEY":   ["server", "api_key"],
}

_raw: dict = {}
_cfg: Optional[_Obj] = None


def _deep_set(d: dict, path: list[str], value: Any) -> None:
    for part in path[:-1]:
        d = d.setdefault(part, {})
    d[path[-1]] = value


def load(path: Optional[str] = None) -> _Obj:
    global _raw, _cfg

    if path:
        candidate = Path(path).expanduser()
    else:
        candidate = None
        for p in DEFAULT_PATHS:
            pp = Path(p).expanduser()
            if pp.exists():
                candidate = pp
                break

    _raw = {}
    if candidate and candidate.exists():
        with open(candidate) as fh:
            _raw = yaml.safe_load(fh) or {}

    # Env overrides
    for env_key, cfg_path in _ENV_MAP.items():
        val = os.environ.get(env_key)
        if val:
            _deep_set(_raw, cfg_path, val)

    _cfg = _Obj(_raw)
    return _cfg


def get() -> _Obj:
    global _cfg
    if _cfg is None:
        _cfg = load()
    return _cfg


def raw() -> dict:
    return _raw

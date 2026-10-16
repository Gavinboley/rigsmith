"""Choices made in the app, kept between runs.

Two modes, and the difference between them is a promise rather than a
preference:

* **accuracy** - use whatever gives the best answer: Claude through Claude
  Code for research and agents, the network for song lookup and TONE3000.
* **local** - nothing leaves this machine. Agents run on a local model
  (LM Studio, Ollama, llama.cpp), research comes from what is already in the
  workspace, and every call that would touch the internet refuses. The
  HeadRush on the LAN is still reachable - it is the point of the tool - but
  nothing past the router is.

The local promise is enforced in one place, :func:`require_online`, which
every network-facing function calls before it does anything. A mode that
was only a default for a dropdown would leak the first time some new code
path forgot to check it.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import get_settings, require_writable
from .errors import Invalid, Unavailable

MODES = {
    "accuracy": "best results: Claude Code for agents and research, network lookups allowed",
    "local": "fully isolated: a local model for agents, no internet at all",
}


@dataclass
class LLMSettings:
    """Which model answers in the chat and runs the agents."""

    provider: str = "auto"
    """auto, claude-code, anthropic, openai-compatible."""
    base_url: str = "http://127.0.0.1:1234/v1"
    """For openai-compatible: LM Studio 1234, Ollama 11434, llama.cpp 8080."""
    model: str = ""
    """Empty means: ask the server what is loaded and pick the best."""
    api_key_env: str = ""
    """Environment variable holding a key, when the endpoint wants one."""
    claude_model: str = ""
    """For claude-code/anthropic; empty uses Claude Code's own default."""
    temperature: float = 0.3
    max_tool_rounds: int = 24
    thinking: bool = False
    """Ask Qwen-style models to think before answering (slower, sometimes better)."""


@dataclass
class AppSettings:
    mode: str = "accuracy"
    llm: LLMSettings = field(default_factory=LLMSettings)
    local_llm: LLMSettings = field(default_factory=lambda: LLMSettings(provider="openai-compatible"))
    """The model used in local mode - always a local endpoint."""
    audio_backend: str = "sounddevice"
    default_profile: str | None = None
    default_route: str | None = None
    allow_preview_downloads: bool = True
    """Fetch a store's 30-second preview for analysis, in accuracy mode."""
    music_dirs: list[str] = field(default_factory=list)
    """Where your music library lives, for finding a song's file by name.
    Empty means the home folder's Music."""
    onboarding_done: bool = False
    scene_modes: dict = field(default_factory=dict)
    """The numbers this Core uses for a block's state in a scene -
    ``{"on": n, "off": n, "no_change": 0}`` - learned with 'device scenes
    --learn'. Until they are, a write gives scene instructions instead."""

    @property
    def local(self) -> bool:
        return self.mode == "local"

    def active_llm(self) -> LLMSettings:
        return self.local_llm if self.local else self.llm

    def to_dict(self) -> dict:
        data = asdict(self)
        data["modes"] = MODES
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "AppSettings":
        llm = LLMSettings(**{k: v for k, v in (data.get("llm") or {}).items() if k in LLMSettings.__dataclass_fields__})
        local_llm = LLMSettings(
            **{
                **{"provider": "openai-compatible"},
                **{k: v for k, v in (data.get("local_llm") or {}).items() if k in LLMSettings.__dataclass_fields__},
            }
        )
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k not in ("llm", "local_llm")}
        settings = cls(llm=llm, local_llm=local_llm, **known)
        if settings.mode not in MODES:
            settings.mode = "accuracy"
        return settings


_cached: AppSettings | None = None


def _path() -> Path:
    return get_settings().app_settings_path


def load() -> AppSettings:
    """The saved settings, with ``RIGSMITH_MODE`` able to force a mode."""
    global _cached
    path = _path()
    data = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    settings = AppSettings.from_dict(data)
    forced = os.environ.get("RIGSMITH_MODE", "").strip().lower()
    if forced in MODES:
        settings.mode = forced
    _cached = settings
    return settings


def save(settings: AppSettings) -> AppSettings:
    require_writable("save settings")
    if settings.mode not in MODES:
        raise Invalid(f"unknown mode '{settings.mode}'", hint="Modes: " + ", ".join(MODES))
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(settings)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    global _cached
    _cached = settings
    return settings


def update(**changes) -> AppSettings:
    settings = load()
    for key, value in changes.items():
        if key in ("llm", "local_llm") and isinstance(value, dict):
            target = getattr(settings, key)
            for name, item in value.items():
                if name in LLMSettings.__dataclass_fields__:
                    setattr(target, name, item)
        elif key in AppSettings.__dataclass_fields__:
            setattr(settings, key, value)
        else:
            raise Invalid(f"no setting called '{key}'")
    return save(settings)


def mode() -> str:
    return load().mode


def require_online(action: str) -> None:
    """Refuse a call that would leave this machine, in local mode.

    Every function that talks to the internet calls this first. The device on
    the LAN is not the internet and does not call it.
    """
    if load().local:
        raise Unavailable(
            f"refusing to {action}: rigsmith is in local mode, which never reaches the internet",
            hint="Switch to accuracy mode in Settings (or: rigsmith settings mode accuracy) "
            "if you want it to.",
        )


def reset_cache() -> None:
    global _cached
    _cached = None

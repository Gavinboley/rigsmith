"""Settings, workspace layout, and the global read-only guard.

The workspace defaults to ``~/.rigsmith`` and holds everything that must survive
between sessions: device backups, protocol captures, cached research, audio
renders, and credentials. It is deliberately outside the repo so that nothing
device-specific or copyrighted ends up in version control.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_WORKSPACE = Path.home() / ".rigsmith"

#: mDNS hostnames the HeadRush family answers to, most specific first.
KNOWN_HOSTNAMES = (
    "headrushcore.local",
    "headrushprime.local",
    "headrushflexprime.local",
)

#: The Remote Editor URL carries no port, so port 80 is the working
#: assumption. Phase 1 protocol capture confirms or corrects this, and the
#: discovered value is what actually gets used at runtime.
ASSUMED_HTTP_PORT = 80


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Runtime configuration, assembled from environment and defaults."""

    workspace: Path = field(default_factory=lambda: Path(
        os.environ.get("RIGSMITH_WORKSPACE") or DEFAULT_WORKSPACE
    ))

    #: When true every write tool is disabled at startup. This is checked in
    #: one place (``require_writable``) so it cannot be bypassed piecemeal.
    readonly: bool = field(default_factory=lambda: _env_flag("RIGSMITH_READONLY"))

    #: Skip the interactive confirmation before writing to the device. Only
    #: honoured for writes into the scratch slot range.
    assume_yes: bool = field(default_factory=lambda: _env_flag("RIGSMITH_ASSUME_YES"))

    #: Explicit device address, bypassing discovery (``RIGSMITH_DEVICE=192.168.1.x``).
    device_host: str | None = field(
        default_factory=lambda: os.environ.get("RIGSMITH_DEVICE") or None
    )

    tone3000_client_id: str | None = field(
        default_factory=lambda: os.environ.get("TONE3000_CLIENT_ID") or None
    )

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).expanduser()

    # -- workspace layout -------------------------------------------------

    @property
    def backups_dir(self) -> Path:
        return self.workspace / "backups"

    @property
    def captures_dir(self) -> Path:
        """Protocol captures: served bundles, endpoint inventories, traffic logs."""
        return self.workspace / "captures"

    @property
    def firmware_dir(self) -> Path:
        """Downloaded firmware images and their unpacked trees. Never committed."""
        return self.workspace / "firmware"

    @property
    def audio_dir(self) -> Path:
        """DI captures, rendered candidates, isolated stems."""
        return self.workspace / "audio"

    @property
    def research_dir(self) -> Path:
        """Cached tone research and compiled recipes."""
        return self.workspace / "research"

    @property
    def credentials_path(self) -> Path:
        return self.workspace / "credentials.json"

    def ensure_workspace(self) -> Path:
        for path in (
            self.workspace,
            self.backups_dir,
            self.captures_dir,
            self.firmware_dir,
            self.audio_dir,
            self.research_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self.workspace


class ReadOnlyError(RuntimeError):
    """Raised when a write is attempted while rigsmith is in read-only mode."""


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide settings, created on first use."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Replace the process-wide settings. Used by the CLI and by tests."""
    global _settings
    _settings = settings


def require_writable(action: str) -> None:
    """Guard every device or filesystem mutation.

    Args:
        action: Human-readable description of what was about to happen,
            surfaced in the error so the caller knows what was blocked.

    Raises:
        ReadOnlyError: if ``RIGSMITH_READONLY`` is set.
    """
    if get_settings().readonly:
        raise ReadOnlyError(
            f"refusing to {action}: rigsmith is in read-only mode "
            "(unset RIGSMITH_READONLY to allow writes)"
        )

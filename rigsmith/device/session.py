"""Pairing with a HeadRush, and remembering that you did.

The Core does not authenticate a user; it authorises a *session*. You ask for
one, it answers 401 with a four-digit PIN and shows the same PIN on its own
screen, and somebody standing at the device accepts it. From then on the
cookie you were given is paired, and every other cookie in the world is not.

Two consequences worth stating, because both have already cost time:

* The cookie is the credential. A fresh HTTP session - a new client, a new
  process that forgot to load this file - is ``notPaired`` no matter who is
  using it, and every call returns 401 with no explanation of why.
* Accepting the PIN needs a person at the hardware. Nothing here can be made
  unattended, and a pairing call that hangs is usually a device sitting in
  another room asking a question nobody has heard.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.errors import Invalid, NotFound, Unavailable

#: What the device calls its session cookie.
COOKIE_NAME = "sessionID"

#: How long to wait for somebody to walk over and tap accept.
DEFAULT_PAIR_TIMEOUT = 180.0

#: How often to ask whether they have.
POLL_SECONDS = 2.0


@dataclass
class Session:
    """A paired session with one device."""

    host: str
    cookie: str
    base_url: str = ""
    hostname: str | None = None
    server: str | None = None
    firmware: str | None = None
    paired_at: float = 0.0

    @property
    def cookies(self) -> dict[str, str]:
        return {COOKIE_NAME: self.cookie}

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "hostname": self.hostname,
            "base_url": self.base_url,
            "session_cookie": self.cookie,
            "server": self.server,
            "firmware": self.firmware,
            "paired_at": self.paired_at,
            "note": (
                "Pairing is bound to this cookie. A new session is notPaired "
                "until somebody accepts a PIN on the device."
            ),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        cookie = data.get("session_cookie") or data.get("cookie")
        if not cookie:
            raise Invalid("a saved session has no cookie in it")
        return cls(
            host=data.get("host", ""),
            cookie=cookie,
            base_url=data.get("base_url", ""),
            hostname=data.get("hostname"),
            server=data.get("server"),
            firmware=data.get("firmware"),
            paired_at=float(data.get("paired_at") or 0.0),
        )


def session_path(host: str | None = None) -> Path:
    """Where a paired session is kept.

    One file per device, keyed by host, so pairing a second Core does not
    silently evict the first.
    """
    from ..config import get_settings

    settings = get_settings()
    directory = settings.workspace / "sessions"
    safe = (host or "device").replace(":", "_").replace("/", "_")
    return directory / f"{safe}.json"


def save_session(session: Session) -> Path:
    from ..config import require_writable

    require_writable("save the device session")
    path = session_path(session.host)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session.to_dict(), indent=2), encoding="utf-8")
    try:
        # It is a credential. Not a secret worth much - it only works on a
        # LAN, against one device - but there is no reason to leave it
        # world-readable either.
        path.chmod(0o600)
    except OSError:
        pass
    return path


def load_session(host: str | None = None) -> Session | None:
    """The saved session for a host, if there is one."""
    try:
        path = session_path(host)
    except Exception:
        return None
    if not path.is_file():
        return None
    try:
        return Session.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, Invalid):
        return None


def forget_session(host: str | None = None) -> bool:
    path = session_path(host)
    if not path.is_file():
        return False
    path.unlink()
    return True


@dataclass
class PairRequest:
    """A pairing in progress: the PIN to accept, and the cookie it belongs to."""

    pin: str
    cookie: str
    state: str = "pending"

    @property
    def accepted(self) -> bool:
        return self.state in ("paired", "accepted", "ok")


def _cookie_from(response: Any) -> str | None:
    """Pull the session cookie out of a response, header or jar."""
    jar = getattr(response, "cookies", None)
    if jar is not None:
        found = jar.get(COOKIE_NAME)
        if found:
            return found
    header = getattr(response, "headers", {})
    raw = header.get("set-cookie") if hasattr(header, "get") else None
    if raw and COOKIE_NAME in raw:
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME and value:
                return value
    return None


def read_pair_state(payload: Any, response: Any) -> PairRequest | None:
    """Interpret whatever the device said about pairing."""
    if not isinstance(payload, dict):
        return None
    state = str(payload.get("pairState") or payload.get("state") or "").lower()
    pin = payload.get("pairPin") or payload.get("pin") or ""
    cookie = _cookie_from(response) or ""
    if not state and not pin:
        return None
    return PairRequest(pin=str(pin), cookie=cookie, state=state or "pending")

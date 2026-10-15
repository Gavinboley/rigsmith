"""Only this machine's browser, on this app's own pages, may drive it.

The UI binds to loopback, and that is not the same as only being reachable
by its own page. Any site open in the same browser can reach 127.0.0.1:

* a plain HTML form can POST to it cross-site with no preflight - which,
  on a route that takes only path and query parameters, was enough to
  write a rig to the Core;
* a page can open the event websocket and watch everything happening;
* and DNS rebinding - an attacker's own hostname re-pointed at 127.0.0.1 -
  makes every request same-origin, so CORS protects nothing at all.

So every request and websocket handshake is checked here, before any route:
the Host header must name this machine (rebinding sends the attacker's
name), and an Origin, when a browser sends one, must be this very origin;
a state-changing request without one falls back to its Referer. Clients
that are not browsers (the CLI, curl, scripts) send neither and are not
affected.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: Names this machine answers to on loopback.
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})

#: Extra names to accept - the test client calls itself "testserver".
#: Empty in a running app.
EXTRA_HOSTS: set[str] = set()

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _hostname(netloc: str) -> str:
    """``127.0.0.1:8000`` -> ``127.0.0.1``; ``[::1]:8000`` -> ``::1``."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc[1 : netloc.find("]")] if "]" in netloc else netloc
    return netloc.rsplit(":", 1)[0] if netloc.count(":") == 1 else netloc


def check(method: str, headers: dict[str, str], *, hosts: frozenset[str] | None, websocket: bool = False) -> str | None:
    """Why a request is refused, or None. ``hosts`` None accepts any Host."""
    host = headers.get("host", "")
    if hosts is not None and _hostname(host) not in hosts | EXTRA_HOSTS:
        return f"this app only answers requests addressed to this machine, not '{host}'"
    origin = headers.get("origin")
    if origin is not None:
        if origin == "null" or urlsplit(origin).netloc.lower() != host.lower():
            return f"this app only takes requests from its own page, not from '{origin}'"
        return None
    if websocket or method in UNSAFE:
        referer = headers.get("referer")
        if referer is not None and urlsplit(referer).netloc.lower() != host.lower():
            return "this app only takes requests from its own page"
    return None


class LocalOnly:
    """ASGI middleware applying :func:`check` to HTTP and websocket scopes."""

    def __init__(self, app, hosts: frozenset[str] | None = LOOPBACK) -> None:
        self.app = app
        self.hosts = hosts

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        websocket = scope["type"] == "websocket"
        refused = check(scope.get("method", "GET"), headers, hosts=self.hosts, websocket=websocket)
        if refused is None:
            return await self.app(scope, receive, send)
        if websocket:
            # Refuse the handshake: close before accepting.
            await receive()
            return await send({"type": "websocket.close", "code": 4403, "reason": refused[:120]})
        body = ('{"error": "' + refused.replace('"', "'") + '"}').encode("utf-8")
        await send({"type": "http.response.start", "status": 403, "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

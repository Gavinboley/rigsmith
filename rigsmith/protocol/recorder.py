"""Record the Remote Editor's live traffic by proxying it.

Static analysis of the served bundle (:mod:`rigsmith.protocol.capture`)
recovers the API *surface* - which endpoints exist. It cannot tell you which
call corresponds to which action, or what the payloads look like in practice.
For that you have to watch the real editor work.

This is a **reverse** proxy rather than a forward one, which makes it far
easier to use: instead of configuring browser proxy settings and dealing with
certificates, you open the proxy's own address and use the editor normally.
Every request, response, and WebSocket frame is written to a JSONL log,
tagged with a monotonic sequence number so the order of operations survives.

Workflow::

    rigsmith protocol record            # opens http://127.0.0.1:<port>
    # ... in the browser, do one thing: add a block, change a parameter ...
    # Ctrl-C, then read the log - the calls that action produced are the
    # ones between your marks.

Read-only with respect to the device: it forwards exactly what the editor
sends, and nothing else.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import Unavailable

# Imported at module scope, not inside build_app, because FastAPI resolves
# route annotations against module globals - and this file uses
# `from __future__ import annotations`, so they arrive as strings. With the
# imports function-local, every annotation silently degrades to a query
# parameter and the proxy stops proxying.
try:
    import httpx
    from fastapi import FastAPI, Request, WebSocket
    from fastapi.responses import Response
    from starlette.websockets import WebSocketDisconnect

    HAVE_WEB = True
except ImportError:  # pragma: no cover - optional dependency
    HAVE_WEB = False
    httpx = None  # type: ignore[assignment]
    FastAPI = Request = WebSocket = Response = WebSocketDisconnect = None  # type: ignore[assignment]


def _require_web() -> None:
    if not HAVE_WEB:
        raise Unavailable(
            "the traffic recorder needs the web stack",
            hint="Install it with:  pip install 'rigsmith[ui]'",
        )

#: Headers that must not be forwarded verbatim - they describe the hop, not
#: the message, and passing them through breaks keep-alive or content length.
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
    "content-encoding",
}

#: Bodies larger than this are logged truncated - a firmware blob or an audio
#: preview would otherwise swamp the interesting traffic.
MAX_LOGGED_BODY = 64 * 1024


@dataclass
class Exchange:
    """One logged request/response pair or WebSocket frame."""

    seq: int
    at: float
    kind: str
    """``http`` or ``ws``."""
    method: str = ""
    path: str = ""
    status: int | None = None
    direction: str = ""
    """For WebSocket frames: ``client->device`` or ``device->client``."""
    request_body: str | None = None
    response_body: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = {
            "seq": self.seq,
            "at": self.at,
            "kind": self.kind,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "direction": self.direction,
            "headers": self.headers,
            "note": self.note,
        }
        if self.request_body is not None:
            data["request_body"] = self.request_body
        if self.response_body is not None:
            data["response_body"] = self.response_body
        return {k: v for k, v in data.items() if v not in ("", None, {})}

    def render(self) -> str:
        if self.kind == "ws":
            arrow = "-->" if self.direction.startswith("client") else "<--"
            body = (self.request_body or self.response_body or "")[:120]
            return f"  [{self.seq:04d}] ws {arrow} {body}"
        return f"  [{self.seq:04d}] {self.method:6} {self.status or '---'} {self.path}"


def _decode(raw: bytes) -> str:
    if len(raw) > MAX_LOGGED_BODY:
        return (
            raw[:MAX_LOGGED_BODY].decode("utf-8", errors="replace")
            + f"... [truncated, {len(raw)} bytes total]"
        )
    return raw.decode("utf-8", errors="replace")


class Recording:
    """The log being built, and where it is written."""

    def __init__(self, out_dir: Path, upstream: str) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.upstream = upstream
        self.path = self.out_dir / "traffic.jsonl"
        self.exchanges: list[Exchange] = []
        self._seq = 0
        self._handle = self.path.open("w", encoding="utf-8")
        self._handle.write(
            json.dumps(
                {"kind": "meta", "upstream": upstream, "started": time.time()}
            )
            + "\n"
        )
        self._handle.flush()

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def record(self, exchange: Exchange) -> None:
        self.exchanges.append(exchange)
        self._handle.write(json.dumps(exchange.to_dict()) + "\n")
        self._handle.flush()

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def summary(self) -> str:
        http = [e for e in self.exchanges if e.kind == "http"]
        ws = [e for e in self.exchanges if e.kind == "ws"]
        lines = [
            f"Recorded {len(http)} HTTP exchange(s) and {len(ws)} WebSocket frame(s)",
            f"  log: {self.path}",
        ]
        paths: dict[tuple[str, str], int] = {}
        for exchange in http:
            key = (exchange.method, exchange.path.split("?")[0])
            paths[key] = paths.get(key, 0) + 1
        if paths:
            lines.append("\n  endpoints exercised:")
            for (method, path), count in sorted(paths.items(), key=lambda kv: -kv[1]):
                lines.append(f"    {method:6} x{count:<4} {path}")
        return "\n".join(lines)


def build_app(upstream: str, recording: Recording):
    """Build the reverse-proxy ASGI app."""
    _require_web()

    from contextlib import asynccontextmanager

    client = httpx.AsyncClient(base_url=upstream, timeout=30.0, follow_redirects=False)

    @asynccontextmanager
    async def lifespan(_app):  # pragma: no cover - lifecycle
        yield
        await client.aclose()

    app = FastAPI(
        title="rigsmith recorder", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    @app.websocket("/{path:path}")
    async def proxy_ws(websocket: WebSocket, path: str) -> None:
        import websockets as ws_client

        await websocket.accept()
        target = upstream.replace("http://", "ws://").replace("https://", "wss://")
        url = f"{target}/{path}"
        try:
            async with ws_client.connect(url) as upstream_ws:
                import asyncio

                async def pump_client() -> None:
                    while True:
                        message = await websocket.receive_text()
                        recording.record(
                            Exchange(
                                seq=recording.next_seq(),
                                at=time.time(),
                                kind="ws",
                                path=f"/{path}",
                                direction="client->device",
                                request_body=message[:MAX_LOGGED_BODY],
                            )
                        )
                        await upstream_ws.send(message)

                async def pump_device() -> None:
                    async for message in upstream_ws:
                        text = (
                            message
                            if isinstance(message, str)
                            else message.decode("utf-8", errors="replace")
                        )
                        recording.record(
                            Exchange(
                                seq=recording.next_seq(),
                                at=time.time(),
                                kind="ws",
                                path=f"/{path}",
                                direction="device->client",
                                response_body=text[:MAX_LOGGED_BODY],
                            )
                        )
                        await websocket.send_text(text)

                await asyncio.gather(pump_client(), pump_device())
        except (WebSocketDisconnect, OSError, Exception):
            # A closed socket on either side ends the session; the log is
            # already flushed, so nothing is lost.
            return

    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy_http(request: Request, path: str):
        body = await request.body()
        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP
        }
        seq = recording.next_seq()

        try:
            upstream_response = await client.request(
                request.method,
                "/" + path,
                params=dict(request.query_params),
                content=body or None,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            recording.record(
                Exchange(
                    seq=seq,
                    at=time.time(),
                    kind="http",
                    method=request.method,
                    path="/" + path,
                    note=f"upstream error: {exc}",
                )
            )
            return Response(content=f"upstream error: {exc}", status_code=502)

        recording.record(
            Exchange(
                seq=seq,
                at=time.time(),
                kind="http",
                method=request.method,
                path="/" + path,
                status=upstream_response.status_code,
                request_body=_decode(body) if body else None,
                response_body=(
                    _decode(upstream_response.content)
                    if _is_texty(upstream_response)
                    else f"[{len(upstream_response.content)} bytes binary]"
                ),
                headers={
                    k: v
                    for k, v in upstream_response.headers.items()
                    if k.lower() in ("content-type", "location")
                },
            )
        )

        passthrough = {
            k: v
            for k, v in upstream_response.headers.items()
            if k.lower() not in HOP_BY_HOP
        }
        return Response(
            content=upstream_response.content,
            status_code=upstream_response.status_code,
            headers=passthrough,
        )

    return app


def _is_texty(response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    return (
        content_type.startswith("text/")
        or "json" in content_type
        or "javascript" in content_type
        or "xml" in content_type
    )


def record(
    upstream: str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    out_dir: Path | None = None,
    open_browser: bool = True,
    on_ready=None,
) -> Recording:
    """Proxy the device's editor and log everything until interrupted.

    Args:
        upstream: The device base URL, e.g. ``http://headrushcore.local``.
        host: Bind address. Loopback by default - this proxies a device on
            your network and should not be exposed to it.
        port: 0 picks a free port.
        out_dir: Where the log is written.
        on_ready: Called with the local URL once listening.
    """
    import socket
    import threading
    import webbrowser

    _require_web()
    import uvicorn

    from ..config import get_settings

    settings = get_settings()
    settings.ensure_workspace()
    destination = Path(out_dir) if out_dir else settings.captures_dir / (
        "recording-" + time.strftime("%Y%m%d-%H%M%S")
    )

    if port == 0:
        with socket.socket() as probe:
            probe.bind((host, 0))
            port = probe.getsockname()[1]

    recording = Recording(destination, upstream)
    app = build_app(upstream, recording)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    url = f"http://{host}:{port}"
    if on_ready:
        on_ready(url)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        recording.close()

    return recording

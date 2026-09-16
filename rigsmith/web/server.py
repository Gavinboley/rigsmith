"""The local web server.

Binds to loopback by default and picks a free port, so it never collides and
never accidentally exposes device control to the network it is talking to.
"""

from __future__ import annotations

import socket
import threading
import webbrowser
from pathlib import Path

from ..core.errors import Unavailable


def static_dir() -> Path:
    """Locate the bundled static assets.

    Uses ``importlib.resources`` rather than ``__file__`` arithmetic so it
    works the same from a wheel, an editable install, or a zip.
    """
    from importlib import resources

    try:
        return Path(str(resources.files("rigsmith.web") / "static"))
    except (ModuleNotFoundError, AttributeError):  # pragma: no cover
        return Path(__file__).resolve().parent / "static"


def build_app():
    """Build the ASGI application."""
    try:
        from fastapi import FastAPI
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise Unavailable(
            "the web interface needs FastAPI",
            hint="Install it with:  pip install 'rigsmith[ui]'",
        ) from exc

    from .. import __version__
    from .api import install_error_handler, reset_audio_allowance, router
    from .ws import install_websocket

    # A new app serves a new session; files the last one was told about do
    # not carry over.
    reset_audio_allowance()

    app = FastAPI(
        title="rigsmith",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
    )
    install_error_handler(app)
    app.include_router(router)
    install_websocket(app)

    assets = static_dir()
    if assets.is_dir():
        app.mount(
            "/static", StaticFiles(directory=str(assets)), name="static"
        )

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(assets / "index.html")

    return app


def _free_port(host: str) -> int:
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def run_ui(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> None:
    """Serve the UI until interrupted.

    Loopback-only by default. This process can control an amp modeler on the
    local network, so binding it to that same network by accident would be a
    genuine footgun; ``--host`` exists for the deliberate case.
    """
    try:
        import uvicorn
    except ImportError as exc:
        raise Unavailable(
            "the web interface needs uvicorn",
            hint="Install it with:  pip install 'rigsmith[ui]'",
        ) from exc

    if port == 0:
        port = _free_port(host)

    url = f"http://{host}:{port}"
    print(f"rigsmith ui  ->  {url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print(
            "  WARNING: bound to a non-loopback address, so anyone on this "
            "network can control your device through it."
        )
    print("  Ctrl-C to stop.\n")

    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()

    server = uvicorn.Server(
        uvicorn.Config(build_app(), host=host, port=port, log_level="warning")
    )
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass

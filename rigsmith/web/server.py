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


def build_app(*, hosts=None):
    """Build the ASGI application.

    ``hosts`` names what the Host header may say; the default is this
    machine's loopback names (see :mod:`.guard`), and ``"*"`` accepts any -
    only for a deliberate ``--host`` bind to a network.
    """
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
    from .guard import LOOPBACK, LocalOnly

    allowed = None if hosts == "*" else frozenset(hosts) if hosts else LOOPBACK
    app.add_middleware(LocalOnly, hosts=allowed)
    app.include_router(router)
    from .routes import routers

    for extra in routers():
        app.include_router(extra)
    install_websocket(app)

    assets = static_dir()
    if assets.is_dir():
        app.mount(
            "/static", StaticFiles(directory=str(assets)), name="static"
        )

        # The page is not part of the API, and its return annotation names a
        # class imported in this function, which a postponed annotation
        # cannot resolve - in the schema it made /openapi.json a 500.
        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(assets / "index.html")

    from fastapi.responses import HTMLResponse

    from . import docs as guides

    # The guides in docs/, as pages (no return annotation, as above).
    @app.get("/docs/{name}", include_in_schema=False)
    def guide(name: str):
        page = guides.page(name)
        if page is None:
            return HTMLResponse(f"<p>No guide called {name!r} here - they are {', '.join(guides.GUIDES)}.</p>", status_code=404)
        return HTMLResponse(page)

    return app


#: The UI's usual port, so its address can be bookmarked. When something
#: else holds it, a free one is used instead.
DEFAULT_PORT = 8765


def already_running(host: str, port: int, workspace) -> bool:
    """A rigsmith UI on this same workspace is answering at ``host:port``."""
    import httpx

    try:
        data = httpx.get(f"http://{host}:{port}/api/health", timeout=1.0).json()
    except Exception:
        return False
    return isinstance(data, dict) and data.get("workspace") == str(workspace)


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def _free_port(host: str) -> int:
    with socket.socket() as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def run_ui(
    *,
    host: str = "127.0.0.1",
    port: int | None = None,
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

    if port is None:
        # The usual port - or, when this workspace's UI already has it, that
        # one: a second launch opens the first rather than starting another.
        from ..config import get_settings

        if already_running(host, DEFAULT_PORT, get_settings().workspace):
            url = f"http://{host}:{DEFAULT_PORT}"
            print(f"rigsmith ui is already running  ->  {url}")
            if open_browser:
                webbrowser.open(url)
            return
        port = DEFAULT_PORT if _port_free(host, DEFAULT_PORT) else _free_port(host)
    elif port == 0:
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

    # Bound to loopback, only loopback names are answered (a rebound name is
    # refused); bound to one address, that address too; bound to every
    # interface, any name - the warning above is the guard then.
    if host in ("127.0.0.1", "localhost", "::1"):
        hosts = None
    elif host in ("0.0.0.0", "::"):
        hosts = "*"
    else:
        hosts = {"127.0.0.1", "localhost", "::1", host}
    server = uvicorn.Server(
        uvicorn.Config(build_app(hosts=hosts), host=host, port=port, log_level="warning")
    )
    try:
        server.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass

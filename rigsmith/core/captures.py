"""NAM captures: find them, read them, and get them onto the device.

Two sources, and they are good at different things. The TONE3000 API is for
*selection* - it can filter on architecture, size, make, and calibration,
which is what you need to choose well. The device's own on-screen TONE3000
browser is for *delivery* - it downloads straight over Wi-Fi with no computer
in the loop.

Access is per-user OAuth, deliberately: TONE3000's terms prohibit systematic
downloading, so this searches on the user's behalf and self-throttles rather
than crawling.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import get_settings
from .errors import Invalid, NotFound, Unavailable


# --------------------------------------------------------------------------
# local .nam files
# --------------------------------------------------------------------------


def inspect(path: str | Path):
    """Read a local ``.nam`` file."""
    from ..tone.nam import parse_nam

    try:
        return parse_nam(path)
    except ValueError as exc:
        raise Invalid(f"could not read {Path(path).name}: {exc}") from exc
    except OSError as exc:
        raise NotFound(f"could not open {path}: {exc}") from exc


def render_inspection(model) -> str:
    """Format a capture's details, including the evidence for each inference.

    The `.nam` format carries no architecture-generation field, so A1/A2 is
    inferred. Showing the reasoning matters more than showing the verdict.
    """
    lines = [model.describe(), ""]
    lines.append(f"generation: {model.generation.value}")
    lines.extend(f"  - {item}" for item in model.generation_evidence)
    lines.append(f"size: {model.size or 'unknown'}")
    lines.extend(f"  - {item}" for item in model.size_evidence)

    verdict = {
        True: "yes",
        False: "NO - the Core does not accept this combination",
        None: "cannot tell - generation or size is unknown",
    }[model.headrush_compatible]
    lines.append(f"loadable on the Core: {verdict}")

    if model.is_calibrated:
        lines.append(
            f"calibrated: input {model.input_level_dbu} dBu, "
            f"output {model.output_level_dbu} dBu"
        )
    else:
        lines.append("calibrated: no - levels will need setting by ear")
    return "\n".join(lines)


def scan(directory: str | Path):
    """Parse every ``.nam`` under a directory, skipping unreadable ones."""
    from ..tone.nam import scan_directory

    path = Path(directory)
    if not path.is_dir():
        raise NotFound(f"{directory} is not a directory")
    return scan_directory(path)


def render_scan(models, directory: str | Path) -> str:
    if not models:
        return f"No readable .nam files under {directory}."
    lines = [f"{len(models)} capture(s) in {directory}:"]
    lines.extend(f"  {m.describe()}" for m in models)
    a2 = sum(1 for m in models if m.generation.value == "A2")
    unknown = sum(1 for m in models if m.generation.value == "unknown")
    lines.append(
        f"\nA2: {a2}   A1: {len(models) - a2 - unknown}   undetermined: {unknown}"
    )
    return "\n".join(lines)


def rank_local(
    directory: str | Path,
    *,
    gear_make: str | None = None,
    gear_model: str | None = None,
    tone_type: str | None = None,
):
    """Score a local capture library against a tone target.

    Metadata agreement only - a ranking aid, not a measure of how close
    anything sounds. That only comes from rendering audio through it.
    """
    from ..tone.nam import Generation, rank_for_target

    models = scan(directory)
    return rank_for_target(
        models,
        gear_make=gear_make,
        gear_model=gear_model,
        tone_type=tone_type,
        prefer_generation=Generation.A2,
    )


# --------------------------------------------------------------------------
# TONE3000
# --------------------------------------------------------------------------


@dataclass
class AuthStatus:
    """Whether TONE3000 is usable right now, and what is missing."""

    client_id: bool
    tokens: bool
    expired: bool = False
    username: str | None = None

    @property
    def ready(self) -> bool:
        return self.client_id and self.tokens and not self.expired

    def render(self) -> str:
        if self.ready:
            who = f" as {self.username}" if self.username else ""
            return f"TONE3000: authenticated{who}"
        lines = ["TONE3000: not ready"]
        if not self.client_id:
            lines.append(
                "  no client id - set TONE3000_CLIENT_ID "
                "(register an app at https://www.tone3000.com/api)"
            )
        if not self.tokens:
            lines.append("  not logged in - run:  rigsmith tone3000 login")
        elif self.expired:
            lines.append("  token expired - run:  rigsmith tone3000 login")
        lines.append(
            "  You can also browse TONE3000 on the Core's own touchscreen; "
            "that path needs no API access at all."
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "client_id": self.client_id,
            "tokens": self.tokens,
            "expired": self.expired,
            "username": self.username,
            "ready": self.ready,
        }


def auth_status() -> AuthStatus:
    """Report TONE3000 credentials without making a network call."""
    from ..tone.tone3000 import load_tokens

    settings = get_settings()
    tokens = load_tokens(settings.credentials_path)
    return AuthStatus(
        client_id=bool(settings.tone3000_client_id),
        tokens=tokens is not None,
        expired=bool(tokens and tokens.expired),
    )


def login(
    *,
    port: int = 0,
    timeout_s: float = 300.0,
    open_browser: bool = True,
    on_url: "Callable[[str], None] | None" = None,
) -> AuthStatus:
    """Complete the TONE3000 OAuth flow and save the tokens.

    Runs a one-shot loopback listener to catch the redirect, so nothing has to
    be pasted by hand. Binds to 127.0.0.1 on an ephemeral port by default.

    Args:
        port: Callback port. 0 lets the OS choose a free one.
        timeout_s: How long to wait for the user to authorise.
        open_browser: Launch a browser at the authorize URL.
        on_url: Called with the URL, so a surface can display it rather than
            (or as well as) opening a browser.

    Raises:
        Unavailable: if no client id is configured, or the flow times out.
    """
    import http.server
    import threading
    import urllib.parse
    import webbrowser

    from ..tone.tone3000 import Tone3000Client, generate_pkce, save_tokens

    settings = get_settings()
    if not settings.tone3000_client_id:
        raise Unavailable(
            "no TONE3000 client id configured",
            hint=(
                "Register an application at https://www.tone3000.com/api and "
                "set TONE3000_CLIENT_ID to its publishable client id."
            ),
        )

    verifier, challenge = generate_pkce()
    received: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - stdlib naming
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            received.update({k: v[0] for k, v in params.items()})

            body = (
                b"<html><body style='font-family:system-ui;padding:3rem'>"
                b"<h2>rigsmith</h2><p>Authorisation received. "
                b"You can close this tab and return to the terminal.</p>"
                b"</body></html>"
                if "code" in received
                else b"<html><body style='font-family:system-ui;padding:3rem'>"
                b"<h2>rigsmith</h2><p>No authorisation code was returned.</p>"
                b"</body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, *args):  # keep the console clean
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    actual_port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{actual_port}/callback"

    client = Tone3000Client(client_id=settings.tone3000_client_id)
    url, state = client.authorize_url(redirect_uri, challenge=challenge)

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        if on_url:
            on_url(url)
        if open_browser:
            webbrowser.open(url)
        if not done.wait(timeout_s):
            raise Unavailable(
                f"timed out after {timeout_s:.0f}s waiting for authorisation",
                hint="Run the login again, or open the URL manually.",
            )
    finally:
        server.shutdown()
        server.server_close()

    if received.get("state") and received["state"] != state:
        raise Unavailable(
            "the authorisation response did not match this request",
            hint="Start the login again; do not reuse an old browser tab.",
        )
    if "code" not in received:
        detail = received.get("error_description") or received.get("error") or "no code"
        raise Unavailable(f"authorisation failed: {detail}")

    import asyncio

    tokens = asyncio.run(
        client.exchange_code(
            received["code"], verifier=verifier, redirect_uri=redirect_uri
        )
    )
    save_tokens(settings.credentials_path, tokens)
    return auth_status()


def _client():
    from ..tone.tone3000 import Tone3000Client, load_tokens

    settings = get_settings()
    tokens = load_tokens(settings.credentials_path)
    if tokens is None:
        raise Unavailable(
            "not authenticated with TONE3000",
            hint=(
                "Run:  rigsmith tone3000 login\n"
                "You can also browse TONE3000 directly on the Core's "
                "touchscreen - that needs no API access and downloads "
                "straight to the device."
            ),
        )
    return Tone3000Client(tokens=tokens, client_id=settings.tone3000_client_id)


async def search(
    query: str = "",
    *,
    make: str | None = None,
    tags: list[str] | None = None,
    architecture: str = "2",
    sizes: list[str] | None = None,
    calibrated: bool = False,
    limit: int = 15,
):
    """Search TONE3000. Defaults to A2, which is what the Core runs best."""
    from ..tone.tone3000 import Tone3000Error

    client = _client()
    try:
        return await client.search(
            query,
            architecture=None if architecture == "any" else int(architecture),
            makes=[make] if make else None,
            tags=tags,
            sizes=sizes,
            calibrated=calibrated or None,
            page_size=min(limit, 25),
        )
    except Tone3000Error:
        raise
    except ValueError as exc:
        raise Invalid(str(exc)) from exc


def render_search(tones, query: str = "") -> str:
    if not tones:
        return f"No captures matched '{query}'." if query else "No captures found."
    lines = [f"{len(tones)} capture(s):"]
    lines.extend(f"  {t.describe()}" for t in tones)
    lines.append(
        "\nInspect a downloaded .nam before committing to it - the API's "
        "architecture field and the file's own contents can disagree."
    )
    return "\n".join(lines)

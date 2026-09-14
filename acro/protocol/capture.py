"""Pull the Remote Editor's served bundle and mine it for the wire protocol.

The device serves a self-contained web app. That bundle necessarily contains
every endpoint the app can call, so fetching and reading it recovers the API
surface without touching the device's storage or firmware. This is ordinary
HTTP GET traffic against a device on your own network.

Two passes:

``fetch_bundle``
    Walk ``/``, follow every script/style/module reference it finds, and
    archive the lot.

``analyse_bundle``
    Mine the archived text for endpoint paths, WebSocket URLs, and the
    vocabulary of message-type strings that tend to sit alongside them.

The result is an :class:`Inventory` - a JSON-serialisable description of the
API surface that later phases turn into a typed client.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

# Assets referenced from HTML. Kept deliberately loose - the bundle's exact
# shape is unknown until this runs against real hardware.
_ASSET_PATTERNS = (
    re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I),
    re.compile(r"""<link[^>]+href=["']([^"']+)["']""", re.I),
    re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.I),
)

# ES module specifiers inside JS, for bundles that ship as real modules.
_IMPORT_PATTERNS = (
    re.compile(r"""\bimport\s+[^;'"]*from\s*["']([^"']+)["']"""),
    re.compile(r"""\bimport\s*\(\s*["']([^"']+)["']\s*\)"""),
    re.compile(r"""\bfrom\s*["'](\.{1,2}/[^"']+)["']"""),
)

# Absolute-ish API paths. Requires a second segment or a trailing slash so we
# do not collect every "/" and "/x" string in the bundle.
_PATH_PATTERN = re.compile(r"""["'`](/(?:api|v\d|rig|rigs|setlist|setlists|block|blocks|preset|presets|scene|scenes|model|models|catalog|cloud|system|device|status|param|params|ws|socket|file|files|ir|nam|clone|clones)[A-Za-z0-9_\-./${}:]*)["'`]""", re.I)

# Any path-looking literal with at least two segments - broader net, reported
# separately so a human can triage it.
_GENERIC_PATH_PATTERN = re.compile(r"""["'`](/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-.${}:]+)+)["'`]""")

_WS_PATTERN = re.compile(r"""["'`](wss?://[^"'`\s]+)["'`]""", re.I)
_WS_CTOR_PATTERN = re.compile(r"""new\s+WebSocket\s*\(\s*([^)]{0,200})\)""", re.I)

# fetch()/axios/XHR call sites, to distinguish real calls from stray strings.
_CALL_PATTERNS = (
    re.compile(r"""\bfetch\s*\(\s*["'`]([^"'`]+)["'`]"""),
    re.compile(r"""\.(get|post|put|patch|delete|head)\s*\(\s*["'`]([^"'`]+)["'`]""", re.I),
    re.compile(r"""\bopen\s*\(\s*["'](GET|POST|PUT|PATCH|DELETE)["']\s*,\s*["'`]([^"'`]+)["'`]""", re.I),
)

_HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}

_METHOD_HINT = re.compile(
    r"""method\s*:\s*["'](GET|POST|PUT|PATCH|DELETE)["']""", re.I
)

# Message-type vocabularies commonly used by device web UIs. The key may be
# quoted (JSON, or JS that survived minification) or bare (object literal).
_MSG_KEYS = "type|event|cmd|command|action|op|msg|method"
_MSG_TYPE_PATTERN = re.compile(
    r"""(?:["'`]\s*(?:%s)\s*["'`]|\b(?:%s)\b)\s*:\s*["'`]([A-Za-z0-9_.\-]{2,48})["'`]"""
    % (_MSG_KEYS, _MSG_KEYS)
)

_TEXTUAL_SUFFIXES = {".html", ".htm", ".js", ".mjs", ".cjs", ".css", ".json", ".map", ".txt", ""}


@dataclass
class Asset:
    """One file retrieved from the device."""

    url: str
    path: str
    status: int
    content_type: str
    size: int
    saved_as: str | None = None
    error: str | None = None

    @property
    def is_text(self) -> bool:
        ct = (self.content_type or "").lower()
        return (
            ct.startswith("text/")
            or "javascript" in ct
            or "json" in ct
            or "xml" in ct
            or Path(urlparse(self.url).path).suffix.lower() in _TEXTUAL_SUFFIXES
        )


@dataclass
class Endpoint:
    """A candidate API endpoint found in the bundle."""

    path: str
    methods: list[str] = field(default_factory=list)
    #: "call" means it appeared at a fetch/axios call site - much stronger
    #: evidence than "literal", which is just a string that looks like a path.
    evidence: str = "literal"
    sources: list[str] = field(default_factory=list)
    occurrences: int = 1

    @property
    def templated(self) -> bool:
        return "${" in self.path or "{" in self.path


@dataclass
class Inventory:
    """Everything static analysis could work out about the wire protocol."""

    base_url: str
    captured_at: str
    firmware: str | None = None
    device_title: str | None = None
    server: str | None = None
    assets: list[Asset] = field(default_factory=list)
    endpoints: list[Endpoint] = field(default_factory=list)
    generic_paths: list[str] = field(default_factory=list)
    websockets: list[str] = field(default_factory=list)
    websocket_constructors: list[str] = field(default_factory=list)
    message_types: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "captured_at": self.captured_at,
            "firmware": self.firmware,
            "device_title": self.device_title,
            "server": self.server,
            "assets": [asdict(a) for a in self.assets],
            "endpoints": [asdict(e) for e in self.endpoints],
            "generic_paths": self.generic_paths,
            "websockets": self.websockets,
            "websocket_constructors": self.websocket_constructors,
            "message_types": self.message_types,
            "notes": self.notes,
        }

    def summary(self) -> str:
        ok = sum(1 for a in self.assets if a.error is None)
        strong = sum(1 for e in self.endpoints if e.evidence == "call")
        return (
            f"{ok}/{len(self.assets)} assets, "
            f"{len(self.endpoints)} endpoints ({strong} at call sites), "
            f"{len(self.websockets)} websocket URLs, "
            f"{len(self.message_types)} message types"
        )


def _normalise(url: str, base: str) -> str | None:
    """Resolve an asset reference against the bundle root, or drop it."""
    url = url.strip()
    if not url or url.startswith(("data:", "blob:", "mailto:", "javascript:", "#")):
        return None
    absolute = urljoin(base, url)
    if not absolute.startswith(("http://", "https://")):
        return None
    # Stay on the device; never follow references off to a CDN.
    if urlparse(absolute).netloc != urlparse(base).netloc:
        return None
    return absolute.split("#", 1)[0]


async def fetch_bundle(
    base_url: str,
    *,
    max_assets: int = 200,
    timeout: float = 15.0,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[Asset], dict[str, str]]:
    """Fetch ``/`` and everything it references.

    Returns the assets plus a ``url -> text`` map for the textual ones, which
    is what :func:`analyse_bundle` mines.
    """
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    assets: list[Asset] = []
    texts: dict[str, str] = {}
    queue: list[str] = [urljoin(base_url + "/", "/")]
    seen: set[str] = set()

    try:
        while queue and len(assets) < max_assets:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)

            asset = Asset(
                url=url,
                path=urlparse(url).path or "/",
                status=0,
                content_type="",
                size=0,
            )
            try:
                resp = await client.get(url)
                asset.status = resp.status_code
                asset.content_type = resp.headers.get("content-type", "")
                body = resp.content
                asset.size = len(body)
                if resp.status_code >= 400:
                    asset.error = f"HTTP {resp.status_code}"
                elif asset.is_text:
                    text = resp.text
                    texts[url] = text
                    for pattern in _ASSET_PATTERNS + _IMPORT_PATTERNS:
                        for match in pattern.findall(text):
                            nxt = _normalise(match, url)
                            if nxt and nxt not in seen:
                                queue.append(nxt)
            except (httpx.HTTPError, OSError) as exc:
                asset.error = f"{type(exc).__name__}: {exc}"
            assets.append(asset)
    finally:
        if owns_client:
            await client.aclose()

    return assets, texts


def analyse_bundle(texts: dict[str, str]) -> tuple[
    list[Endpoint], list[str], list[str], list[str], list[str]
]:
    """Mine fetched text for endpoints, WS URLs, and message vocabulary.

    Returns ``(endpoints, generic_paths, websockets, ws_constructors, message_types)``.
    """
    endpoints: dict[str, Endpoint] = {}
    generic: dict[str, int] = {}
    websockets: set[str] = set()
    ws_ctors: set[str] = set()
    message_types: set[str] = set()

    def record(path: str, source: str, evidence: str, method: str | None = None) -> None:
        path = path.strip()
        if not path.startswith("/") or len(path) < 2:
            return
        existing = endpoints.get(path)
        if existing is None:
            existing = Endpoint(path=path, evidence=evidence, occurrences=0)
            endpoints[path] = existing
        existing.occurrences += 1
        # A call site is stronger evidence than a bare literal; never downgrade.
        if evidence == "call":
            existing.evidence = "call"
        if method:
            method = method.upper()
            if method not in existing.methods:
                existing.methods.append(method)
        label = urlparse(source).path or source
        if label not in existing.sources:
            existing.sources.append(label)

    for url, text in texts.items():
        for pattern in _CALL_PATTERNS:
            for match in pattern.findall(text):
                if isinstance(match, tuple):
                    method, path = match[0], match[1]
                    record(path, url, "call", method)
                else:
                    record(match, url, "call")

        for match in _PATH_PATTERN.findall(text):
            record(match, url, "literal")

        for match in _GENERIC_PATH_PATTERN.findall(text):
            if match not in endpoints:
                generic[match] = generic.get(match, 0) + 1

        websockets.update(_WS_PATTERN.findall(text))
        for match in _WS_CTOR_PATTERN.findall(text):
            ws_ctors.add(" ".join(match.split())[:200])
        # A bare HTTP verb here is a `method: "POST"` request option, not a
        # protocol message type.
        message_types.update(
            m for m in _MSG_TYPE_PATTERN.findall(text)
            if m.upper() not in _HTTP_VERBS
        )

        for method in _METHOD_HINT.findall(text):
            # Method hints without an adjacent path are recorded as a note by
            # the caller rather than guessed at here.
            _ = method

    ordered = sorted(
        endpoints.values(),
        key=lambda e: (e.evidence != "call", -e.occurrences, e.path),
    )
    top_generic = sorted(generic, key=lambda p: (-generic[p], p))[:200]
    return (
        ordered,
        top_generic,
        sorted(websockets),
        sorted(ws_ctors),
        sorted(message_types),
    )


async def capture(
    base_url: str,
    *,
    out_dir: Path | None = None,
    max_assets: int = 200,
    timeout: float = 15.0,
    save_assets: bool = True,
) -> Inventory:
    """Fetch and analyse the Remote Editor bundle, optionally archiving it.

    Args:
        base_url: e.g. ``http://headrushcore.local``.
        out_dir: where to archive assets and write ``inventory.json``.
        save_assets: archive the raw files as well as the inventory. These are
            vendor assets - they stay in the workspace and are never committed.
    """
    assets, texts = await fetch_bundle(
        base_url, max_assets=max_assets, timeout=timeout
    )

    endpoints, generic, websockets, ws_ctors, message_types = analyse_bundle(texts)

    root_text = next(
        (t for url, t in texts.items() if urlparse(url).path in ("/", "/index.html")),
        "",
    )
    title_match = re.search(r"<title[^>]*>(.*?)</title>", root_text, re.I | re.S)
    firmware_match = re.search(
        r"""(?:firmware|fw|version)["'\s:=]{1,6}v?(\d+\.\d+(?:\.\d+)?)""",
        root_text or " ".join(list(texts.values())[:3]),
        re.I,
    )

    inventory = Inventory(
        base_url=base_url,
        captured_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        firmware=firmware_match.group(1) if firmware_match else None,
        device_title=title_match.group(1).strip() if title_match else None,
        assets=assets,
        endpoints=endpoints,
        generic_paths=generic,
        websockets=websockets,
        websocket_constructors=ws_ctors,
        message_types=message_types,
    )

    if not endpoints:
        inventory.notes.append(
            "No endpoints recovered. The bundle may be minified beyond these "
            "patterns, lazily loaded, or served from a path other than /. "
            "Fall back to acro.protocol.recorder to capture live traffic."
        )
    if not websockets and not ws_ctors:
        inventory.notes.append(
            "No WebSocket references found; the editor may poll over plain HTTP."
        )

    if out_dir is not None:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        if save_assets:
            asset_root = out_dir / "assets"
            for asset in assets:
                text = texts.get(asset.url)
                if text is None:
                    continue
                rel = (urlparse(asset.url).path or "/").lstrip("/") or "index.html"
                if rel.endswith("/"):
                    rel += "index.html"
                dest = asset_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(text, encoding="utf-8", errors="replace")
                asset.saved_as = str(dest.relative_to(out_dir))
        (out_dir / "inventory.json").write_text(
            json.dumps(inventory.to_dict(), indent=2), encoding="utf-8"
        )

    return inventory


def load_inventory(path: Path) -> Inventory:
    """Read an ``inventory.json`` back into an :class:`Inventory`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    inv = Inventory(
        base_url=data["base_url"],
        captured_at=data["captured_at"],
        firmware=data.get("firmware"),
        device_title=data.get("device_title"),
        server=data.get("server"),
        generic_paths=data.get("generic_paths", []),
        websockets=data.get("websockets", []),
        websocket_constructors=data.get("websocket_constructors", []),
        message_types=data.get("message_types", []),
        notes=data.get("notes", []),
    )
    inv.assets = [Asset(**a) for a in data.get("assets", [])]
    inv.endpoints = [Endpoint(**e) for e in data.get("endpoints", [])]
    return inv

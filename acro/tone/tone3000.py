"""Client for the TONE3000 API.

TONE3000 is where the NAM captures live, and its search endpoint filters on
exactly what matters here - ``architecture`` (A1 vs A2), ``sizes``, ``makes``,
``tags``, and ``format``. Since the HeadRush Core runs A2 Full and Lite, an
A2-first search is the default.

**Access terms.** This is a per-user OAuth client, deliberately. TONE3000's
terms prohibit systematic downloading and bulk extraction, so this module
authenticates *as the user* and makes bounded, rate-limited queries. It does
not crawl, and it does not cache other people's captures wholesale.

Some endpoints are tier-gated: the free tier covers the OAuth flows plus the
bounded endpoints (favorited, downloaded, created, trending, latest), while
broader search and the zip download endpoint may require a commercial
agreement. When the API says no, this client reports that plainly instead of
retrying or working around it.

For getting captures *onto the device*, prefer the Core's own on-screen
TONE3000 browser - it downloads directly over Wi-Fi with no computer in the
loop. This client is primarily for research and selection.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import httpx

BASE_URL = "https://www.tone3000.com"
API = "/api/v1"

#: Documented default. Exceeding it is the caller's problem to avoid, so the
#: client self-throttles rather than discovering the limit the hard way.
RATE_LIMIT_PER_MINUTE = 100

#: Search caps, from the API docs.
MAX_PAGE_SIZE = 25
MAX_MODEL_PAGE_SIZE = 300

GEAR_TYPES = ("amp", "amp-cab", "pedal", "cab", "outboard", "space", "experimental")
SIZES = ("standard", "lite", "feather", "nano", "custom")
FORMATS = ("nam", "ir", "aida-x", "aa-snapshot", "proteus")
SORTS = ("best-match", "newest", "oldest", "trending", "downloads-all-time")


class Tone3000Error(RuntimeError):
    """An API call failed."""


class Tone3000AuthError(Tone3000Error):
    """Authentication is missing, expired, or insufficient for this endpoint."""


class Tone3000TierError(Tone3000Error):
    """The endpoint requires a tier this account does not have."""


@dataclass
class Tone:
    """A TONE3000 "tone" - a captured piece of gear, holding one or more models."""

    id: str
    title: str
    gear: str | None = None
    format: str | None = None
    makes: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)
    license: str | None = None
    a1_models_count: int = 0
    a2_models_count: int = 0
    irs_count: int = 0
    custom_models_count: int = 0
    downloads_count: int = 0
    favorites_count: int = 0
    creator: str | None = None
    url: str | None = None
    description: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def has_a2(self) -> bool:
        return self.a2_models_count > 0

    def describe(self) -> str:
        bits = [f"{self.title} (#{self.id})"]
        if self.makes:
            bits.append(f"[{', '.join(self.makes[:3])}]")
        arch = []
        if self.a2_models_count:
            arch.append(f"A2 x{self.a2_models_count}")
        if self.a1_models_count:
            arch.append(f"A1 x{self.a1_models_count}")
        if self.irs_count:
            arch.append(f"IR x{self.irs_count}")
        if arch:
            bits.append("/".join(arch))
        if self.sizes:
            bits.append(f"sizes={','.join(self.sizes)}")
        if self.downloads_count:
            bits.append(f"{self.downloads_count} downloads")
        return " ".join(bits)

    @classmethod
    def from_api(cls, data: dict) -> "Tone":
        user = data.get("user") or {}
        return cls(
            id=str(data.get("id", "")),
            title=data.get("title") or "(untitled)",
            gear=data.get("gear"),
            format=data.get("format"),
            makes=list(data.get("makes") or []),
            tags=list(data.get("tags") or []),
            sizes=list(data.get("sizes") or []),
            license=data.get("license"),
            a1_models_count=int(data.get("a1_models_count") or 0),
            a2_models_count=int(data.get("a2_models_count") or 0),
            irs_count=int(data.get("irs_count") or 0),
            custom_models_count=int(data.get("custom_models_count") or 0),
            downloads_count=int(data.get("downloads_count") or 0),
            favorites_count=int(data.get("favorites_count") or 0),
            creator=user.get("username") or user.get("name"),
            url=data.get("url"),
            description=data.get("description"),
            raw=data,
        )


@dataclass
class ToneModel:
    """One downloadable model belonging to a tone."""

    id: str
    name: str | None = None
    size: str | None = None
    tone_id: str | None = None
    architecture_version: int | str | None = None
    model_url: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def generation(self) -> str:
        raw = str(self.architecture_version or "").strip().lower().lstrip("a")
        if raw.startswith("2"):
            return "A2"
        if raw.startswith("1"):
            return "A1"
        return "unknown"

    @property
    def headrush_compatible(self) -> bool | None:
        from .nam import HEADRUSH_SUPPORTED

        if self.generation == "unknown" or not self.size:
            return None
        return (self.generation, self.size.lower()) in HEADRUSH_SUPPORTED

    def describe(self) -> str:
        return f"{self.name or self.id} {self.generation} {self.size or '?'}"

    @classmethod
    def from_api(cls, data: dict) -> "ToneModel":
        return cls(
            id=str(data.get("id", "")),
            name=data.get("name"),
            size=data.get("size"),
            tone_id=str(data["tone_id"]) if data.get("tone_id") is not None else None,
            architecture_version=data.get("architecture_version"),
            model_url=data.get("model_url"),
            raw=data,
        )


# --------------------------------------------------------------------------
# OAuth 2.0 with PKCE
# --------------------------------------------------------------------------


def generate_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)`` for an OAuth PKCE exchange."""
    verifier = base64.urlsafe_b64encode(os.urandom(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


@dataclass
class TokenSet:
    """Stored OAuth credentials."""

    access_token: str
    refresh_token: str | None = None
    expires_at: float | None = None
    token_type: str = "Bearer"

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= (self.expires_at - 60)

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TokenSet":
        return cls(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=data.get("expires_at"),
            token_type=data.get("token_type", "Bearer"),
        )


def load_tokens(path: Path) -> TokenSet | None:
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    blob = data.get("tone3000")
    return TokenSet.from_dict(blob) if blob else None


def save_tokens(path: Path, tokens: TokenSet) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data["tone3000"] = tokens.to_dict()
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    try:  # best effort; Windows ignores POSIX modes
        path.chmod(0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def _join(values: Iterable[str] | str | None, separator: str) -> str | None:
    if values is None:
        return None
    if isinstance(values, str):
        return values
    joined = separator.join(str(v) for v in values if v)
    return joined or None


class Tone3000Client:
    """Thin, honest wrapper over the TONE3000 REST API."""

    def __init__(
        self,
        *,
        tokens: TokenSet | None = None,
        client_id: str | None = None,
        base_url: str = BASE_URL,
        timeout: float = 20.0,
        rate_limit: int = RATE_LIMIT_PER_MINUTE,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tokens = tokens
        self.client_id = client_id
        self.timeout = timeout
        self._min_interval = 60.0 / max(1, rate_limit)
        self._last_request = 0.0

    # -- plumbing ---------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "acro/0.1 (+github.com/Gavinboley/acro-plan)"}
        if self.tokens:
            headers["Authorization"] = f"{self.tokens.token_type} {self.tokens.access_token}"
        return headers

    async def _throttle(self) -> None:
        import asyncio

        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        await self._throttle()
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{path}"

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params, headers=self._headers())

        if resp.status_code in (401, 403):
            detail = resp.text[:300]
            if not self.tokens:
                raise Tone3000AuthError(
                    f"{path} requires authentication. Run 'acro tone3000 login' first. "
                    f"({resp.status_code}: {detail})"
                )
            if resp.status_code == 403:
                raise Tone3000TierError(
                    f"{path} was refused for this account. The free tier covers the "
                    f"OAuth flows and the bounded endpoints (favorited, downloaded, "
                    f"created, trending, latest); wider search and the download "
                    f"endpoint may need a commercial agreement. ({detail})"
                )
            raise Tone3000AuthError(f"{path}: token rejected ({detail})")
        if resp.status_code == 429:
            raise Tone3000Error(
                f"{path}: rate limited. The documented default is "
                f"{RATE_LIMIT_PER_MINUTE} requests/minute."
            )
        if resp.status_code >= 400:
            raise Tone3000Error(f"{path}: HTTP {resp.status_code} - {resp.text[:300]}")

        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise Tone3000Error(f"{path}: response was not JSON ({exc})") from exc

    # -- auth -------------------------------------------------------------

    def authorize_url(
        self,
        redirect_uri: str,
        *,
        challenge: str,
        state: str | None = None,
        prompt: str | None = None,
        tone_id: str | None = None,
        scope: str | None = None,
    ) -> tuple[str, str]:
        """Build the authorization URL. Returns ``(url, state)``.

        ``prompt`` may be ``select_tone`` (let the user pick) or ``load_tone``
        (with ``tone_id``), which are the lighter-weight flows.
        """
        if not self.client_id:
            raise Tone3000AuthError(
                "no client_id configured. Set TONE3000_CLIENT_ID or pass client_id."
            )
        state = state or secrets.token_urlsafe(24)
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        if prompt:
            params["prompt"] = prompt
        if tone_id:
            params["tone_id"] = tone_id
        if scope:
            params["scope"] = scope

        query = urlencode(params)
        return f"{self.base_url}{API}/oauth/authorize?{query}", state

    async def exchange_code(
        self, code: str, *, verifier: str, redirect_uri: str
    ) -> TokenSet:
        """Exchange an authorization code for tokens."""
        if not self.client_id:
            raise Tone3000AuthError("no client_id configured")

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}{API}/oauth/token", data=payload)
        if resp.status_code >= 400:
            raise Tone3000AuthError(
                f"token exchange failed: HTTP {resp.status_code} - {resp.text[:300]}"
            )
        return self._tokens_from_response(resp.json())

    async def refresh(self) -> TokenSet:
        """Refresh the access token."""
        if not (self.tokens and self.tokens.refresh_token):
            raise Tone3000AuthError("no refresh token available; log in again")
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.tokens.refresh_token,
            "client_id": self.client_id,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}{API}/oauth/token", data=payload)
        if resp.status_code >= 400:
            raise Tone3000AuthError(
                f"refresh failed: HTTP {resp.status_code} - {resp.text[:300]}"
            )
        self.tokens = self._tokens_from_response(resp.json())
        return self.tokens

    @staticmethod
    def _tokens_from_response(data: dict) -> TokenSet:
        expires_in = data.get("expires_in")
        return TokenSet(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=time.time() + float(expires_in) if expires_in else None,
            token_type=data.get("token_type", "Bearer"),
        )

    # -- reads ------------------------------------------------------------

    async def search(
        self,
        query: str = "",
        *,
        architecture: str | int | None = 2,
        gears: Iterable[str] | str | None = None,
        sizes: Iterable[str] | str | None = None,
        makes: Iterable[str] | str | None = None,
        tags: Iterable[str] | str | None = None,
        creators: Iterable[str] | str | None = None,
        fmt: str | None = "nam",
        calibrated: bool | None = None,
        verified: bool | None = None,
        sort: str = "best-match",
        page: int = 1,
        page_size: int = MAX_PAGE_SIZE,
    ) -> list[Tone]:
        """Search tones.

        ``architecture`` defaults to ``2`` (A2), which is what the Core runs
        best. Pass ``None`` to let the API apply its own default, which
        returns A1 plus custom.

        Note the separator quirk in the API: ``gears``, ``sizes``, ``makes``,
        and ``tags`` are underscore-separated except ``sizes``, which is
        hyphen-separated; ``creators`` is comma-separated.
        """
        if sort not in SORTS:
            raise ValueError(f"sort must be one of {', '.join(SORTS)}")

        params = {
            "query": query or None,
            "architecture": architecture,
            "gears": _join(gears, "_"),
            "sizes": _join(sizes, "-"),
            "makes": _join(makes, "_"),
            "tags": _join(tags, "_"),
            "creators": _join(creators, ","),
            "format": fmt,
            "calibrated": "true" if calibrated else None,
            "verified": "true" if verified else None,
            "sort": sort,
            "page": page,
            "page_size": min(page_size, MAX_PAGE_SIZE),
        }
        data = await self._get(f"{API}/tones/search", params)
        return [Tone.from_api(item) for item in _items(data)]

    async def trending(self) -> list[Tone]:
        return [Tone.from_api(i) for i in _items(await self._get(f"{API}/tones/trending"))]

    async def latest(self) -> list[Tone]:
        return [Tone.from_api(i) for i in _items(await self._get(f"{API}/tones/latest"))]

    async def favorited(self) -> list[Tone]:
        return [Tone.from_api(i) for i in _items(await self._get(f"{API}/tones/favorited"))]

    async def created(self) -> list[Tone]:
        return [Tone.from_api(i) for i in _items(await self._get(f"{API}/tones/created"))]

    async def get_tone(self, tone_id: str, *, architecture: str | int | None = None) -> Tone:
        data = await self._get(f"{API}/tones/{tone_id}", {"architecture": architecture})
        return Tone.from_api(data.get("data", data) if isinstance(data, dict) else data)

    async def list_models(
        self,
        tone_id: str,
        *,
        architecture: str | int | None = None,
        page: int = 1,
        page_size: int = 100,
    ) -> list[ToneModel]:
        data = await self._get(
            f"{API}/models",
            {
                "tone_id": tone_id,
                "architecture": architecture,
                "page": page,
                "page_size": min(page_size, MAX_MODEL_PAGE_SIZE),
            },
        )
        return [ToneModel.from_api(item) for item in _items(data)]

    async def makes(self) -> list[str]:
        data = await self._get(f"{API}/makes")
        items = _items(data)
        return [i if isinstance(i, str) else str(i.get("name") or i.get("id")) for i in items]

    async def tags(self) -> list[str]:
        data = await self._get(f"{API}/tags")
        items = _items(data)
        return [i if isinstance(i, str) else str(i.get("name") or i.get("id")) for i in items]

    async def whoami(self) -> dict:
        data = await self._get(f"{API}/user")
        return data.get("data", data) if isinstance(data, dict) else {}

    async def download_model(self, model: ToneModel, destination: Path) -> Path:
        """Download one model file to ``destination``.

        Uses the ``model_url`` from the API, which is only valid for content
        the authenticated user may access.
        """
        if not model.model_url:
            raise Tone3000Error(
                f"model {model.id} has no model_url - it may not be accessible "
                "to this account, or the tone needs fetching first"
            )
        await self._throttle()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(timeout=max(self.timeout, 60.0), follow_redirects=True) as client:
            resp = await client.get(model.model_url, headers=self._headers())
        if resp.status_code >= 400:
            raise Tone3000Error(
                f"download failed for model {model.id}: HTTP {resp.status_code}"
            )
        destination.write_bytes(resp.content)
        return destination


def _items(payload: Any) -> list[dict]:
    """Pull a list of records out of whichever envelope the API used."""
    if isinstance(payload, list):
        return [p for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "items", "tones", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                return [v for v in value if isinstance(v, dict)]
        # A single record returned bare.
        if "id" in payload:
            return [payload]
    return []

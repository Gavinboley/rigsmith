"""Finding a song: who made it, when, and something to listen to.

Two public services, both free and keyless:

* the **iTunes Search API** - title, artist, album, year, genre, artwork, and
  a 30-second preview. Previews are licensed for promotion, so they are
  streamed to the page with the store's attribution, and only fetched into
  the workspace for analysis when the setting allowing it is on.
* **MusicBrainz** - the recording's credits and release history, which is
  where "who played guitar on this" and "which album version" get answered.

Neither gives you the song itself, and rigsmith does not go and get it: the
record you chase is the copy you own. A file is what the song map is built
from; the lookup is for metadata, research leads, and a first listen.

Every request goes through :func:`rigsmith.core.appsettings.require_online`,
so none of this happens in local mode.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..core.errors import Unavailable

ITUNES = "https://itunes.apple.com/search"
MUSICBRAINZ = "https://musicbrainz.org/ws/2"
USER_AGENT = "rigsmith/0.1 (https://github.com/Gavinboley/rigsmith)"


@dataclass
class SongMatch:
    title: str
    artist: str
    album: str | None = None
    year: int | None = None
    genre: str | None = None
    duration_s: float | None = None
    artwork_url: str | None = None
    preview_url: str | None = None
    store_url: str | None = None
    source: str = "itunes"
    mbid: str | None = None
    credits: list[dict] = field(default_factory=list)
    attribution: str = "Preview provided courtesy of iTunes"

    def describe(self) -> str:
        bits = [f"{self.title} - {self.artist}"]
        if self.album:
            bits.append(self.album)
        if self.year:
            bits.append(str(self.year))
        return "  |  ".join(bits)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["describe"] = self.describe()
        return data


def _client():
    import httpx

    return httpx.Client(timeout=10.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)


def search(query: str, *, limit: int = 10) -> list[SongMatch]:
    """Songs matching free text - "going under evanescence"."""
    from ..core.appsettings import require_online

    require_online("search for a song online")
    try:
        with _client() as client:
            response = client.get(ITUNES, params={"term": query, "media": "music", "entity": "song", "limit": limit})
            response.raise_for_status()
            results = response.json().get("results", [])
    except Exception as exc:  # network errors surface as one plain message
        raise Unavailable(f"the song lookup failed: {exc}", hint="Check the internet connection, or open a file you have.") from exc
    found = []
    for item in results:
        year = None
        released = item.get("releaseDate")
        if released and released[:4].isdigit():
            year = int(released[:4])
        found.append(
            SongMatch(
                title=item.get("trackName", ""),
                artist=item.get("artistName", ""),
                album=item.get("collectionName"),
                year=year,
                genre=item.get("primaryGenreName"),
                duration_s=(item.get("trackTimeMillis") or 0) / 1000 or None,
                artwork_url=(item.get("artworkUrl100") or "").replace("100x100", "600x600") or None,
                preview_url=item.get("previewUrl"),
                store_url=item.get("trackViewUrl"),
            )
        )
    return found


def credits(title: str, artist: str) -> dict:
    """Recording credits from MusicBrainz: performers, instruments, releases."""
    from ..core.appsettings import require_online

    require_online("look up recording credits")
    query = f'recording:"{title}" AND artist:"{artist}"'
    try:
        with _client() as client:
            response = client.get(f"{MUSICBRAINZ}/recording", params={"query": query, "fmt": "json", "limit": 3})
            response.raise_for_status()
            recordings = response.json().get("recordings", [])
            if not recordings:
                return {"found": False}
            best = recordings[0]
            detail = client.get(
                f"{MUSICBRAINZ}/recording/{best['id']}",
                params={"inc": "artist-rels+releases+artist-credits", "fmt": "json"},
            )
            detail.raise_for_status()
            data = detail.json()
    except Exception as exc:
        raise Unavailable(f"the MusicBrainz lookup failed: {exc}") from exc
    performers = []
    for relation in data.get("relations", []):
        if relation.get("target-type") != "artist":
            continue
        performers.append(
            {
                "name": (relation.get("artist") or {}).get("name"),
                "role": relation.get("type"),
                "instruments": relation.get("attributes", []),
            }
        )
    releases = [
        {"title": r.get("title"), "date": r.get("date"), "country": r.get("country")}
        for r in data.get("releases", [])[:10]
    ]
    return {
        "found": True,
        "mbid": data.get("id"),
        "title": data.get("title"),
        "length_s": (data.get("length") or 0) / 1000 or None,
        "performers": performers,
        "guitarists": [p for p in performers if any("guitar" in str(i).lower() for i in p["instruments"])],
        "releases": releases,
        "source": f"https://musicbrainz.org/recording/{data.get('id')}",
    }


def fetch_preview(match: SongMatch, destination) -> str:
    """Bring a 30-second preview into the workspace for analysis."""
    from pathlib import Path

    from ..core.appsettings import load, require_online

    require_online("download a song preview")
    if not load().allow_preview_downloads:
        raise Unavailable(
            "preview downloads are turned off",
            hint="Turn on 'allow preview downloads' in Settings, or open your own copy of the song.",
        )
    if not match.preview_url:
        raise Unavailable("this match has no preview")
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _client() as client:
            response = client.get(match.preview_url)
            response.raise_for_status()
            path.write_bytes(response.content)
    except Exception as exc:
        raise Unavailable(f"the preview download failed: {exc}") from exc
    return str(path)

"""Songs from the page: create, map, choose parts, render part DIs."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...core import songs
from ...core.jobs import get_manager
from ..api import _allow_audio, _audio_url
from ..plain import PlainRoute

router = APIRouter(prefix="/api/songs", route_class=PlainRoute)


class SongBody(BaseModel):
    title: str
    artist: str | None = None
    album: str | None = None
    year: int | None = None
    mix: str | None = None
    stem: str | None = None
    lookup: bool = False


class AudioBody(BaseModel):
    mix: str | None = None
    stem: str | None = None


class ChooseBody(BaseModel):
    part_ids: list[str]
    chosen: bool = True
    only: bool = False


class RenameBody(BaseModel):
    name: str


class PartDiBody(BaseModel):
    profile_id: str
    midi: str | None = None
    track: int | None = None
    notes: str | None = None
    power_chords: bool = False
    like: str | None = None
    amount: float = 1.0


def _payload(song) -> dict:
    data = song.to_dict()
    urls = {}
    for key in ("mix", "stem"):
        if getattr(song, key):
            _allow_audio(getattr(song, key))
            urls[key] = _audio_url(getattr(song, key))
    for layer, path in (song.layer_files or {}).items():
        if path:
            urls[f"layer-{layer}"] = _audio_url(path)
    data["urls"] = urls
    # Whose playing each part's DI is - autopilot uses yours over a rendering.
    from ...core import takes
    from ...core.errors import NotFound

    for part in data.get("parts", []):
        if not part.get("di_take"):
            continue
        try:
            take = takes.get(part["di_take"])
        except NotFound:
            continue
        _allow_audio(take.path)
        part["di"] = {
            "kind": "rendered" if take.origin == "synth" else "yours",
            "take_id": take.take_id,
            "url": _audio_url(take.path),
            "guitar": take.guitar,
        }
    return data


@router.get("")
def list_songs() -> dict:
    return {"songs": [{**s.to_dict(), "parts": len(s.parts), "chosen": len(s.chosen())} for s in songs.list_songs()]}


@router.get("/local")
def local(title: str, artist: str | None = None) -> dict:
    """Your own copy of a song, found in the music library by name."""
    matches = songs.find_local(title, artist)
    for match in matches:
        _allow_audio(match["path"])
    return {"matches": matches, "roots": [str(r) for r in songs.music_dirs()]}


@router.get("/search")
def search(q: str) -> dict:
    return {"matches": [m.to_dict() for m in songs.lookup(q)]}


@router.post("")
def create(body: SongBody) -> dict:
    for value in (body.mix, body.stem):
        if value:
            _allow_audio(value)
    song = songs.create(body.title, artist=body.artist, album=body.album, year=body.year, mix=body.mix, stem=body.stem)
    if body.lookup:
        try:
            song = songs.enrich(song.song_id)
        except Exception as exc:  # the lookup is a bonus
            song.notes.append(f"lookup failed: {exc}")
    return _payload(song)


@router.get("/{song_id}")
def get_song(song_id: str) -> dict:
    return _payload(songs.get(song_id))


@router.delete("/{song_id}")
def delete_song(song_id: str) -> dict:
    return {"message": songs.delete(song_id)}


@router.post("/{song_id}/audio")
def attach(song_id: str, body: AudioBody) -> dict:
    for value in (body.mix, body.stem):
        if value:
            _allow_audio(value)
    return _payload(songs.attach(song_id, mix=body.mix, stem=body.stem))


@router.post("/{song_id}/lookup")
def lookup(song_id: str) -> dict:
    return _payload(songs.enrich(song_id))


@router.post("/{song_id}/preview")
def preview(song_id: str) -> dict:
    return _payload(songs.preview(song_id))


@router.post("/{song_id}/isolate")
def isolate(song_id: str) -> dict:
    job = get_manager().submit(f"isolate {song_id}", lambda progress: _payload(songs.isolate(song_id, progress=progress)))
    return {"job_id": job.id, "name": job.name}


@router.post("/{song_id}/analyse")
def analyse(song_id: str) -> dict:
    job = get_manager().submit(f"map {song_id}", lambda progress: _payload(songs.analyse(song_id, progress=progress)))
    return {"job_id": job.id, "name": job.name}


@router.post("/{song_id}/choose")
def choose(song_id: str, body: ChooseBody) -> dict:
    return _payload(songs.choose(song_id, body.part_ids, chosen=body.chosen, only=body.only))


@router.post("/{song_id}/sections/{index}/rename")
def rename_section(song_id: str, index: int, body: RenameBody) -> dict:
    return _payload(songs.rename_section(song_id, index, body.name))


class TempoBody(BaseModel):
    bpm: float | None = None
    factor: float | None = None


@router.post("/{song_id}/tempo")
def set_tempo(song_id: str, body: TempoBody) -> dict:
    return _payload(songs.set_tempo(song_id, body.bpm, factor=body.factor))


@router.get("/{song_id}/parts/{part_id}/audio")
def part_audio(song_id: str, part_id: str) -> dict:
    path = songs.part_audio(song_id, part_id)
    return {"path": str(path), "url": _audio_url(path)}


@router.post("/{song_id}/parts/{part_id}/di")
def part_di(song_id: str, part_id: str, body: PartDiBody) -> dict:
    if body.midi:
        _allow_audio(body.midi)

    def run(progress):
        result = songs.render_part_di(
            song_id, part_id, None if body.profile_id == "synthetic" else body.profile_id,
            midi=body.midi, track=body.track, text=body.notes, power_chords=body.power_chords,
            like=body.like or None, amount=body.amount, progress=progress,
        )
        return {**result.to_dict(), "url": _audio_url(result.path)}

    job = get_manager().submit(f"render {part_id} on {body.profile_id}", run)
    return {"job_id": job.id, "name": job.name}


class TakeBody(BaseModel):
    take: str


@router.post("/{song_id}/parts/{part_id}/take")
def part_take(song_id: str, part_id: str, body: TakeBody) -> dict:
    """Your recording of the part, aligned to it and made its DI when it fits."""
    if "/" in body.take or "\\" in body.take:
        _allow_audio(body.take)
    result = songs.take_for_part(song_id, part_id, body.take)
    if result.get("path"):
        _allow_audio(result["path"])
        result["url"] = _audio_url(result["path"])
    return result


@router.get("/{song_id}/brief")
def brief(song_id: str, part_id: str | None = None) -> dict:
    return {"brief": songs.brief(song_id, part_id)}

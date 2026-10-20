"""Autopilot runs from the page."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ...core import autopilot
from ..api import _allow_audio, _audio_url
from ..plain import PlainRoute

router = APIRouter(prefix="/api/autopilot", route_class=PlainRoute)


class RunBody(BaseModel):
    title: str = ""
    artist: str | None = None
    mix: str | None = None
    stem: str | None = None
    song_id: str | None = None
    parts: list[str] = []
    profile_id: str | None = None
    di: str | None = None
    midi: str | None = None
    midi_track: int | None = None
    bench: str = "virtual"
    route_id: str | None = None
    candidates: int = 5
    tune_budget: int = 24
    research: bool = True
    like: str | None = None
    like_amount: float = 1.0
    match_guitar: bool = True
    use_part_take: bool = True
    ear_habits: bool = False


def _payload(run) -> dict:
    data = run.to_dict()
    for result in data.get("results", []):
        if result.get("path"):
            result["url"] = _audio_url(result["path"])
    if run.target_path:
        data["target_url"] = _audio_url(run.target_path)
    if run.di_path:
        data["di_url"] = _audio_url(run.di_path)
        data["has_notes"] = Path(run.di_path).with_suffix(".notes.json").is_file()
    data["notes"] = run.all_notes()
    return data


@router.get("")
def list_runs() -> dict:
    return {"runs": [_payload(r) for r in autopilot.list_runs()[:30]], "stages": list(autopilot.STAGES)}


@router.get("/habits")
def ear_habits(bench: str = "device") -> dict:
    """The knobs you keep turning the same way after autopilot, on ``bench``."""
    from ...core import habits

    return {
        "bench": bench,
        "habits": [h.to_dict() for h in habits.habits(bench)],
        "rigs": sum(1 for where, _ in habits.turns() if where == bench),
        "needs": habits.MIN_RIGS,
    }


@router.post("")
def start(body: RunBody) -> dict:
    for value in (body.mix, body.stem, body.di, body.midi):
        if value:
            _allow_audio(value)
    run = autopilot.start(autopilot.Request(**body.model_dump()))
    return _payload(run)


class DemoBody(BaseModel):
    profile_id: str | None = None
    bench: str = "virtual"
    route_id: str | None = None


@router.post("/demo")
def start_demo(body: DemoBody) -> dict:
    """Autopilot on the demo song - made the first time - whose answer is known."""
    from ...core import demo

    return _payload(demo.start(profile_id=body.profile_id, bench=body.bench, route_id=body.route_id))


class SongRigBody(BaseModel):
    run_ids: list[str]
    name: str | None = None


@router.post("/song-rig")
def build_song_rig(body: SongRigBody) -> dict:
    """One rig with a scene per part, from finished runs of one song."""
    result = autopilot.song_rig(body.run_ids, name=body.name)
    return {**result, "render": autopilot.render_song_rig(result)}


@router.get("/{run_id}")
def get_run(run_id: str) -> dict:
    return _payload(autopilot.get(run_id))


def _run_notes(run_id: str) -> dict:
    from ...core.errors import NotFound
    from ...core.parts import notes_beside

    run = autopilot.get(run_id)
    notes = notes_beside(run.di_path) if run.di_path else None
    if notes is None:
        raise NotFound("this run's DI has no note list", hint="A DI you played yourself has none - only one rendered from the part's notes does.")
    return notes


@router.get("/{run_id}/notes")
def run_notes(run_id: str) -> dict:
    """The notes the run's DI plays - the view a part's DI has."""
    notes = _run_notes(run_id)
    midi = notes.pop("midi_path")
    notes["midi_url"] = f"/api/autopilot/{quote(run_id)}/notes.mid" if midi else None
    notes["duration"] = round(max((e["start"] + e["duration"] for e in notes["events"]), default=0.0), 3)
    return notes


@router.get("/{run_id}/notes.mid")
def run_notes_midi(run_id: str) -> FileResponse:
    from ...core.errors import NotFound

    midi = _run_notes(run_id)["midi_path"]
    if not midi:
        raise NotFound("no MIDI was kept with this run's DI")
    return FileResponse(midi, media_type="audio/midi", filename=f"{run_id}.mid")


@router.post("/{run_id}/resume")
def resume(run_id: str, from_stage: str | None = None) -> dict:
    return _payload(autopilot.resume(run_id, from_stage=from_stage))


@router.post("/{run_id}/listen")
def listen(run_id: str) -> dict:
    """Apply the run's blind A/B: the draft follows the ears' pick."""
    return _payload(autopilot.listen_back(run_id))


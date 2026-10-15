"""Autopilot runs from the page."""

from __future__ import annotations

from fastapi import APIRouter
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


def _payload(run) -> dict:
    data = run.to_dict()
    for result in data.get("results", []):
        if result.get("path"):
            result["url"] = _audio_url(result["path"])
    if run.target_path:
        data["target_url"] = _audio_url(run.target_path)
    if run.di_path:
        data["di_url"] = _audio_url(run.di_path)
    data["notes"] = run.all_notes()
    return data


@router.get("")
def list_runs() -> dict:
    return {"runs": [_payload(r) for r in autopilot.list_runs()[:30]], "stages": list(autopilot.STAGES)}


@router.post("")
def start(body: RunBody) -> dict:
    for value in (body.mix, body.stem, body.di, body.midi):
        if value:
            _allow_audio(value)
    run = autopilot.start(autopilot.Request(**body.model_dump()))
    return _payload(run)


@router.get("/{run_id}")
def get_run(run_id: str) -> dict:
    return _payload(autopilot.get(run_id))


@router.post("/{run_id}/resume")
def resume(run_id: str, from_stage: str | None = None) -> dict:
    return _payload(autopilot.resume(run_id, from_stage=from_stage))

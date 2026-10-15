"""Tuning runs from the page."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...core import tuning
from ...core.jobs import get_manager
from ..api import _allow_audio, _audio_url
from ..plain import PlainRoute

router = APIRouter(prefix="/api/tune", route_class=PlainRoute)


class TuneBody(BaseModel):
    target: str
    di: str | None = None
    region: str | None = None
    renderer: str = "virtual"
    route_id: str | None = None
    draft_id: str | None = None
    knobs: list[str] | None = None
    budget: int = 30
    method: str = "auto"
    mode: str = "distribution"
    song_id: str | None = None
    part_id: str | None = None


class ApplyBody(BaseModel):
    draft_id: str
    with_eq: bool = False


def _payload(run) -> dict:
    data = run.to_dict()
    if run.best_audio:
        data["best_url"] = _audio_url(run.best_audio)
    _allow_audio(run.target)
    data["target_url"] = _audio_url(run.target)
    return data


@router.post("")
def start(body: TuneBody) -> dict:
    for value in (body.target, body.di):
        if value and ("/" in value or "\\" in value):
            _allow_audio(value)
    song = {"song_id": body.song_id, "part_id": body.part_id} if body.song_id and body.part_id else None
    job = get_manager().submit(
        "tune a rig",
        lambda progress: _payload(
            tuning.tune(
                body.target, body.di, region=body.region, renderer=body.renderer, route_id=body.route_id,
                draft_id=body.draft_id, knobs=body.knobs, budget=body.budget, method=body.method,
                mode=body.mode, song=song, progress=progress,
            )
        ),
    )
    return {"job_id": job.id, "name": job.name}


@router.get("/runs")
def runs() -> dict:
    return {"runs": [_payload(r) for r in tuning.list_runs()[:40]]}


@router.get("/runs/{run_id}")
def run(run_id: str) -> dict:
    return _payload(tuning.get_run(run_id))


@router.post("/runs/{run_id}/apply")
def apply(run_id: str, body: ApplyBody) -> dict:
    return {"message": tuning.apply(run_id, body.draft_id, with_eq=body.with_eq)}

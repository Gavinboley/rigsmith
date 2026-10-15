"""Guitar profiles: learn the instrument, play parts on it."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ...core import profiles
from ...core.jobs import get_manager
from ..api import _allow_audio, _audio_url
from ..plain import PlainRoute

router = APIRouter(prefix="/api/learn", route_class=PlainRoute)


class ProfileBody(BaseModel):
    name: str
    guitar: str | None = None
    pickup: str | None = None
    tuning: str | None = None
    note: str | None = None


class ProfileEdit(BaseModel):
    guitar: str | None = None
    pickup: str | None = None
    tuning: str | None = None
    note: str | None = None


class LearnBody(BaseModel):
    source: str
    protocol_id: str = "free"
    frets: int | None = None
    sensitivity: float = 1.0


class PlayBody(BaseModel):
    notes: str
    label: str = "riff"
    noise: bool = False
    like: str | None = None
    amount: float = 1.0


class SwapBody(BaseModel):
    source: str
    like: str
    amount: float = 1.0
    profile_id: str | None = None
    label: str | None = None
    file_as_take: bool = False


class NoteBody(BaseModel):
    midi: float
    string: int | None = None
    velocity: float = 0.75


class PartBody(BaseModel):
    stem: str | None = None
    midi: str | None = None
    track: int | None = None
    notes: str | None = None
    region: str | None = None
    mode: str = "auto"
    power_chords: bool = False
    transpose: int = 0
    label: str | None = None
    file_as_take: bool = True


def _profile_id(value: str) -> str | None:
    return None if value in ("synthetic", "", None) else value


@router.get("/profiles")
def list_profiles() -> dict:
    found = profiles.list_profiles()
    return {"profiles": [p.summary() for p in found], "rendered": profiles.render_list(found)}


@router.post("/profiles")
def create_profile(body: ProfileBody) -> dict:
    return profiles.create(body.name, guitar=body.guitar, pickup=body.pickup, tuning=body.tuning, note=body.note).summary()


@router.get("/profiles/{profile_id}")
def get_profile(profile_id: str, samples: bool = False) -> dict:
    profile = profiles.get(profile_id)
    data = profile.to_dict() if samples else profile.summary()
    data["coverage"] = profile.coverage(frets=24 if samples else 12)
    resonance = profile.resonance()
    data["resonance"] = resonance.to_dict() if resonance else None
    return data


@router.patch("/profiles/{profile_id}")
def edit_profile(profile_id: str, body: ProfileEdit) -> dict:
    return profiles.update(profile_id, **body.model_dump()).summary()


@router.delete("/profiles/{profile_id}")
def delete_profile(profile_id: str) -> dict:
    return {"message": profiles.delete(profile_id)}


@router.delete("/profiles/{profile_id}/samples/{sample_id}")
def remove_sample(profile_id: str, sample_id: str) -> dict:
    return profiles.remove_sample(profile_id, sample_id).summary()


@router.get("/profiles/{profile_id}/samples/{sample_id}/audio")
def sample_audio(profile_id: str, sample_id: str) -> dict:
    profile = profiles.get(profile_id)
    sample = next((s for s in profile.samples if s.sample_id == sample_id), None)
    if sample is None:
        from ...core.errors import NotFound

        raise NotFound(f"no sample '{sample_id}'")
    path = profiles.sample_dir(profile.profile_id) / sample.file
    return {"url": _audio_url(path), "sample": sample.to_dict()}


@router.post("/profiles/{profile_id}/learn")
def learn(profile_id: str, body: LearnBody) -> dict:
    if "/" in body.source or "\\" in body.source:
        _allow_audio(body.source)
    job = get_manager().submit(
        f"learn {body.source} into {profile_id}",
        lambda progress: profiles.learn(
            profile_id, body.source, protocol_id=body.protocol_id, frets=body.frets,
            sensitivity=body.sensitivity, progress=progress,
        ).to_dict(),
    )
    return {"job_id": job.id, "name": job.name}


@router.post("/profiles/{profile_id}/voice")
def refit_voice(profile_id: str) -> dict:
    model = profiles.refit_voice(profile_id)
    return {"notes": len(model.notes)}


@router.post("/profiles/{profile_id}/play")
def play(profile_id: str, body: PlayBody) -> dict:
    path, rendered = profiles.render_text(_profile_id(profile_id), body.notes, label=body.label, noise=body.noise, like=body.like or None, amount=body.amount)
    return {**rendered.to_dict(), "path": str(path), "url": _audio_url(path)}


@router.post("/profiles/{profile_id}/note")
def note(profile_id: str, body: NoteBody) -> dict:
    path, rendered = profiles.preview_note(_profile_id(profile_id), body.midi, string=body.string, velocity=body.velocity)
    return {**rendered.to_dict(), "path": str(path), "url": _audio_url(path)}


@router.post("/profiles/{profile_id}/part")
def part(profile_id: str, body: PartBody) -> dict:
    from ...core import parts

    for value in (body.stem, body.midi):
        if value:
            _allow_audio(value)

    def run(progress):
        result = parts.part_di(
            _profile_id(profile_id), stem=body.stem, midi=body.midi, track=body.track, text=body.notes,
            region=body.region, mode=body.mode, power_chords=body.power_chords, transpose=body.transpose,
            label=body.label, file_as_take=body.file_as_take, progress=progress,
        )
        _allow_audio(result.path)
        return {**result.to_dict(), "url": _audio_url(result.path)}

    job = get_manager().submit(f"render part on {profile_id}", run)
    return {"job_id": job.id, "name": job.name}


@router.get("/protocols")
def protocols(tuning: str | None = None) -> dict:
    from ...learn import protocols as protocol_module

    return {"protocols": [p.to_dict() for p in protocol_module.available(tuning)]}


@router.get("/protocols/{protocol_id}")
def protocol(protocol_id: str, tuning: str | None = None, frets: int | None = None, click: bool = False) -> dict:
    from ...learn import protocols as protocol_module

    data: dict[str, Any] = protocol_module.get(protocol_id, tuning=tuning, frets=frets).to_dict()
    if click and protocol_id != "free":
        path, _, count_in = profiles.click_track(protocol_id, tuning=tuning, frets=frets)
        data.update(click_url=_audio_url(path), count_in=count_in)
    return data


@router.get("/tunings")
def tunings() -> dict:
    from ...learn.instrument import tunings as all_tunings

    return {"tunings": [t.to_dict() for t in all_tunings()]}


@router.post("/demo")
def demo(protocol_id: str = "quick", into: str | None = None) -> dict:
    path, protocol_ = profiles.demo_take(protocol_id)
    result: dict[str, Any] = {"path": str(path), "url": _audio_url(path), "protocol": protocol_.protocol_id}
    if into:
        from ...core.errors import NotFound

        try:
            profiles.get(into)
        except NotFound:
            profiles.create(into, guitar="Synthetic demo", note="made by the demo - not a real guitar")
        result["report"] = profiles.learn(into, str(path), protocol_id=protocol_id).to_dict()
    return result


@router.get("/analyse")
def analyse(source: str, protocol_id: str = "free", tuning: str | None = None) -> dict:
    if "/" in source or "\\" in source:
        _allow_audio(source)
    return profiles.analyse(source, protocol_id=protocol_id, tuning=tuning)


@router.get("/pickups")
def pickups() -> dict:
    """The pickup types a DI can be moved toward, and every learned guitar's own."""
    from ...learn.pickup import PICKUPS

    guitars = []
    for profile in profiles.list_profiles():
        resonance = profile.resonance()
        if resonance:
            guitars.append({"profile_id": profile.profile_id, "label": profile.label, **resonance.to_dict()})
    return {
        "types": [{"name": p.name, "f0_hz": p.f0_hz, "q": p.q, "description": p.description} for p in PICKUPS.values()],
        "guitars": guitars,
    }


@router.get("/pickup")
def pickup_of(source: str) -> dict:
    if "/" in source or "\\" in source:
        _allow_audio(source)
    return profiles.pickup_of(source).to_dict()


@router.post("/swap")
def swap(body: SwapBody) -> dict:
    if "/" in body.source or "\\" in body.source:
        _allow_audio(body.source)
    result = profiles.swap_di(
        body.source, body.like, amount=body.amount, profile_id=_profile_id(body.profile_id or ""),
        label=body.label, file_as_take=body.file_as_take,
    )
    _allow_audio(result["path"])
    return {**result, "url": _audio_url(result["path"])}


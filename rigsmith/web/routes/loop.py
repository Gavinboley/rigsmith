"""The hardware loop from the page: routes, calibration, reamps, meters."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from ...core import loop
from ...core.jobs import get_manager
from ..api import _allow_audio, _audio_url
from ..plain import PlainRoute

router = APIRouter(prefix="/api/loop", route_class=PlainRoute)


class RouteBody(BaseModel):
    route_id: str
    kind: str = "analog"
    output_device: str
    input_device: str | None = None
    output_channels: list[int] | None = None
    input_channels: list[int] | None = None
    reference_device: str | None = None
    reference_channel: int | None = None
    sample_rate: int = 48000
    hostapi: str | None = None
    backend: str = "sounddevice"
    note: str | None = None
    make_default: bool = False


class CalibrateBody(BaseModel):
    route_id: str | None = None
    di: str | None = None
    profile_id: str | None = None
    direct: str | None = None
    direct_is_reference: bool = False
    apply: bool = True


class ReampBody(BaseModel):
    di: str
    route_id: str | None = None
    label: str = "loop"
    file_as_take: bool = True


class RecordBody(BaseModel):
    seconds: float
    device: str
    channel: int = 1
    take_id: str | None = None
    guitar: str | None = None
    pickup: str | None = None
    tuning: str | None = None
    note: str | None = None
    backend: str | None = None


class AssessBody(BaseModel):
    target: str
    capture: str


class MonitorStartBody(BaseModel):
    input_device: str
    input_channel: int = 1
    output_device: str | None = None
    output_channels: list[int] | None = None
    listen: bool = False
    gain_db: float = 0.0
    tuning: str | None = None
    backend: str | None = None


class CheckClipBody(BaseModel):
    device: str
    channel: int = 1
    seconds: float = 3.5
    tuning: str | None = None
    backend: str | None = None


@router.get("/devices")
def devices(backend: str | None = None) -> dict:
    try:
        from ...loop.routes import hardware

        found = loop.devices(backend)
        return {"devices": [d.to_dict() for d in found], "hardware": hardware(found), "error": None}
    except Exception as exc:  # no audio stack is a state to show, not a failure
        return {"devices": [], "hardware": [], "error": str(exc)}


@router.get("/suggest")
def suggest(backend: str | None = None) -> dict:
    try:
        return {"routes": [r.to_dict() for r in loop.suggest_routes(backend)]}
    except Exception as exc:
        return {"routes": [], "error": str(exc)}


@router.get("/routes")
def routes() -> dict:
    found = loop.list_routes()
    result = []
    for route in found:
        data = route.to_dict()
        calibration = loop.calibration(route.route_id)
        data["calibration"] = calibration.to_dict() if calibration else None
        result.append(data)
    return {"routes": result, "default": loop.store().default_id()}


@router.post("/routes")
def create_route(body: RouteBody) -> dict:
    values = body.model_dump()
    route_id = values.pop("route_id")
    return loop.create_route(route_id, **values).to_dict()


@router.post("/routes/adopt/{route_id}")
def adopt(route_id: str, backend: str | None = None) -> dict:
    return loop.adopt_suggestion(route_id, backend=backend).to_dict()


@router.post("/routes/{route_id}/default")
def make_default(route_id: str) -> dict:
    loop.set_default(route_id)
    return {"default": route_id}


@router.delete("/routes/{route_id}")
def delete_route(route_id: str) -> dict:
    loop.delete_route(route_id)
    return {"deleted": route_id}


@router.get("/bench")
def bench() -> dict:
    """Whether the Core's loaded rig has an Amp and a Cab to switch (read only)."""
    from ...core import tuning

    return tuning.bench_check().to_dict()


@router.post("/bench-rig")
def bench_rig() -> dict:
    from ...core import tuning

    draft_id = tuning.bench_rig_draft()
    return {"draft_id": draft_id, "message": f"Draft '{draft_id}' has one amp and one cab - write it from the Rig panel."}


@router.post("/demo")
def demo() -> dict:
    return {"routes": [r.to_dict() for r in loop.demo_bench()]}


@router.post("/tone")
def tone(route_id: str | None = None, frequency: float = 440.0, level_db: float = -18.0) -> dict:
    return loop.test_tone(route_id, frequency=frequency, level_db=level_db)


@router.get("/level")
def level(device: str, channel: int = 1, seconds: float = 3.0, backend: str | None = None) -> dict:
    """Listen to a guitar input for a few seconds and judge the level. Saves nothing."""
    return loop.input_level(device, channel=channel, seconds=seconds, backend=backend)


@router.get("/tuner")
def tuner(device: str, channel: int = 1, seconds: float = 0.35, tuning: str | None = None, backend: str | None = None) -> dict:
    """Single-shot or monitor-backed tuner + gain-staging probe."""
    return loop.tuner_probe(device, channel=channel, seconds=seconds, tuning=tuning, backend=backend)


@router.post("/monitor/start")
def monitor_start(body: MonitorStartBody) -> dict:
    """Start or update live low-latency input monitoring + optional output pass-through."""
    return loop.start_monitor(
        input_device=body.input_device,
        input_channel=body.input_channel,
        output_device=body.output_device,
        output_channels=body.output_channels,
        listen=body.listen,
        gain_db=body.gain_db,
        tuning=body.tuning,
        backend=body.backend,
    )


@router.post("/monitor/stop")
def monitor_stop() -> dict:
    """Stop live monitoring."""
    return loop.stop_monitor()


@router.get("/monitor")
def monitor_status(tuning: str | None = None, reset_peak: bool = False) -> dict:
    """Read live tuner and gain staging telemetry from the active monitor."""
    return loop.monitor_status(tuning=tuning, reset_peak=reset_peak)


@router.post("/check-clip")
def check_clip(body: CheckClipBody) -> dict:
    """Record a short DI check clip, analyze gain staging + pitch, and return a playable audio URL."""
    result = loop.check_clip(
        body.device,
        channel=body.channel,
        seconds=body.seconds,
        tuning=body.tuning,
        backend=body.backend,
    )
    return {**result, "url": _audio_url(result["path"])}


@router.get("/meter")
def meter(route_id: str | None = None) -> dict:
    return loop.meter(route_id)


@router.post("/calibrate")
def calibrate(body: CalibrateBody) -> dict:
    job = get_manager().submit(
        "calibrate the loop",
        lambda progress: loop.calibrate(
            body.route_id, di=body.di, profile_id=body.profile_id, direct=body.direct,
            direct_is_reference=body.direct_is_reference, apply=body.apply, progress=progress,
        ).to_dict(),
    )
    return {"job_id": job.id, "name": job.name}


@router.post("/reamp")
def reamp(body: ReampBody) -> dict:
    if "/" in body.di or "\\" in body.di:
        _allow_audio(body.di)

    def run(progress):
        result = loop.reamp(body.di, route_id=body.route_id, label=body.label, file_as_take=body.file_as_take, progress=progress)
        return {**result.to_dict(), "url": _audio_url(result.path)}

    job = get_manager().submit(f"reamp {body.label}", run)
    return {"job_id": job.id, "name": job.name}


@router.post("/record")
def record(body: RecordBody) -> dict:
    def run(progress):
        take = loop.record_input(
            body.seconds, device=body.device, channel=body.channel, take_id=body.take_id,
            guitar=body.guitar, pickup=body.pickup, tuning=body.tuning, note=body.note,
            backend=body.backend, progress=progress,
        )
        return {**take.to_dict(), "url": _audio_url(take.path)}

    job = get_manager().submit(f"record {body.seconds:.0f}s from {body.device}", run)
    return {"job_id": job.id, "name": job.name}


@router.post("/assess")
def assess(body: AssessBody) -> dict:
    for value in (body.target, body.capture):
        if "/" in value or "\\" in value:
            _allow_audio(value)
    return loop.assess(body.target, body.capture).to_dict()

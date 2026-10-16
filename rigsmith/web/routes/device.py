"""Pairing with the Core, and reading what is on it, from the page."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from ...core import devices
from ...core.jobs import get_manager
from ..plain import PlainRoute

router = APIRouter(prefix="/api/device", route_class=PlainRoute)


class PairBody(BaseModel):
    host: str | None = None
    timeout: float = 120.0


class HostBody(BaseModel):
    host: str | None = None


@router.get("/sessions")
def sessions() -> dict:
    found = devices.sessions()
    return {"sessions": found, "render": devices.render_sessions(found)}


@router.post("/pair")
def pair(body: PairBody) -> dict:
    """Start pairing. The PIN arrives as the job's message - the person at the
    Core has to see it before the wait starts, so it cannot be the result."""

    def run(progress):
        progress.update(0.05, "asking the device for a PIN")

        def show(pin: str) -> None:
            progress.update(0.3, f"PIN {pin} - accept it on the device")

        session = asyncio.run(devices.pair(body.host, timeout=body.timeout, on_pin=show))
        return {"host": session.host, "server": session.server, "message": f"Paired with {session.host}."}

    job = get_manager().submit("pair with the Core", run)
    return {"job_id": job.id, "name": job.name}


@router.post("/forget")
def forget(body: HostBody) -> dict:
    if not body.host:
        from ...core.errors import Invalid

        raise Invalid("say which device to forget")
    return {"forgotten": devices.forget(body.host)}


@router.post("/snapshot")
def snapshot(body: HostBody) -> dict:
    def run(progress):
        progress.update(0.1, "reading the device")
        taken, path = asyncio.run(devices.snapshot(body.host))
        return {**taken.to_dict(), "render": taken.render(), "path": str(path)}

    job = get_manager().submit("snapshot the device", run)
    return {"job_id": job.id, "name": job.name}


class ScenesBody(BaseModel):
    host: str | None = None
    learn: bool = False


@router.post("/scenes")
def scenes(body: ScenesBody) -> dict:
    """The loaded rig's scene table, raw - read only on the device. With
    ``learn``, footswitch 1's scene teaches the on and off numbers."""

    def run(progress):
        from ...core import appsettings

        progress.update(0.1, "reading the scenes")
        rows = asyncio.run(devices.read_scenes(body.host))
        render = devices.render_scenes(rows)
        if body.learn:
            modes = devices.learn_scene_modes(rows)
            appsettings.update(scene_modes=modes)
            render += f"\n\nLearned: on is {modes['on']}, off is {modes['off']}. Rigs written from now on carry their scenes."
        return {"rows": rows, "render": render}

    job = get_manager().submit("read the device's scenes", run)
    return {"job_id": job.id, "name": job.name}


class CalibrateBody(BaseModel):
    host: str | None = None
    blocks: list[str] = []


@router.get("/catalog")
def catalog() -> dict:
    return devices.catalog_status()


@router.post("/catalog/calibrate")
def calibrate(body: CalibrateBody) -> dict:
    """Measure the loaded rig's knob curves. Reads the device; writes only the catalog."""

    def run(progress):
        progress.update(0.1, "reading the loaded rig's blocks")
        return asyncio.run(devices.calibrate_catalog(body.host, body.blocks or None))

    job = get_manager().submit("measure the knob curves", run)
    return {"job_id": job.id, "name": job.name}

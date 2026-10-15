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

"""App settings: mode, models, defaults."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ...core import appsettings
from ..plain import PlainRoute

router = APIRouter(prefix="/api/settings", route_class=PlainRoute)


class SettingsBody(BaseModel):
    mode: str | None = None
    llm: dict[str, Any] | None = None
    local_llm: dict[str, Any] | None = None
    audio_backend: str | None = None
    default_profile: str | None = None
    default_route: str | None = None
    allow_preview_downloads: bool | None = None
    music_dirs: list[str] | None = None
    onboarding_done: bool | None = None


@router.get("")
def get_settings() -> dict:
    return appsettings.load().to_dict()


@router.patch("")
def update(body: SettingsBody) -> dict:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    # An explicit null clears a default; pydantic folds it into "unset", so
    # an empty string is the way the page says "none".
    for key in ("default_profile", "default_route"):
        if changes.get(key) == "":
            changes[key] = None
    return appsettings.update(**changes).to_dict()


@router.get("/models")
def models(base_url: str | None = None) -> dict:
    """What a local server has loaded - for the model picker."""
    from ...agent.providers import OpenAICompatible

    settings = appsettings.load()
    provider = OpenAICompatible(base_url or settings.local_llm.base_url)
    try:
        return {"models": provider.models(quick=True), "reachable": True}
    except Exception as exc:
        return {"models": [], "reachable": False, "error": str(exc).splitlines()[0]}


class ModelCheckBody(BaseModel):
    base_url: str | None = None
    model: str | None = None


@router.post("/model-check")
def model_check(body: ModelCheckBody) -> dict:
    """Score a local model on rigsmith's tools - as a job, since it is a minute of inference."""
    from ...agent import modelcheck
    from ...agent.providers import OpenAICompatible
    from ...core.jobs import get_manager

    llm = appsettings.load().local_llm
    provider = OpenAICompatible(body.base_url or llm.base_url, model=body.model or llm.model, temperature=llm.temperature, thinking=llm.thinking)

    def work(progress):
        return modelcheck.run(provider, progress=progress).to_dict()

    job = get_manager().submit("check the local model", work)
    return {"job_id": job.id, "name": job.name}


@router.get("/setup")
def setup() -> dict:
    """The first-run checklist: what is set up, and what each missing piece buys."""
    from ...core import setup as checklist

    steps = checklist.checklist()
    return {"steps": [s.to_dict() for s in steps], "render": checklist.render(steps), "dismissed": appsettings.load().onboarding_done}


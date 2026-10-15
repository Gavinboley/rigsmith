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


@router.get("/setup")
def setup() -> dict:
    """The first-run checklist: what is set up, and what each missing piece buys."""
    from ...core import setup as checklist

    steps = checklist.checklist()
    return {"steps": [s.to_dict() for s in steps], "render": checklist.render(steps), "dismissed": appsettings.load().onboarding_done}


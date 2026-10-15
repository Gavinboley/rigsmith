"""Tools for tuning a rig's knobs against a target."""

from __future__ import annotations

from typing import Literal

from ...core import tuning
from ...core.jobs import get_manager
from .._shared import tool


@tool
def tune_rig(
    target: str,
    di: str | None = None,
    region: str | None = None,
    renderer: Literal["virtual", "loop"] = "virtual",
    route_id: str | None = None,
    draft_id: str | None = None,
    knobs: list[str] | None = None,
    budget: int = 30,
    mode: Literal["distribution", "performance", "aligned"] = "distribution",
    song_id: str | None = None,
    part_id: str | None = None,
) -> str:
    """Search knob settings that bring a rig closest to a target.

    ``renderer='loop'`` writes each setting live to the Core's loaded patch
    (or the simulated bench) and reamps the DI - every evaluation is the
    device's own output. Use mode='performance' when the DI was rendered from
    the part's own notes (render_song_part_di). Background job; the result
    includes a match-EQ for what the knobs could not close.
    """
    song = {"song_id": song_id, "part_id": part_id} if song_id and part_id else None
    job = get_manager().submit(
        "tune a rig",
        lambda progress: tuning.tune(
            target, di, region=region, renderer=renderer, route_id=route_id, draft_id=draft_id,
            knobs=knobs, budget=budget, mode=mode, song=song, progress=progress,
        ),
    )
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def tuning_runs() -> str:
    """Past tuning runs and how far each got."""
    runs = tuning.list_runs()
    return "\n".join(r.describe().splitlines()[0] for r in runs[:20]) or "No tuning runs yet."


@tool
def apply_tuning(run_id: str, draft_id: str, with_eq: bool = False) -> str:
    """Put a tuning run's best settings into a draft. Writing the draft to the
    device stays the usual gated step - preview, back up, confirm."""
    return tuning.apply(run_id, draft_id, with_eq=with_eq)

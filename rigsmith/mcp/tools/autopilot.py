"""Tools for the autopilot: one song in, one rig out."""

from __future__ import annotations

from typing import Literal

from ...core import autopilot
from .._shared import tool


@tool
def run_autopilot(
    title: str,
    artist: str | None = None,
    mix_path: str | None = None,
    stem_path: str | None = None,
    song_id: str | None = None,
    parts: list[str] | None = None,
    profile_id: str | None = None,
    midi_path: str | None = None,
    bench: Literal["virtual", "device"] = "virtual",
    route_id: str | None = None,
    research: bool = True,
) -> str:
    """Chase a song's guitar tone end to end, in the background.

    Maps the song, picks the part (or uses ``parts``), researches the gear,
    renders the part on the user's guitar, hears candidate amps through the
    bench, tunes the best, and builds a draft rig plus a blind A/B. The
    'virtual' bench rehearses on a simulated amp; 'device' switches the Core's
    amp live and reamps through the loop. Nothing is written to the device.
    Returns a run id; check it with autopilot_status.
    """
    run = autopilot.start(
        autopilot.Request(
            title=title, artist=artist, mix=mix_path, stem=stem_path, song_id=song_id,
            parts=list(parts or []), profile_id=profile_id, midi=midi_path, bench=bench,
            route_id=route_id, research=research,
        )
    )
    return f"Started autopilot {run.run_id}. Check it with autopilot_status('{run.run_id}')."


@tool
def autopilot_status(run_id: str | None = None) -> str:
    """One autopilot run stage by stage, or the recent runs if no id is given."""
    if run_id:
        return autopilot.get(run_id).describe()
    runs = autopilot.list_runs()[:10]
    return "\n".join(r.describe().splitlines()[0] for r in runs) or "No autopilot runs yet."

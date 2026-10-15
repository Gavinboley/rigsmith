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
    like: str | None = None,
) -> str:
    """Chase a song's guitar tone end to end, in the background.

    Maps the song, picks the part (or uses ``parts``), researches the gear,
    renders the part on the user's guitar, hears candidate amps through the
    bench, tunes the best, and builds a draft rig plus a blind A/B. The
    'virtual' bench rehearses on a simulated amp; 'device' switches the Core's
    amp live and reamps through the loop. Nothing is written to the device.
    The part DI follows the research's guitar (a Strat record gets a single
    coil) unless ``like`` names one. Returns a run id; check it with
    autopilot_status.
    """
    run = autopilot.start(
        autopilot.Request(
            title=title, artist=artist, mix=mix_path, stem=stem_path, song_id=song_id,
            parts=list(parts or []), profile_id=profile_id, midi=midi_path, bench=bench,
            route_id=route_id, research=research, like=like,
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


@tool
def autopilot_listen_back(run_id: str) -> str:
    """Let the run's blind A/B decide the draft, once the user has listened.

    Records every finalist's verdict in the song's recipe as a listening
    measurement, and if the ears' winner is not the score's, rebuilds the
    draft from it. Ask the user to do the blind test first - only their ears
    count here.
    """
    run = autopilot.listen_back(run_id)
    return "\n".join(run.notes[-2:]) or run.describe()


@tool
def resume_autopilot(run_id: str, from_stage: str | None = None) -> str:
    """Pick a stopped or failed autopilot run back up, in the background.

    It carries on from its first unfinished stage, or from ``from_stage``
    (song, stem, map, parts, research, di, candidates, cabs, tune, rig) to
    redo that stage and everything after it - after fixing what stopped it.
    """
    run = autopilot.resume(run_id, from_stage=from_stage)
    return f"resumed {run.run_id} - follow it with autopilot_status"


@tool
def build_song_rig(run_ids: list[str], name: str | None = None) -> str:
    """One rig for several parts of a song, with a scene per part on its own footswitch.

    Give finished autopilot runs of the same song, one per part (up to five).
    The rig carries up to two amp-and-cab sounds; each scene turns on its
    part's sound and the effects that part measures as having. Parts whose
    best sound did not make the two are moved to the nearest one, and the
    result says so. Builds a draft; writing it to the device is separate.
    """
    return autopilot.render_song_rig(autopilot.song_rig(run_ids, name=name))


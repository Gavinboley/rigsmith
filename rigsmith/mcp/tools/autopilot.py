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
    ear_habits: bool = False,
) -> str:
    """Chase a song's guitar tone end to end, in the background.

    Maps the song, picks the part (or uses ``parts``), researches the gear,
    renders the part on the user's guitar, hears candidate amps through the
    bench, tunes the best, and builds a draft rig plus a blind A/B. The
    'virtual' bench rehearses on a simulated amp; 'device' switches the Core's
    amp live and reamps through the loop. Nothing is written to the device.
    The part DI follows the research's guitar (a Strat record gets a single
    coil) unless ``like`` names one. ``ear_habits`` starts the amp from the
    knobs the user keeps turning after autopilot (see ear_habits) - only
    when they ask for it. Returns a run id; check it with autopilot_status.
    """
    run = autopilot.start(
        autopilot.Request(
            title=title, artist=artist, mix=mix_path, stem=stem_path, song_id=song_id,
            parts=list(parts or []), profile_id=profile_id, midi=midi_path, bench=bench,
            route_id=route_id, research=research, like=like, ear_habits=ear_habits,
        )
    )
    return f"Started autopilot {run.run_id}. Check it with autopilot_status('{run.run_id}')."


@tool
def run_autopilot_demo(profile_id: str | None = None, bench: Literal["virtual", "device"] = "virtual", route_id: str | None = None) -> str:
    """Run autopilot on rigsmith's demo song - for someone with no song file
    to hand, or to check the whole chain works.

    The demo is made the first time (a clean intro, a double-tracked rhythm,
    a lead solo, a band under them) through known settings of the simulated
    amp, so a finished run says which voicing it chose and how far each knob
    landed from the real one - autopilot_status shows it as "the demo's
    answer". Returns a run id.
    """
    from ...core import demo

    run = demo.start(profile_id=profile_id, bench=bench, route_id=route_id)
    return f"Started autopilot {run.run_id} on the demo song. Check it with autopilot_status('{run.run_id}')."


@tool
def ear_habits(bench: Literal["virtual", "device"] = "device") -> str:
    """The amp knobs the user keeps turning the same way after autopilot
    builds a rig - their room, monitors or taste rather than any one song -
    learned from what each finished run built and what its draft holds now.
    A run can start from them (run_autopilot ear_habits=True)."""
    from ...core import habits

    found = habits.habits(bench)
    if not found:
        looked = sum(1 for where, _ in habits.turns() if where == bench)
        return f"no habits yet on the {bench} bench ({looked} finished rig(s) looked at; a habit needs {habits.MIN_RIGS} turned the same way)"
    return "\n".join(h.describe() for h in found)


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


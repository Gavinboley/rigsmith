"""Tone sheets for drafts: the rig as you would dial it in, and where it came from.

The rig half is :mod:`rigsmith.rig.sheet`; this adds the history only the
workspace knows - the autopilot run that built the draft, the song and part
it chased, the research behind it, and what still differs from the record.
"""

from __future__ import annotations

from ..rig.catalog import get_catalog
from ..rig.sheet import Sheet, build


def run_for(draft_id: str):
    """The autopilot run that built a draft (or its song rig), if any."""
    from . import autopilot

    for run in autopilot.list_runs():
        if run.draft_id == draft_id or (run.song_rig or {}).get("draft_id") == draft_id:
            return run
    return None


def origin(draft_id: str) -> dict:
    """``{origin, research, remaining, notes}`` for a draft autopilot built; {} otherwise."""
    from . import recipes, songs

    run = run_for(draft_id)
    if run is None:
        return {}
    found: dict[str, list] = {"origin": [], "research": [], "remaining": [], "notes": []}
    song = None
    if run.song_id:
        try:
            song = songs.get(run.song_id)
        except Exception:
            song = None
    found["origin"].append(("Song", run.request.title + (f" - {run.request.artist}" if run.request.artist else "")))
    if (run.song_rig or {}).get("draft_id") == draft_id:
        for scene in run.song_rig["scenes"]:
            found["origin"].append((f"FS{scene['footswitch']}", f"{scene['scene']} - {scene['sound']} (run {scene['run']})"))
        found["notes"].extend(run.song_rig.get("notes") or [])
    else:
        if song is not None and run.part_ids:
            try:
                from .autopilot import _part

                part = _part(song, run)
                found["origin"].append(("Part", part.label + (f" - {part.sketch['summary']}" if (part.sketch or {}).get("summary") else "")))
            except Exception:
                pass
        if run.results:
            chosen = run.ear_pick or run.results[0]["label"]
            best = next((r for r in run.results if r["label"] == chosen), run.results[0])
            why = "your ears in the blind test" if run.ear_pick else "the closest score"
            found["origin"].append(("Chosen", f"{best['label']}, by {why} ({best['composite']:.3f}; lower is closer)"))
            others = [f"{r['label']} ({r['composite']:.3f})" for r in run.results if r["label"] != best["label"]][:3]
            if others:
                found["origin"].append(("Also heard", ", ".join(others)))
        bench = {"device": "on the Core", "virtual": "on the simulated bench - tune it again on the Core"}.get(run.request.bench, run.request.bench)
        found["origin"].append(("Heard", bench))
        for row in sorted((r for r in run.remaining if r.get("audible")), key=lambda r: -abs(r["delta_db"])):
            text = f"{row['band']} {row['delta_db']:+.1f} dB"
            if row.get("meaning"):
                text += f" - {row['meaning']}"
            if row.get("try"):
                text += f"; try {row['try']}"
            found["remaining"].append(text)
        found["notes"].extend(run.all_notes())
    structure = (song.structure if song is not None else None) or {}
    if structure.get("tempo"):
        found["origin"].append(("Song tempo", f"{structure['tempo']:g} BPM" + (", set by hand" if structure.get("tempo_source") == "hand" else ", detected")))
    if run.recipe_id:
        try:
            recipe = recipes.get(run.recipe_id)
        except Exception:
            recipe = None
        if recipe is not None:
            if recipe.summary:
                found["research"].append(recipe.summary)
            if recipe.guitar:
                found["research"].append(f"guitar: {recipe.guitar}" + (f", {recipe.pickup} pickup" if recipe.pickup else "") + (f", {recipe.tuning}" if recipe.tuning else ""))
            for gear in recipe.gear:
                source = f" - {gear.sources[0].title}" if gear.sources else ""
                found["research"].append(f"{gear.role}: {gear.name} ({gear.confidence.value}{source})")
            found["research"].extend(f"open question: {q}" for q in recipe.open_questions)
    return found


def tone_sheet(draft_id: str) -> Sheet:
    """The tone sheet for a draft."""
    from . import rigs

    from ..device.client import _scene_modes

    draft = rigs.get(draft_id)
    return build(draft.rig, get_catalog(), origin=origin(draft_id), scenes_written=_scene_modes() is not None)

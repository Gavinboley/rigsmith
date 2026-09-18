"""Tools for the user's own recordings."""

from __future__ import annotations

from typing import Literal

from ...core import devices, takes
from .._shared import tool

Kind = Literal["di", "wet", "loop", "reference"]
Origin = Literal["di-box", "usb", "looper", "file", "mic"]


@tool
def list_takes(kind: str | None = None, guitar: str | None = None) -> str:
    """List the user's own recordings: DIs, references, loops off the pedal.

    Check this before asking them to record anything. A take id works
    anywhere an audio path does, in every matching tool, so once something is
    here it can be referred to by name.

    Args:
        kind: Filter to 'di', 'wet', 'loop', or 'reference'.
        guitar: Filter to one instrument.
    """
    return takes.render_list(takes.list_takes(kind=kind, guitar=guitar))


@tool
def show_take(take_id: str) -> str:
    """Show one recording: the instrument, what was played, and any concerns.

    The concerns are worth relaying. A take recorded off concert pitch puts
    every harmonic slightly off where the record's sits, which the band
    comparison reads as a tone difference - so it is worth retuning and
    re-recording before spending an evening chasing captures.
    """
    return takes.get(take_id).render()


@tool
def add_take(
    path: str,
    take_id: str | None = None,
    kind: Kind = "di",
    origin: Origin = "file",
    guitar: str | None = None,
    pickup: str | None = None,
    strings: str | None = None,
    tuning: str | None = None,
    note: str | None = None,
) -> str:
    """Add one of the user's recordings to the library.

    Ask which guitar and pickup before calling this, and do not guess. Six
    months later the label is the only thing separating a library from a
    folder of wav files, and a wrong label is worse than a missing one.

    Args:
        kind: 'di' is dry, straight from the guitar, and is the only kind
            that can be rendered through a candidate rig. 'wet' and 'loop'
            have already been through an amp - rendering one through another
            amp stacks two, which sounds like neither.
        origin: Where it was recorded. 'usb' is the Core's own interface,
            'looper' is a loop pulled off the pedal's storage.
        tuning: If they say one, it is checked against the lowest note in the
            recording - and queried rather than corrected, because a low
            string that was never played cannot be heard.
        note: What they played, in their words.
    """
    added = takes.add(
        path,
        take_id=take_id,
        kind=kind,
        origin=origin,
        guitar=guitar,
        pickup=pickup,
        strings=strings,
        tuning=tuning,
        note=note,
    )
    lines = [added.render()]
    if not guitar:
        lines.append(
            "\nNo guitar was recorded for this take. Ask which instrument it "
            "was and set it with edit_take - that label is the reason the "
            "library exists."
        )
    return "\n".join(lines)


@tool
def edit_take(
    take_id: str,
    kind: Kind | None = None,
    guitar: str | None = None,
    pickup: str | None = None,
    strings: str | None = None,
    tuning: str | None = None,
    note: str | None = None,
) -> str:
    """Change a recording's labels. The audio itself is never touched."""
    changes = {
        name: value
        for name, value in {
            "kind": kind, "guitar": guitar, "pickup": pickup,
            "strings": strings, "tuning": tuning, "note": note,
        }.items()
        if value is not None
    }
    if not changes:
        raise ValueError("nothing to change")
    return takes.update(take_id, **changes).render()


@tool
def delete_take(take_id: str, delete_file: bool = False) -> str:
    """Forget a recording. The audio stays unless delete_file is set.

    Never set delete_file without asking. These are recordings of the user
    playing - the one thing in this workspace that cannot be made again.
    """
    return takes.delete(take_id, remove_file=delete_file)


@tool
def list_device_audio() -> str:
    """List audio files sitting on the device's USB storage.

    Needs the Core in Menu > USB Transfer. HeadRush has not documented where
    the looper writes, so this looks in the likely folders and then
    everywhere - impulse responses will show up alongside anything the user
    recorded, and telling them apart is a human judgement.
    """
    volume, found = devices.list_device_audio()
    return devices.render_device_audio(volume, found)


@tool
def import_device_audio(
    kind: Kind = "loop",
    guitar: str | None = None,
    pickup: str | None = None,
    note: str | None = None,
    only: list[str] | None = None,
) -> str:
    """Copy recordings off the device's USB storage into the takes library.

    Defaults to 'loop' rather than 'di' deliberately: whatever the pedal
    recorded has already been through the pedal. Do not relabel one as a DI
    unless the user says it was recorded dry.

    Args:
        only: Filenames to import. Left out, everything audio is imported -
            which will include impulse responses, so prefer naming them.
    """
    imported, skipped = devices.import_device_audio(
        kind=kind, guitar=guitar, pickup=pickup, note=note, only=only
    )
    return devices.render_import(imported, skipped)


@tool
def compare_guitars(
    target_path: str,
    nam_path: str | None = None,
    ir_path: str | None = None,
    take_ids: list[str] | None = None,
    target_region: str | None = None,
) -> str:
    """Rank the user's instruments against a target, one variable at a time.

    search_offline answers "which capture gets closest". This answers the
    question underneath it: which guitar should they be holding. Same rig,
    same target - and it is the one difference no amount of capture hunting
    fixes, because a Les Paul is not going to sound like a Strat however good
    the model is.

    Needs at least two DI takes. Suggest this when they own more than one
    guitar and are chasing a specific record; it is often a shorter path than
    another twenty captures.

    Args:
        nam_path: Render each guitar through this capture first. Without one
            the DIs are compared raw, which is still a real answer about the
            instruments but reads dark and thin against a recorded target.
        take_ids: Limit the comparison. Defaults to every DI in the library.
        target_region: 'start:end', or 'auto' for the busiest passage.
    """
    from ...core import matching

    results = matching.compare_guitars(
        target_path,
        nam_path=nam_path,
        ir_path=ir_path,
        take_ids=take_ids,
        target_region=target_region,
    )
    return matching.render_guitar_comparison(
        results, through_a_rig=bool(nam_path or ir_path)
    )

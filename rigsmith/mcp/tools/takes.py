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
    """Show one recording: the instrument, what was played, and any concerns."""
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

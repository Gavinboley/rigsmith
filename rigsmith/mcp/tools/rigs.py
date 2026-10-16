"""Rig authoring tools.

Every one of these delegates to :mod:`rigsmith.core.rigs`. Nothing here does
orchestration - if a tool body grows past formatting a result, the logic
belongs in core so the CLI and web UI get it too.
"""

from __future__ import annotations

from typing import Any, Literal

from ...core import rigs
from .._shared import tool

SignalPathName = Literal[
    "straight", "middle_split_3_4_3", "middle_split_5_3_3", "middle_split_7_2_3"
]
CategoryName = Literal["amp", "cab", "ir", "clone", "nam", "effect"]


@tool
def create_rig(
    name: str,
    signal_path: SignalPathName = "straight",
    notes: str | None = None,
) -> str:
    """Start a new rig draft.

    Returns a draft_id used by every other authoring tool. Nothing touches the
    device until write_rig.

    Args:
        name: Rig name as it will appear on the device.
        signal_path: 'straight' for a linear chain, or a middle-split path
            whose digits are the slot allocation (pre, split-per-lane, post).
        notes: Free text kept with the draft, e.g. the tone you are chasing.
    """
    result = rigs.create(name, signal_path=signal_path, notes=notes)
    return (
        f"{result.summary}\n"
        f"Add blocks with add_block(draft_id='{result.draft.draft_id}', ...)."
    )


@tool
def list_drafts() -> str:
    """List rig drafts in progress, with block and scene counts.

    Drafts persist between sessions, so a rig started earlier is still here.
    Use the draft_id with explain_rig to see one in full.
    """
    drafts = rigs.list_drafts()
    if not drafts:
        return "No drafts. Start one with create_rig."
    lines = [f"{len(drafts)} draft(s):"]
    for draft in drafts:
        lines.append(
            f"  {draft.draft_id:24} '{draft.rig.name}'  "
            f"{len(draft.rig.blocks)}/14 blocks, {len(draft.rig.scenes)} scenes  "
            f"(updated {draft.updated_at})"
        )
    return "\n".join(lines)


@tool
def explain_rig(draft_id: str) -> str:
    """Describe a draft in full: signal chain, blocks, parameters, and scenes."""
    draft = rigs.get(draft_id)
    lines = [draft.rig.describe(), "", f"chain: {draft.rig.signal_chain()}"]
    if draft.rig.notes:
        lines.append(f"notes: {draft.rig.notes}")

    params = [
        (b.slot, b.model_id, b.params)
        for b in sorted(draft.rig.blocks, key=lambda b: b.slot)
        if b.params
    ]
    if params:
        lines.append("\nparameters:")
        for slot, model_id, values in params:
            rendered = ", ".join(f"{k}={v}" for k, v in sorted(values.items()))
            lines.append(f"  slot {slot} ({model_id}): {rendered}")
    return "\n".join(lines)


@tool
def tone_sheet(draft_id: str) -> str:
    """The rig as it would be dialled in by hand - each block's controls as
    the Core's screen shows them, and the scenes - plus where it came from:
    the song and part, the research, and what still differs from the record."""
    from ...core import sheets

    return sheets.tone_sheet(draft_id).markdown()


@tool
def delete_draft(draft_id: str) -> str:
    """Discard a draft.

    Local only - drafts live in the workspace, so this never touches the
    device or anything already written to it. Use duplicate_draft first if
    you want to keep a variant before throwing the original away.
    """
    return rigs.delete(draft_id)


@tool
def duplicate_draft(draft_id: str, new_name: str | None = None) -> str:
    """Fork a draft so a variant can be tried without losing the original."""
    return rigs.duplicate(draft_id, new_name).summary


@tool
def rename_rig(draft_id: str, name: str) -> str:
    """Rename the rig inside a draft. The draft_id itself does not change."""
    return rigs.rename(draft_id, name).summary


@tool
def add_block(
    draft_id: str,
    category: CategoryName,
    model_id: str,
    slot: int | None = None,
    params: dict[str, Any] | None = None,
    model_name: str | None = None,
    enabled: bool = True,
) -> str:
    """Add a block to a draft rig.

    Blocks run in slot order, so add them in signal-chain order: drives first,
    then the amp or NAM capture, then the cab or IR, then time-based effects.

    Args:
        draft_id: From create_rig.
        category: 'nam' for a Neural Amp Modeler capture, 'clone' for a
            HeadRush Amp Cloner capture, 'ir' for a cabinet impulse response.
        model_id: Catalog id, or the capture name for nam/clone/ir blocks.
        slot: 1-14. Omit to append to the next free slot.
        params: Model-specific parameters. For a NAM block include
            {"size": "A2 Lite"} when known - it materially changes CPU cost.
        model_name: Human-readable label, if different from model_id.
        enabled: Whether the block starts active.
    """
    return rigs.add_block(
        draft_id,
        category,
        model_id,
        slot=slot,
        params=params,
        model_name=model_name,
        enabled=enabled,
    ).render()


@tool
def remove_block(draft_id: str, slot: int) -> str:
    """Remove the block in a slot. Scene references to it are cleaned up."""
    return rigs.remove_block(draft_id, slot).render()


@tool
def move_block(draft_id: str, from_slot: int, to_slot: int) -> str:
    """Move a block to a different position in the chain.

    The block lands in exactly the slot asked for. Only blocks that actually
    collide are displaced, so gaps in the chain are preserved, and scene
    references follow their blocks rather than staying pinned to slot numbers.
    """
    return rigs.move_block(draft_id, from_slot, to_slot).render()


@tool
def set_param(draft_id: str, slot: int, name: str, value: float | str | bool) -> str:
    """Set one parameter on one block.

    'input_gain_db' and 'output_gain_db' are the universal advanced-block-edit
    gains (-12 to +12 dB); everything else is model-specific.
    """
    return rigs.set_param(draft_id, slot, name, value).render(chain=False)


@tool
def toggle_block(draft_id: str, slot: int, enabled: bool) -> str:
    """Turn a block on or off in the rig's base state.

    This is the rig's resting state. To switch blocks per-part, use scenes.
    """
    return rigs.toggle_block(draft_id, slot, enabled).render()


@tool
def set_block_doubling(
    draft_id: str,
    slot: int,
    mode: Literal["none", "stereo", "switch"],
    second_model_id: str | None = None,
) -> str:
    """Configure 2X doubling on an amp, cab, IR, clone, or NAM slot.

    'stereo' runs both models at once for a wider image and costs twice the
    CPU. 'switch' puts two models in one slot and swaps between them with a
    footswitch.
    """
    return rigs.set_doubling(draft_id, slot, mode, second_model_id).render(chain=False)


@tool
def set_signal_path(draft_id: str, signal_path: SignalPathName) -> str:
    """Change the rig's routing between straight and middle-split layouts."""
    return rigs.set_signal_path(draft_id, signal_path).render()


@tool
def undo_rig_edit(draft_id: str, redo: bool = False, steps: int = 1) -> str:
    """Step the draft back to before its last edit - a nudge the user did
    not like, a block removed by mistake - and say which edit that was.
    redo=True steps forward again; steps goes several edits at once ("back
    to before the reverb"); steps=0 only lists the edits, newest first.
    Up to 30 edits back."""
    if steps == 0:
        found = rigs.history(draft_id)
        return "\n".join(f"{n}. {c}" for n, c in enumerate(found["past"], 1)) or "no edits kept yet"
    return rigs.undo(draft_id, redo=redo, steps=steps).summary


@tool
def nudge_tone(draft_id: str, how: str, slot: int | None = None) -> str:
    """Turn the draft's amp the way the user said, in their words - "a bit
    brighter", "tighter low end", "a lot more gain, less fizz" - or what is
    wrong with it: "too boomy", "too much gain", "not enough bite". Knows
    brighter/darker, more/less gain, bass, mids, treble and presence,
    tighter, fuller, more bite, smoother, louder/quieter on the amp, and
    more/less reverb or delay, wetter/drier and more/fewer repeats on the
    first reverb or delay; "a bit" and "a lot" scale it. Says what it moved and anything it did not understand."""
    return rigs.nudge(draft_id, how, slot=slot).summary


@tool
def set_rig_tempo(draft_id: str, bpm: float | None = None) -> str:
    """Give the rig its own tempo (30-240 BPM), saved as Fixed so its synced
    delays and tremolos load on the song's beat. Omit bpm to follow the
    device's tempo instead."""
    return rigs.set_tempo(draft_id, bpm).summary


@tool
def configure_scene(
    draft_id: str,
    name: str,
    on_slots: list[int] | None = None,
    off_slots: list[int] | None = None,
    no_change_slots: list[int] | None = None,
    footswitch: int | None = None,
    presets: dict[str, str] | None = None,
    reset: bool = False,
) -> str:
    """Create or update a Scene - HeadRush's snapshot.

    A scene turns blocks on or off with one footswitch press. Any slot the
    scene does not mention is left alone (No Change), which is how you build
    a scene that only touches what it cares about.

    **Updates merge.** Calling this again for an existing scene changes only
    what you name: unlisted slots keep their setting, and the footswitch is
    unchanged unless you pass a new one. So a scene can be built up over
    several calls. Pass reset=True to start over, or list a slot in
    no_change_slots to drop it from the scene.

    A single footswitch toggles between at most two scenes.
    """
    return rigs.configure_scene(
        draft_id,
        name,
        on_slots=on_slots,
        off_slots=off_slots,
        no_change_slots=no_change_slots,
        footswitch=footswitch,
        presets=presets,
        reset=reset,
    ).render(chain=False)


@tool
def delete_scene(draft_id: str, name: str) -> str:
    """Remove a scene from the rig.

    The blocks themselves are untouched - only the scene that switched
    them is removed, freeing its footswitch for another scene.
    """
    return rigs.delete_scene(draft_id, name).render(chain=False)


@tool
def validate_rig(draft_id: str) -> str:
    """Check a draft for problems before writing it to the device.

    Reports errors (which block a write), warnings (legal but probably
    unintended), and the estimated CPU load. The CPU figure is an estimate
    built on community measurements, not a vendor specification - the
    device's own meter is authoritative.

    When a NAM block's capture has been downloaded, its own metadata is read
    too. The finding worth acting on is the double cab: most TONE3000
    captures are of a whole miked rig, and an IR after one of those runs the
    signal through two speakers, which sounds boxy for reasons that are very
    hard to trace back. Where the file does not say what was captured, the
    finding is phrased as a question - do not resolve it by guessing.
    """
    result = rigs.validate(draft_id)
    header = "VALID" if result.ok else f"INVALID - {len(result.errors)} error(s)"
    return f"{header}\n{result.report()}"


@tool
def compare_rigs(draft_id: str, other_id: str) -> str:
    """What differs between two drafts - blocks, settings and scenes, going
    from the first to the second. For a draft finished by ear beside its
    rebuild, or two tries at one sound."""
    difference = rigs.compare(draft_id, other_id)
    return difference.render() + ("" if difference.empty else f"\n\nsummary: {difference.summary()}")


@tool
def preview_changes(draft_id: str) -> str:
    """Show exactly what writing this draft would change on the device."""
    difference = rigs.preview(draft_id)
    result = rigs.validate(draft_id)

    lines = [difference.render(), "", f"summary: {difference.summary()}"]
    lines.append(f"est. CPU: ~{result.estimated_cpu:.0%}")
    if not result.ok:
        lines.append(f"\nBLOCKED: {len(result.errors)} error(s) must be fixed first:")
        lines.extend(f"  {f}" for f in result.errors)
    elif result.warnings:
        lines.append("\nwarnings:")
        lines.extend(f"  {f}" for f in result.warnings)
    return "\n".join(lines)


@tool
def export_rig(draft_id: str, path: str | None = None) -> str:
    """Write a draft to a JSON file.

    This is rigsmith's own readable format - not the device's binary `.rig`.
    Useful for version control, sharing, and review.
    """
    return f"Exported draft '{draft_id}' to {rigs.export(draft_id, path)}"


@tool
def write_rig(
    draft_id: str, confirm: bool = False, allow_without_backup: bool = False
) -> str:
    """Write a draft to the device.

    Validates, backs up, and shows a diff first. Requires confirm=True so the
    diff can be reviewed before anything changes on the hardware.

    The backup is a gate, not a courtesy: if the device's USB storage is not
    mounted the write is refused. Relay that to the user rather than working
    around it - putting the Core into Menu > USB Transfer is the fix, and it
    takes seconds.

    Args:
        allow_without_backup: Write even when the device's USB storage is not
            mounted and no backup can be taken. Do not set this on the user's
            behalf - a write overwrites a rig slot, and a rig built on the
            device's own screen exists nowhere else. Ask them first.
    """
    result = rigs.write(
        draft_id, confirm=confirm, allow_without_backup=allow_without_backup
    )
    rendered = result.render()
    rendered = rendered.replace(
        "waive the backup requirement explicitly",
        "call again with allow_without_backup=True",
    )
    if not result.written and not confirm and result.validation.ok:
        # Core's message is surface-agnostic; name the actual argument here.
        rendered = rendered.replace(
            "call again with confirm.",
            "call again with confirm=True.",
            1,
        )
    return rendered

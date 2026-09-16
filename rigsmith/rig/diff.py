"""Readable diffs between two rigs.

This is what the user sees before agreeing to a write. It has one job: make
it obvious what is about to change on the hardware, including the things that
are easy to miss - a parameter nudged by 0.05, a scene quietly repointed, a
block bypassed rather than removed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Block, Rig, Scene, SceneBlockState


@dataclass
class Change:
    """One difference between two rigs."""

    kind: str
    """``add``, ``remove``, ``move``, ``param``, ``toggle``, ``scene``, ``rig``."""
    description: str
    slot: int | None = None
    before: object = None
    after: object = None

    def render(self, colour: bool = False) -> str:
        marks = {
            "add": "+",
            "remove": "-",
            "move": "~",
            "param": "~",
            "toggle": "~",
            "scene": "~",
            "rig": "~",
        }
        mark = marks.get(self.kind, "~")
        return f"  {mark} {self.description}"


@dataclass
class RigDiff:
    """The full set of changes between two rigs."""

    before_name: str
    after_name: str
    changes: list[Change] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.changes

    def render(self) -> str:
        if self.empty:
            return "No changes."
        lines = [f"{self.before_name} -> {self.after_name}"]
        order = {"rig": 0, "add": 1, "remove": 2, "move": 3, "toggle": 4, "param": 5, "scene": 6}
        for change in sorted(self.changes, key=lambda c: (order.get(c.kind, 9), c.slot or 0)):
            lines.append(change.render())
        return "\n".join(lines)

    def summary(self) -> str:
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.kind] = counts.get(change.kind, 0) + 1
        if not counts:
            return "no changes"
        return ", ".join(f"{n} {kind}" for kind, n in sorted(counts.items()))


def _label(block: Block) -> str:
    return block.model_name or block.model_id


def _format_value(value) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _diff_block(before: Block, after: Block, changes: list[Change]) -> None:
    slot = after.slot
    name = _label(after)

    if before.model_id != after.model_id:
        changes.append(
            Change(
                "param",
                f"slot {slot}: model {_label(before)} -> {name}",
                slot=slot,
                before=before.model_id,
                after=after.model_id,
            )
        )

    if before.enabled != after.enabled:
        state = "enabled" if after.enabled else "bypassed"
        changes.append(
            Change("toggle", f"slot {slot} ({name}): {state}", slot=slot,
                   before=before.enabled, after=after.enabled)
        )

    keys = sorted(set(before.params) | set(after.params))
    for key in keys:
        old = before.params.get(key)
        new = after.params.get(key)
        if old == new:
            continue
        if old is None:
            changes.append(
                Change("param", f"slot {slot} ({name}): {key} set to {_format_value(new)}",
                       slot=slot, after=new)
            )
        elif new is None:
            changes.append(
                Change("param", f"slot {slot} ({name}): {key} cleared (was {_format_value(old)})",
                       slot=slot, before=old)
            )
        else:
            changes.append(
                Change("param", f"slot {slot} ({name}): {key} {_format_value(old)} -> {_format_value(new)}",
                       slot=slot, before=old, after=new)
            )

    for attr, label in (
        ("input_gain_db", "input gain"),
        ("output_gain_db", "output gain"),
    ):
        old = getattr(before, attr)
        new = getattr(after, attr)
        if old != new:
            changes.append(
                Change("param", f"slot {slot} ({name}): {label} {old:g} -> {new:g} dB",
                       slot=slot, before=old, after=new)
            )

    if before.doubling != after.doubling or before.doubled_model_id != after.doubled_model_id:
        changes.append(
            Change(
                "param",
                f"slot {slot} ({name}): 2X {before.doubling.value} -> "
                f"{after.doubling.value}"
                + (f" ({after.doubled_model_id})" if after.doubled_model_id else ""),
                slot=slot,
            )
        )

    # EQ, LED colour and lane round-trip through the codec and reach the
    # device, so a diff that skipped them would show "no changes" for an edit
    # that is about to alter the sound.
    if before.eq != after.eq:
        if len(before.eq) != len(after.eq):
            changes.append(
                Change(
                    "param",
                    f"slot {slot} ({name}): EQ bands {len(before.eq)} -> {len(after.eq)}",
                    slot=slot,
                )
            )
        else:
            for index, (old, new) in enumerate(zip(before.eq, after.eq), start=1):
                if old == new:
                    continue
                parts = []
                if old.frequency != new.frequency:
                    parts.append(f"{old.frequency:g} -> {new.frequency:g} Hz")
                if old.gain_db != new.gain_db:
                    parts.append(f"{old.gain_db:+g} -> {new.gain_db:+g} dB")
                if old.q != new.q:
                    parts.append(f"Q {old.q:g} -> {new.q:g}")
                if old.mode != new.mode:
                    parts.append(f"{old.mode} -> {new.mode}")
                changes.append(
                    Change(
                        "param",
                        f"slot {slot} ({name}): EQ band {index} " + ", ".join(parts),
                        slot=slot,
                    )
                )

    if before.led_color != after.led_color:
        changes.append(
            Change(
                "param",
                f"slot {slot} ({name}): LED {before.led_color or 'default'} -> "
                f"{after.led_color or 'default'}",
                slot=slot,
            )
        )

    if before.lane != after.lane:
        changes.append(
            Change(
                "move",
                f"slot {slot} ({name}): lane {before.lane} -> {after.lane}",
                slot=slot,
            )
        )


def _describe_scene(scene: Scene) -> str:
    on = sorted(s for s, st in scene.states.items() if st is SceneBlockState.ON)
    off = sorted(s for s, st in scene.states.items() if st is SceneBlockState.OFF)
    fs = f"FS{scene.footswitch}" if scene.footswitch else "unassigned"
    return f"{fs}, on={on or '-'}, off={off or '-'}"


def _diff_scenes(before: Rig, after: Rig, changes: list[Change]) -> None:
    by_name_before = {s.name.lower(): s for s in before.scenes}
    by_name_after = {s.name.lower(): s for s in after.scenes}

    for key, scene in by_name_after.items():
        if key not in by_name_before:
            changes.append(
                Change("scene", f"scene added: '{scene.name}' ({_describe_scene(scene)})")
            )
            continue

        old = by_name_before[key]
        if old.footswitch != scene.footswitch:
            changes.append(
                Change(
                    "scene",
                    f"scene '{scene.name}': footswitch "
                    f"{old.footswitch or 'none'} -> {scene.footswitch or 'none'}",
                )
            )
        if old.states != scene.states:
            changes.append(
                Change(
                    "scene",
                    f"scene '{scene.name}': states {_describe_scene(old)} -> "
                    f"{_describe_scene(scene)}",
                )
            )
        if old.presets != scene.presets:
            changes.append(
                Change("scene", f"scene '{scene.name}': preset recalls changed")
            )
        # The scene's LED colour is what the footswitch lights up as, so it
        # is device state and belongs in the diff a person approves - even
        # though it changes nothing about the sound.
        if old.led_color != scene.led_color:
            changes.append(
                Change(
                    "scene",
                    f"scene '{scene.name}': LED "
                    f"{old.led_color or 'default'} -> {scene.led_color or 'default'}",
                    before=old.led_color,
                    after=scene.led_color,
                )
            )

    for key, scene in by_name_before.items():
        if key not in by_name_after:
            changes.append(Change("scene", f"scene removed: '{scene.name}'"))


def diff_rigs(before: Rig | None, after: Rig) -> RigDiff:
    """Compare two rigs. ``before=None`` means the rig is entirely new."""
    if before is None:
        result = RigDiff("(new)", after.name)
        result.changes.append(
            Change("rig", f"create rig '{after.name}' ({after.signal_path.value})")
        )
        for block in sorted(after.blocks, key=lambda b: b.slot):
            result.changes.append(
                Change("add", f"slot {block.slot}: {_label(block)} ({block.category.value})",
                       slot=block.slot, after=block.model_id)
            )
        for scene in after.scenes:
            result.changes.append(
                Change("scene", f"scene '{scene.name}' ({_describe_scene(scene)})")
            )
        return result

    result = RigDiff(before.name, after.name)
    changes = result.changes

    if before.name != after.name:
        changes.append(Change("rig", f"name: '{before.name}' -> '{after.name}'"))
    if before.signal_path is not after.signal_path:
        changes.append(
            Change("rig", f"signal path: {before.signal_path.value} -> {after.signal_path.value}")
        )
    if before.rig_width != after.rig_width:
        changes.append(
            Change("rig", f"rig width: {before.rig_width}% -> {after.rig_width}%")
        )
    if before.setlist != after.setlist:
        changes.append(
            Change("rig", f"setlist: {before.setlist or 'none'} -> {after.setlist or 'none'}")
        )
    if before.slot_number != after.slot_number:
        changes.append(
            Change(
                "rig",
                f"position: {before.slot_number or 'unset'} -> "
                f"{after.slot_number or 'unset'}",
            )
        )

    before_blocks = {b.slot: b for b in before.blocks}
    after_blocks = {b.slot: b for b in after.blocks}

    # Two passes, because a rig can legitimately hold two instances of the
    # same pedal. Matching purely by model id would then pair them
    # arbitrarily, and attribute one block's parameter change to the other.
    # Pass one claims blocks that kept both their slot and their model, which
    # is the unambiguous case; pass two matches what is left by model id.
    matched_before: set[int] = set()
    partners: dict[int, Block] = {}

    for slot, block in after_blocks.items():
        candidate = before_blocks.get(slot)
        if candidate is not None and candidate.model_id == block.model_id:
            partners[slot] = candidate
            matched_before.add(slot)

    before_by_model: dict[str, list[Block]] = {}
    for block in sorted(before.blocks, key=lambda b: b.slot):
        if block.slot not in matched_before:
            before_by_model.setdefault(block.model_id, []).append(block)

    for slot, block in sorted(after_blocks.items()):
        if slot in partners:
            continue
        candidates = before_by_model.get(block.model_id, [])
        partner = next((c for c in candidates if c.slot not in matched_before), None)
        if partner is not None:
            partners[slot] = partner
            matched_before.add(partner.slot)

    for slot, block in sorted(after_blocks.items()):
        partner = partners.get(slot)
        if partner is None:
            changes.append(
                Change("add", f"slot {slot}: add {_label(block)} ({block.category.value})",
                       slot=slot, after=block.model_id)
            )
            continue
        if partner.slot != slot:
            changes.append(
                Change("move", f"{_label(block)}: slot {partner.slot} -> {slot}",
                       slot=slot, before=partner.slot, after=slot)
            )
        _diff_block(partner, block, changes)

    for slot, block in sorted(before_blocks.items()):
        if slot not in matched_before:
            changes.append(
                Change("remove", f"slot {slot}: remove {_label(block)}",
                       slot=slot, before=block.model_id)
            )

    _diff_scenes(before, after, changes)
    return result

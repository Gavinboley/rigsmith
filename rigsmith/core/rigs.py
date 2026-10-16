"""Rig authoring: the one implementation the CLI, MCP, and web all call.

Every mutation returns an :class:`EditResult` carrying the updated draft, a
human sentence describing what happened, and fresh validation. That shape
exists because all three surfaces need the same three things and would
otherwise each re-derive them: the MCP server renders it as text for an
agent, the web UI serialises it to JSON, and the CLI prints it.

The domain model raises ``ValueError`` for rule violations; this layer
translates those into :mod:`rigsmith.core.errors` so each surface can map
failures to its own idiom without string-matching.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import get_settings, require_writable
from ..rig.catalog import Catalog, get_catalog
from ..rig.codec import save_rig
from ..rig.diff import RigDiff, diff_rigs
from ..rig.model import (
    Block,
    BlockCategory,
    Doubling,
    Rig,
    Scene,
    SceneBlockState,
    SignalPath,
)
from ..rig.validate import ValidationResult
from ..rig.validate import validate_rig as _validate_base
from .drafts import Draft, get_store
from .errors import Conflict, Invalid, NotFound, Unavailable


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class EditResult:
    """The outcome of one edit: what changed, and whether the rig is still sane."""

    draft: Draft
    summary: str
    validation: ValidationResult
    details: list[str] = field(default_factory=list)

    @property
    def rig(self) -> Rig:
        return self.draft.rig

    @property
    def ok(self) -> bool:
        return self.validation.ok

    def render(self, *, chain: bool = True) -> str:
        lines = [self.summary]
        if chain:
            lines.append(f"chain: {self.rig.signal_chain()}")
        lines.extend(self.details)
        lines.append(f"est. CPU: ~{self.validation.estimated_cpu:.0%}")
        for finding in self.validation.findings:
            if finding.severity in ("error", "warning"):
                lines.append(f"  {finding}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        from ..rig.codec import rig_to_dict

        return {
            "draft_id": self.draft.draft_id,
            "summary": self.summary,
            "details": self.details,
            "chain": self.rig.signal_chain(),
            "ok": self.ok,
            "rig": rig_to_dict(self.rig),
            "validation": {
                "ok": self.validation.ok,
                "estimated_cpu": self.validation.estimated_cpu,
                "findings": [
                    {
                        "severity": f.severity,
                        "message": f.message,
                        "slot": f.slot,
                        "scene": f.scene,
                    }
                    for f in self.validation.findings
                ],
            },
        }


@dataclass
class WriteResult:
    """The outcome of attempting to write a draft to the device."""

    draft: Draft
    diff: RigDiff
    validation: ValidationResult
    written: bool
    message: str
    backup_path: Path | None = None
    report: Any = None
    """The device client's step-by-step :class:`WriteReport`, when a write
    was actually attempted."""
    exact: bool = False
    """Written, and every value took - the rig on the device is the draft."""

    def render(self) -> str:
        lines = []
        if self.backup_path:
            lines.append(f"Backup taken first: {self.backup_path}")
        lines.append(self.message)
        if self.report is not None:
            lines.append("")
            lines.append(self.report.render())
        lines.append("")
        lines.append(self.diff.render())
        lines.append("")
        lines.append(f"summary: {self.diff.summary()}")
        lines.append(f"est. CPU: ~{self.validation.estimated_cpu:.0%}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_id": self.draft.draft_id,
            "written": self.written,
            "exact": self.exact,
            "message": self.message,
            "backup_path": str(self.backup_path) if self.backup_path else None,
            "diff": [c.description for c in self.diff.changes],
            "diff_summary": self.diff.summary(),
            # The rendered diff, because a surface that asks "write this?"
            # has to be able to show what "this" is.
            "diff_rendered": self.diff.render(),
            "ok": self.validation.ok,
            "validation": {
                "ok": self.validation.ok,
                "estimated_cpu": self.validation.estimated_cpu,
                "errors": [str(f) for f in self.validation.errors],
                "warnings": [str(f) for f in getattr(self.validation, "warnings", [])],
            },
            "report": self.report.to_dict() if self.report is not None else None,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _capture_lookup():
    """Find NAM blocks' captures among the ones already downloaded.

    A capture's own metadata answers questions the rig cannot - whether a cab
    is baked in, most of all. Captures that are not on disk simply are not
    found, and the checks that depend on them stay open questions.
    """
    from ..tone.nam import find_capture

    directory = get_settings().nam_dir
    cache: dict[str, Any] = {}

    def lookup(model_id: str):
        if model_id not in cache:
            try:
                cache[model_id] = find_capture(model_id, directory)
            except OSError:
                cache[model_id] = None
        return cache[model_id]

    return lookup


def _validate_rig(rig: Rig, catalog: Catalog | None = None) -> ValidationResult:
    return _validate_base(rig, catalog, capture_lookup=_capture_lookup())


def _get(draft_id: str) -> Draft:
    try:
        return get_store().get(draft_id)
    except KeyError as exc:
        raise NotFound(str(exc).strip("'\"")) from exc


def _block(draft: Draft, slot: int) -> Block:
    block = draft.rig.block_at(slot)
    if block is None:
        used = sorted(draft.rig.used_slots)
        raise NotFound(
            f"no block in slot {slot}",
            hint=f"Occupied slots: {used or 'none'}",
        )
    return block


def parse_category(value: str) -> BlockCategory:
    try:
        return BlockCategory(str(value).lower())
    except ValueError:
        options = ", ".join(c.value for c in BlockCategory)
        raise Invalid(
            f"unknown block category '{value}'", hint=f"Options: {options}"
        ) from None


def parse_signal_path(value: str) -> SignalPath:
    try:
        return SignalPath(str(value).lower())
    except ValueError:
        options = ", ".join(p.value for p in SignalPath)
        raise Invalid(
            f"unknown signal path '{value}'", hint=f"Options: {options}"
        ) from None


def parse_doubling(value: str) -> Doubling:
    try:
        return Doubling(str(value).lower())
    except ValueError:
        options = ", ".join(d.value for d in Doubling)
        raise Invalid(
            f"unknown doubling mode '{value}'", hint=f"Options: {options}"
        ) from None


def _finish(draft: Draft, summary: str, *, details: list[str] | None = None) -> EditResult:
    """Persist the draft and package the result the same way every time."""
    get_store().save(draft, change=summary)
    return EditResult(
        draft=draft,
        summary=summary,
        validation=_validate_rig(draft.rig),
        details=details or [],
    )


# --------------------------------------------------------------------------
# drafts
# --------------------------------------------------------------------------


def create(
    name: str,
    *,
    signal_path: str = "straight",
    notes: str | None = None,
) -> EditResult:
    """Start a new rig draft."""
    if not name or not name.strip():
        raise Invalid("a rig needs a name")
    rig = Rig(name=name.strip(), signal_path=parse_signal_path(signal_path), notes=notes)
    draft = get_store().create(rig)
    return _finish(
        draft,
        f"Created draft '{draft.draft_id}' for rig '{rig.name}' "
        f"({rig.signal_path.value}, {len(rig.free_slots)} slots free).",
    )


def list_drafts() -> list[Draft]:
    """Every draft in progress, newest activity first."""
    drafts = get_store().list_drafts()
    return sorted(drafts, key=lambda d: d.updated_at, reverse=True)


def get(draft_id: str) -> Draft:
    """One draft, or :class:`NotFound`."""
    return _get(draft_id)


def delete(draft_id: str) -> str:
    """Discard a draft. The device is untouched - drafts are local only."""
    require_writable(f"delete draft '{draft_id}'")
    draft = _get(draft_id)
    name = draft.rig.name
    get_store().delete(draft_id)
    return f"Deleted draft '{draft_id}' ('{name}')."


def duplicate(draft_id: str, new_name: str | None = None) -> EditResult:
    """Fork a draft, so a variant can be tried without losing the original."""
    import copy

    source = _get(draft_id)
    rig = copy.deepcopy(source.rig)
    rig.name = new_name or f"{rig.name} copy"
    draft = get_store().create(rig, baseline=source.baseline)
    return _finish(
        draft,
        f"Duplicated '{draft_id}' as '{draft.draft_id}' ('{rig.name}').",
    )


def rename(draft_id: str, name: str) -> EditResult:
    """Rename the rig inside a draft. The draft id does not change."""
    if not name or not name.strip():
        raise Invalid("a rig needs a name")
    draft = _get(draft_id)
    was = draft.rig.name
    draft.rig.name = name.strip()
    return _finish(draft, f"Renamed '{was}' to '{draft.rig.name}'.")


# --------------------------------------------------------------------------
# blocks
# --------------------------------------------------------------------------


def add_block(
    draft_id: str,
    category: str,
    model_id: str,
    *,
    slot: int | None = None,
    params: dict[str, Any] | None = None,
    model_name: str | None = None,
    enabled: bool = True,
) -> EditResult:
    """Place a block in the chain. Omit ``slot`` to append to the next free one."""
    draft = _get(draft_id)
    block = Block(
        slot=slot or 0,
        category=parse_category(category),
        model_id=model_id,
        model_name=model_name,
        params=dict(params or {}),
        enabled=enabled,
    )
    try:
        draft.rig.add_block(block)
    except ValueError as exc:
        message = str(exc)
        if "occupied" in message or "is full" in message:
            raise Conflict(
                message, hint=f"Free slots: {draft.rig.free_slots or 'none'}"
            ) from exc
        raise Invalid(message) from exc

    return _finish(draft, f"Added {model_id} to slot {block.slot}.")


def remove_block(draft_id: str, slot: int) -> EditResult:
    """Remove a block. Scene references to it are cleaned up automatically."""
    draft = _get(draft_id)
    _block(draft, slot)
    block = draft.rig.remove_block(slot)
    return _finish(draft, f"Removed {block.model_id} from slot {slot}.")


def move_block(draft_id: str, from_slot: int, to_slot: int) -> EditResult:
    """Move a block. Scene references follow their blocks."""
    draft = _get(draft_id)
    _block(draft, from_slot)
    try:
        block = draft.rig.move_block(from_slot, to_slot)
    except ValueError as exc:
        raise Invalid(str(exc)) from exc
    return _finish(draft, f"Moved {block.model_id} to slot {block.slot}.")


def set_param(draft_id: str, slot: int, name: str, value: Any) -> EditResult:
    """Set one parameter on one block.

    ``input_gain_db`` and ``output_gain_db`` are the universal advanced-edit
    gains (-12..+12 dB); everything else is model-specific.
    """
    draft = _get(draft_id)
    block = _block(draft, slot)

    if name in ("input_gain_db", "output_gain_db"):
        try:
            setattr(block, name, float(value))
        except (TypeError, ValueError) as exc:
            raise Invalid(f"{name} must be a number, got {value!r}") from exc
    else:
        block.params[name] = value

    return _finish(draft, f"slot {slot} ({block.model_id}): {name} = {value}")


def set_params(draft_id: str, slot: int, values: dict[str, float], *, why: str = "") -> EditResult:
    """Set several of one block's numbers as one edit - one step to undo."""
    draft = _get(draft_id)
    block = _block(draft, slot)
    said = []
    for name, value in values.items():
        before = block.params.get(name)
        block.params[name] = value
        said.append(f"{name} {before:.0%} -> {value:.0%}" if isinstance(before, (int, float)) and not isinstance(before, bool) else f"{name} = {value}")
    return _finish(draft, f"slot {slot} ({block.model_id}): " + (f"{why} - " if why else "") + ", ".join(said))


def nudge(draft_id: str, text: str, *, slot: int | None = None, live: bool = False) -> EditResult:
    """Move the knobs the way the words say: "a bit brighter, too much reverb".

    Amp words turn the first amp block, and "more reverb" or "fewer
    repeats" the first reverb or delay with that control - or all of them
    turn the block ``slot`` names. A control the draft has not set starts
    from the catalog's default for it. ``live`` also sends the moves to the
    Core, when the rig it has loaded is this one
    (:func:`rigsmith.core.devices.send_live`), so the change is heard at once.
    """
    from ..rig.catalog import get_catalog
    from ..tune import nudge as words

    draft = _get(draft_id)
    asked = words.parse(text)
    if not asked.moves:
        raise Invalid(
            f"nothing in '{text}' says which way to move",
            hint="Try: brighter, darker, more or less gain, more or less bass, tighter, more or less mids, more bite, smoother, louder, more or less reverb or delay - with 'a bit' or 'a lot'; or what is wrong: too boomy, too much gain, not enough bite.",
        )
    catalog = get_catalog()
    chosen = _block(draft, slot) if slot is not None else None
    ordered = sorted(draft.rig.blocks, key=lambda b: b.slot)

    def control_on(block, kind: str, name: str) -> str | None:
        """What ``block`` calls the control, or None when it has none."""
        if kind == "amp":
            return name
        spec = catalog.get(block.model_id)
        names = words.EFFECT_CONTROLS.get(name, ())
        if spec is None or not spec.params:
            return names[0] if names else None
        return next((n for n in names if spec.param(n) is not None), None)

    def is_kind(block, kind: str) -> bool:
        if kind == "amp":
            return block.category is BlockCategory.AMP
        spec = catalog.get(block.model_id)
        return block.category is BlockCategory.EFFECT and words.effect_kind(block.model_id, spec.subcategory if spec else None) == kind

    moves: dict[int, dict[str, float]] = {}
    missing: list[str] = []
    for control, change in asked.moves.items():
        kind, name = words.target(control)
        if chosen is not None and kind != "amp" and not is_kind(chosen, kind):
            # "More reverb" on a named slot that is not a reverb would turn
            # whatever that block happens to call Mix or Level.
            missing.append(f"slot {chosen.slot} is not a {kind}")
            continue
        candidates = [chosen] if chosen is not None else [b for b in ordered if is_kind(b, kind)]
        found = next(((b, control_on(b, kind, name)) for b in candidates if control_on(b, kind, name)), None)
        if found is None:
            missing.append(f"no {kind} block" if chosen is None else f"no {name} control on slot {chosen.slot}")
            continue
        block, real = found
        moves.setdefault(block.slot, {})[real] = moves.get(block.slot, {}).get(real, 0.0) + change
    if not moves:
        raise Invalid(f"this draft has {' or '.join(dict.fromkeys(missing))} to turn", hint="Add one, or name the slot to nudge.")

    parts = []
    for block_slot, block_moves in sorted(moves.items()):
        block = _block(draft, block_slot)
        spec = catalog.get(block.model_id)
        current = dict(block.params)
        for control in block_moves:
            if control not in current:
                param = spec.param(control) if spec else None
                default = param.default if param is not None and isinstance(param.default, (int, float)) and not isinstance(param.default, bool) else 0.5
                current[control] = float(default) if 0.0 <= float(default) <= 1.0 else 0.5
        changed = words.apply(current, block_moves)
        for control, (_, after) in changed.items():
            block.params[control] = after
        summary = ", ".join(f"{control} {before:.0%} -> {after:.0%}" for control, (before, after) in changed.items()) or "already at the end of their travel"
        part = f"slot {block.slot} ({block.model_id}): {summary}"
        if live and changed:
            import asyncio

            from . import devices

            sent = asyncio.run(devices.send_live(draft.rig, block.slot, {c: after for c, (_, after) in changed.items()}))
            part += " - and on the Core" + (f", which kept its own {', '.join(sent['refused'])}" if sent["refused"] else "")
        parts.append(part)
    message = "; ".join(parts)
    if missing:
        message += f" ({', '.join(dict.fromkeys(missing))})"
    if asked.unknown:
        message += f" (not understood: {', '.join(asked.unknown)})"
    return _finish(draft, message)


def undo(draft_id: str, *, redo: bool = False, steps: int = 1) -> EditResult:
    """Step the draft back to before its last edit - or, ``redo``, forward again.

    ``steps`` goes back (or forward) that many edits at once - to before a
    particular one in :func:`history`. Up to
    :data:`rigsmith.core.drafts.HISTORY` edits back; a new edit after an
    undo drops what could have been redone, as in any editor.
    """
    _get(draft_id)
    if steps < 1:
        raise Invalid("steps must be 1 or more")
    available = len(get_store().changes(draft_id)["future" if redo else "past"])
    if steps > available and available:
        raise Invalid(f"only {available} edit(s) to {'redo' if redo else 'undo'} on '{draft_id}', not {steps}")
    changes = []
    try:
        for _ in range(steps):
            draft, change = get_store().undo(draft_id, redo=redo)
            changes.append(change)
    except LookupError as exc:
        raise Invalid(f"{exc} on '{draft_id}'", hint="Up to the last 30 edits to a draft can be stepped back through.") from exc
    verb = "redid" if redo else "undid"
    summary = f"{verb}: {changes[0]}" if len(changes) == 1 else f"{verb} {len(changes)} edits: " + "; ".join(changes)
    return EditResult(draft=draft, summary=summary, validation=_validate_rig(draft.rig))


def history(draft_id: str) -> dict[str, list[str]]:
    """``{"past": [...], "future": [...]}``: each edit undo would step back, newest first, and each redo would replay."""
    _get(draft_id)
    return get_store().changes(draft_id)


def toggle_block(draft_id: str, slot: int, enabled: bool) -> EditResult:
    """Turn a block on or off in the rig's base state."""
    draft = _get(draft_id)
    block = _block(draft, slot)
    block.enabled = enabled
    state = "enabled" if enabled else "bypassed"
    return _finish(draft, f"slot {slot} ({block.model_id}): {state}")


def set_doubling(
    draft_id: str, slot: int, mode: str, second_model_id: str | None = None
) -> EditResult:
    """Configure 2X doubling on an amp, cab, IR, clone, or NAM slot."""
    draft = _get(draft_id)
    block = _block(draft, slot)
    doubling = parse_doubling(mode)

    block.doubling = doubling
    block.doubled_model_id = second_model_id if doubling is not Doubling.NONE else None

    label = f"2X {doubling.value}"
    if second_model_id:
        label += f" with {second_model_id}"
    return _finish(draft, f"slot {slot}: {label}")


def set_signal_path(draft_id: str, path: str) -> EditResult:
    """Change the rig's routing."""
    draft = _get(draft_id)
    draft.rig.signal_path = parse_signal_path(path)
    return _finish(draft, f"Signal path set to {draft.rig.signal_path.value}.")


def set_rig_width(draft_id: str, width: int) -> EditResult:
    """Set stereo width, 100 = full stereo, 0 = collapsed to mono."""
    if not 0 <= width <= 100:
        raise Invalid(f"rig width {width} is outside 0..100")
    draft = _get(draft_id)
    draft.rig.rig_width = width
    return _finish(draft, f"Rig width set to {width}%.")


def set_tempo(draft_id: str, bpm: float | None) -> EditResult:
    """Give the rig its own tempo, or None (or 0) to follow the device's."""
    from ..rig.model import TEMPO_RANGE

    if bpm and not TEMPO_RANGE[0] <= bpm <= TEMPO_RANGE[1]:
        raise Invalid(
            f"{bpm:g} BPM is outside the Core's rig tempo range",
            hint=f"It stores {TEMPO_RANGE[0]:g}-{TEMPO_RANGE[1]:g}; halve or double a tempo outside it.",
        )
    draft = _get(draft_id)
    draft.rig.tempo = round(float(bpm), 2) if bpm else None
    if draft.rig.tempo:
        return _finish(draft, f"Rig tempo set to {draft.rig.tempo:g} BPM - it loads with this tempo.")
    return _finish(draft, "Rig tempo cleared - it follows the device's tempo.")


# --------------------------------------------------------------------------
# scenes
# --------------------------------------------------------------------------


def configure_scene(
    draft_id: str,
    name: str,
    *,
    on_slots: list[int] | None = None,
    off_slots: list[int] | None = None,
    no_change_slots: list[int] | None = None,
    footswitch: int | None = None,
    presets: dict[str | int, str] | None = None,
    reset: bool = False,
) -> EditResult:
    """Create or update a Scene.

    **Updates merge.** Only what you name changes; unlisted slots keep their
    setting and the footswitch is preserved unless a new one is given. That
    makes it safe to build a scene up across several calls, which is how the
    draft workflow is meant to be used. ``reset=True`` starts over, and
    ``no_change_slots`` drops slots back to No Change.
    """
    if not name or not name.strip():
        raise Invalid("a scene needs a name")

    draft = _get(draft_id)
    scene = draft.rig.scene_named(name)
    created = scene is None

    if scene is None:
        scene = Scene(name=name.strip())
        draft.rig.scenes.append(scene)
    elif reset:
        scene.states = {}
        scene.presets = {}
        scene.footswitch = None

    for slot in on_slots or []:
        scene.states[int(slot)] = SceneBlockState.ON
    for slot in off_slots or []:
        scene.states[int(slot)] = SceneBlockState.OFF
    for slot in no_change_slots or []:
        scene.states.pop(int(slot), None)
        scene.presets.pop(int(slot), None)
    for slot, preset in (presets or {}).items():
        scene.presets[int(slot)] = preset
    if footswitch is not None:
        scene.footswitch = footswitch

    on = sorted(s for s, v in scene.states.items() if v is SceneBlockState.ON)
    off = sorted(s for s, v in scene.states.items() if v is SceneBlockState.OFF)
    details = []
    if scene.presets:
        details.append(f"  recalls presets for slots {sorted(scene.presets)}")

    return _finish(
        draft,
        f"Scene '{scene.name}' {'created' if created else 'updated'}: "
        f"on={on or '-'} off={off or '-'} "
        f"footswitch={scene.footswitch or 'unassigned'}",
        details=details,
    )


def delete_scene(draft_id: str, name: str) -> EditResult:
    """Remove a scene from the rig."""
    draft = _get(draft_id)
    scene = draft.rig.scene_named(name)
    if scene is None:
        known = ", ".join(s.name for s in draft.rig.scenes) or "none"
        raise NotFound(f"no scene '{name}'", hint=f"Scenes in this rig: {known}")
    draft.rig.scenes.remove(scene)
    return _finish(draft, f"Deleted scene '{scene.name}'.")


# --------------------------------------------------------------------------
# validate, preview, export, write
# --------------------------------------------------------------------------


def validate(draft_id: str, catalog: Catalog | None = None) -> ValidationResult:
    """Check a draft without changing it."""
    return _validate_rig(_get(draft_id).rig, catalog or get_catalog())


def preview(draft_id: str) -> RigDiff:
    """What writing this draft would change on the device."""
    draft = _get(draft_id)
    return diff_rigs(draft.baseline, draft.rig)


def compare(draft_id: str, other_id: str) -> RigDiff:
    """What differs between two drafts - read as going from ``draft_id`` to ``other_id``.

    For the drafts that come in pairs: one finished by ear beside its
    rebuild, a duplicate tried another way, a run's rig against another's.
    """
    first, second = _get(draft_id), _get(other_id)
    difference = diff_rigs(first.rig, second.rig)
    difference.before_name, difference.after_name = f"{first.rig.name} ({draft_id})", f"{second.rig.name} ({other_id})"
    return difference


def export(draft_id: str, path: str | Path | None = None) -> Path:
    """Write a draft to a JSON file.

    This is rigsmith's own readable format, not the device's binary `.rig`.
    """
    draft = _get(draft_id)
    require_writable(f"export draft '{draft_id}'")
    target = (
        Path(path)
        if path
        else get_settings().workspace / "exports" / f"{draft.draft_id}.json"
    )
    return save_rig(draft.rig, target)


def write(
    draft_id: str, *, confirm: bool = False, allow_without_backup: bool = False
) -> WriteResult:
    """Write a draft to the device: validate, back up, diff, then write.

    Returns without writing when ``confirm`` is false, so the diff can be
    reviewed first.

    A write overwrites a rig slot on the device, and a rig built by hand on
    the device's own screen exists nowhere else. So the backup is a gate, not
    a courtesy: without one this refuses and says how to mount the storage.
    ``allow_without_backup`` is the deliberate override for someone who knows
    the slot is expendable.
    """
    draft = _get(draft_id)
    validation = _validate_rig(draft.rig)
    difference = diff_rigs(draft.baseline, draft.rig)

    if not validation.ok:
        return WriteResult(
            draft=draft,
            diff=difference,
            validation=validation,
            written=False,
            message=(
                f"Refusing to write: {len(validation.errors)} error(s).\n"
                + "\n".join(f"  {f}" for f in validation.errors)
            ),
        )

    if not confirm:
        return WriteResult(
            draft=draft,
            diff=difference,
            validation=validation,
            written=False,
            message="Not written - review the diff and call again with confirm.",
        )

    require_writable(f"write rig '{draft.rig.name}' to the device")

    backup_path = None
    backup_note = ""
    from ..device.usbmsc import DeviceStorageError, find_device_volume, timestamped_backup

    settings = get_settings()
    settings.ensure_workspace()
    try:
        volume = find_device_volume()
        backup = timestamped_backup(settings.backups_dir, volume)
        backup_path = backup.destination
        if "Rigs" not in backup.folders:
            # The volume mounted but its Rigs folder was missing or empty -
            # a backup without the rigs is not the one the gate is for.
            backup_path = None
            raise DeviceStorageError(
                f"the backup at {backup.destination} copied no rigs - the "
                "device's Rigs folder was missing or empty"
            )
    except DeviceStorageError as exc:
        if not allow_without_backup:
            return WriteResult(
                draft=draft,
                diff=difference,
                validation=validation,
                written=False,
                message=(
                    "Refusing to write without a backup.\n\n"
                    f"{exc}\n\n"
                    "A write overwrites a rig slot, and a rig built on the "
                    "device's own screen exists nowhere else. Put the Core "
                    "into Menu > USB Transfer so a backup can be taken, or "
                    "waive the backup requirement explicitly if the slot is "
                    "expendable."
                ),
            )
        backup_note = (
            "NO BACKUP WAS TAKEN - this write explicitly waived the backup "
            "gate. Whatever was in that slot is gone.\n\n"
        )

    import asyncio

    from ..device.client import connect

    async def perform():
        async with await connect() as client:
            return await client.write_rig(draft.rig)

    try:
        report = asyncio.run(perform())
    except (NotFound, Invalid, Unavailable) as exc:
        return WriteResult(
            draft=draft,
            diff=difference,
            validation=validation,
            written=False,
            backup_path=backup_path,
            message=(
                backup_note
                + f"Nothing was written: {exc}\n\n"
                "Check the Core is awake, on the same network with HeadRush "
                "Remote on, and paired ('rigsmith device pair'); to rehearse "
                "without it, run 'rigsmith protocol simulate' and point "
                "--device at it."
            ),
        )

    return WriteResult(
        draft=draft,
        diff=difference,
        validation=validation,
        written=report.ok,
        exact=report.exact,
        backup_path=backup_path,
        report=report,
        message=(
            backup_note
            + (
                f"Wrote '{draft.rig.name}' to the device."
                if report.exact
                else f"Wrote '{draft.rig.name}' to the device, but it kept "
                f"{len(report.refused)} value(s) of its own ({', '.join(report.refused[:4])}) "
                "- the rig on it is not exactly the draft."
                if report.ok
                else f"Write stopped after {report.completed} step(s)."
            )
        ),
    )

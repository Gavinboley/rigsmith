"""Serialise rigs to and from JSON.

This is *rigsmith's* format, not the device's. The HeadRush `.rig` binary format
is undocumented, and deliberately not on the critical path: the Remote Editor
can create rigs, so the device writes its own files. What this module gives us
is a durable, reviewable, diffable representation for drafts, exports, and
test fixtures - something a human can read and edit in a text editor, which a
binary blob would not be.

If the `.rig` format is ever decoded, it belongs here alongside this, not
instead of it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import (
    Block,
    BlockCategory,
    Doubling,
    EqBand,
    Rig,
    Scene,
    SceneBlockState,
    SignalPath,
)

#: Bumped when the on-disk shape changes incompatibly.
FORMAT_VERSION = 1


def eq_to_dict(band: EqBand) -> dict:
    return {
        "frequency": band.frequency,
        "gain_db": band.gain_db,
        "q": band.q,
        "mode": band.mode,
    }


def block_to_dict(block: Block) -> dict:
    data: dict[str, Any] = {
        "slot": block.slot,
        "category": block.category.value,
        "model_id": block.model_id,
        "enabled": block.enabled,
    }
    if block.model_name:
        data["model_name"] = block.model_name
    if block.params:
        data["params"] = dict(block.params)
    if block.input_gain_db:
        data["input_gain_db"] = block.input_gain_db
    if block.output_gain_db:
        data["output_gain_db"] = block.output_gain_db
    if block.eq:
        data["eq"] = [eq_to_dict(b) for b in block.eq]
    if block.doubling is not Doubling.NONE:
        data["doubling"] = block.doubling.value
        data["doubled_model_id"] = block.doubled_model_id
    if block.led_color:
        data["led_color"] = block.led_color
    if block.lane is not None:
        data["lane"] = block.lane
    return data


def block_from_dict(data: dict) -> Block:
    return Block(
        slot=int(data["slot"]),
        category=BlockCategory(data["category"]),
        model_id=data["model_id"],
        model_name=data.get("model_name"),
        enabled=bool(data.get("enabled", True)),
        params=dict(data.get("params") or {}),
        input_gain_db=float(data.get("input_gain_db", 0.0)),
        output_gain_db=float(data.get("output_gain_db", 0.0)),
        eq=[EqBand(**band) for band in data.get("eq", [])],
        doubling=Doubling(data.get("doubling", "none")),
        doubled_model_id=data.get("doubled_model_id"),
        led_color=data.get("led_color"),
        lane=data.get("lane"),
    )


def scene_to_dict(scene: Scene) -> dict:
    data: dict[str, Any] = {
        "name": scene.name,
        "states": {str(slot): state.value for slot, state in scene.states.items()},
    }
    if scene.presets:
        data["presets"] = {str(slot): name for slot, name in scene.presets.items()}
    if scene.footswitch is not None:
        data["footswitch"] = scene.footswitch
    if scene.led_color:
        data["led_color"] = scene.led_color
    return data


def scene_from_dict(data: dict) -> Scene:
    return Scene(
        name=data["name"],
        states={
            int(slot): SceneBlockState(state)
            for slot, state in (data.get("states") or {}).items()
        },
        presets={int(slot): name for slot, name in (data.get("presets") or {}).items()},
        footswitch=data.get("footswitch"),
        led_color=data.get("led_color"),
    )


def rig_to_dict(rig: Rig) -> dict:
    data: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "name": rig.name,
        "signal_path": rig.signal_path.value,
        "rig_width": rig.rig_width,
        "blocks": [block_to_dict(b) for b in sorted(rig.blocks, key=lambda b: b.slot)],
        "scenes": [scene_to_dict(s) for s in rig.scenes],
    }
    if rig.setlist:
        data["setlist"] = rig.setlist
    if rig.slot_number is not None:
        data["slot_number"] = rig.slot_number
    if rig.notes:
        data["notes"] = rig.notes
    if rig.source_recipe:
        data["source_recipe"] = rig.source_recipe
    return data


def rig_from_dict(data: dict) -> Rig:
    version = data.get("format_version", FORMAT_VERSION)
    if version > FORMAT_VERSION:
        raise ValueError(
            f"rig uses format version {version}, but this build understands "
            f"up to {FORMAT_VERSION}. Upgrade rigsmith rather than risk a bad write."
        )
    rig = Rig(
        name=data["name"],
        signal_path=SignalPath(data.get("signal_path", "straight")),
        rig_width=int(data.get("rig_width", 100)),
        setlist=data.get("setlist"),
        slot_number=data.get("slot_number"),
        notes=data.get("notes"),
        source_recipe=data.get("source_recipe"),
    )
    rig.blocks = [block_from_dict(b) for b in data.get("blocks", [])]
    rig.blocks.sort(key=lambda b: b.slot)
    rig.scenes = [scene_from_dict(s) for s in data.get("scenes", [])]
    return rig


def save_rig(rig: Rig, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rig_to_dict(rig), indent=2), encoding="utf-8")
    return path


def load_rig(path: str | Path) -> Rig:
    return rig_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

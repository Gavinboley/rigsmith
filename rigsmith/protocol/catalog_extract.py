"""Harvest the real model catalog from the device and the firmware.

The Core ships roughly 97 amp models, each with its own knob set, and no
per-model parameter table is published anywhere. Until this existed,
:mod:`rigsmith.rig.catalog` could only ever serve its seed - which meant
validation ran permanently in "unverified" mode and the parameter-range
checks, the part of the safety net meant to stop a confidently wrong write,
were unreachable in practice.

Three sources, deliberately cross-checked rather than merged blindly:

* a **subtree dump** from a paired device, which is the device describing
  itself - every block type, parameter, range, default and picker list, in a
  shape that needs no guessing at all;
* the **Remote Editor bundle**, which has to enumerate every model to draw
  its own UI, and
* the **firmware image**, which contains the same data plus whatever the
  bundle leaves out.

The first is right by construction and the other two are inference from
minified code, so the subtree is merged first and the others only fill gaps.
Where they disagree, the disagreement is reported. A catalog that quietly
picked a winner would be worse than one that admits it does not know.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..rig.catalog import Catalog, ModelSpec, ParamSpec
from ..rig.model import BlockCategory

#: Keys a model-ish JSON object is likely to use for its identity.
_ID_KEYS = ("id", "modelId", "model_id", "uuid", "key", "slug")
_NAME_KEYS = ("name", "title", "displayName", "display_name", "label")
_CATEGORY_KEYS = ("category", "type", "blockType", "block_type", "kind", "class")
_PARAM_KEYS = ("params", "parameters", "controls", "knobs", "settings")

#: Category strings seen in the wild, mapped onto our own vocabulary.
_CATEGORY_ALIASES = {
    "amp": BlockCategory.AMP,
    "amplifier": BlockCategory.AMP,
    "cab": BlockCategory.CAB,
    "cabinet": BlockCategory.CAB,
    "speaker": BlockCategory.CAB,
    "ir": BlockCategory.IR,
    "impulse": BlockCategory.IR,
    "impulseresponse": BlockCategory.IR,
    "clone": BlockCategory.CLONE,
    "ampclone": BlockCategory.CLONE,
    "nam": BlockCategory.NAM,
    "effect": BlockCategory.EFFECT,
    "fx": BlockCategory.EFFECT,
    "pedal": BlockCategory.EFFECT,
}

_FAMILY_HINTS = {"revalver": "revalver", "headrush": "headrush"}

#: Smallest blob worth attempting to parse. Well below a single model entry,
#: because a catalog can legitimately be short and a missed extraction is far
#: worse than a few wasted parse attempts.
_MIN_BLOB_CHARS = 24


def find_json_blobs(text: str, *, limit: int = 500) -> list[str]:
    """Pull balanced JSON object/array literals out of arbitrary text.

    A regex cannot do this. Model catalogs are nested, and any non-greedy
    pattern stops at the first closing brace - so `{"a":{"b":1}}` yields
    `{"a":{"b":1}`, which fails to parse and silently drops the catalog.
    This scans with a depth counter instead, and skips over string literals
    so that braces inside them do not confuse the count.
    """
    blobs: list[str] = []
    index = 0
    length = len(text)

    while index < length and len(blobs) < limit:
        char = text[index]
        if char not in "{[":
            index += 1
            continue

        closing = "}" if char == "{" else "]"
        depth = 0
        in_string = False
        quote = ""
        escaped = False
        end = -1

        for cursor in range(index, length):
            current = text[cursor]

            if in_string:
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    in_string = False
                continue

            if current in "\"'":
                in_string = True
                quote = current
            elif current in "{[":
                depth += 1
            elif current in "}]":
                depth -= 1
                if depth == 0:
                    end = cursor
                    break

        if end == -1:
            index += 1
            continue

        candidate = text[index : end + 1]
        if len(candidate) >= _MIN_BLOB_CHARS and candidate.endswith(closing):
            blobs.append(candidate)
        # Skip past this literal rather than re-scanning inside it.
        index = end + 1

    return blobs


@dataclass
class ExtractionReport:
    """What an extraction found, and what it could not agree on."""

    catalog: Catalog
    sources: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def model_count(self) -> int:
        return len(self.catalog.models)

    def render(self) -> str:
        lines = [
            f"Extracted {self.model_count} model(s) from "
            f"{', '.join(self.sources) or 'nothing'}"
        ]
        lines.extend(self.catalog.completeness_report())

        if self.conflicts:
            lines.append(f"\n{len(self.conflicts)} conflict(s) between sources:")
            lines.extend(f"  {c}" for c in self.conflicts[:20])
            lines.append(
                "  Conflicts are reported, not resolved - a catalog that "
                "guessed would be worse than one that admits uncertainty."
            )
        lines.extend(f"\nnote: {n}" for n in self.notes)

        if not self.model_count:
            lines.append(
                "\nNothing was extracted. The bundle may be minified beyond "
                "these patterns, or the firmware container may not unpack. "
                "Validation stays in unverified mode, which is lenient but "
                "cannot check model ids or parameter ranges."
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "model_count": self.model_count,
            "sources": self.sources,
            "conflicts": self.conflicts,
            "notes": self.notes,
        }


def _first(mapping: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def _category_of(raw: Any) -> BlockCategory | None:
    if raw is None:
        return None
    token = re.sub(r"[^a-z]", "", str(raw).lower())
    return _CATEGORY_ALIASES.get(token)


def _family_of(blob: str) -> str | None:
    lowered = blob.lower()
    for hint, family in _FAMILY_HINTS.items():
        if hint in lowered:
            return family
    return None


def _param_from(raw: Any) -> ParamSpec | None:
    """Build a ParamSpec from whatever shape a parameter is described in."""
    if isinstance(raw, str):
        return ParamSpec(name=raw)
    if not isinstance(raw, dict):
        return None

    name = _first(raw, _NAME_KEYS) or _first(raw, _ID_KEYS)
    if not name:
        return None

    minimum = _first(raw, ("min", "minimum", "lo", "low"))
    maximum = _first(raw, ("max", "maximum", "hi", "high"))
    default = _first(raw, ("default", "defaultValue", "value", "init"))
    unit = _first(raw, ("unit", "units", "suffix"))
    choices = _first(raw, ("choices", "options", "values", "enum"))

    kind = "float"
    if isinstance(choices, list) and choices:
        kind = "enum"
    elif isinstance(default, bool):
        kind = "bool"

    def _number(value):
        return float(value) if isinstance(value, (int, float)) else None

    return ParamSpec(
        name=str(name),
        kind=kind,
        minimum=_number(minimum),
        maximum=_number(maximum),
        default=default if isinstance(default, (int, float, str, bool)) else None,
        unit=str(unit) if unit else None,
        choices=[str(c) for c in choices] if isinstance(choices, list) else [],
    )


def _model_from(raw: dict, *, source: str) -> ModelSpec | None:
    """Recognise a model-shaped object, or return None."""
    identifier = _first(raw, _ID_KEYS)
    name = _first(raw, _NAME_KEYS)
    category = _category_of(_first(raw, _CATEGORY_KEYS))

    if category is None or not (identifier or name):
        return None

    params: list[ParamSpec] = []
    raw_params = _first(raw, _PARAM_KEYS)
    if isinstance(raw_params, list):
        params = [p for p in (_param_from(item) for item in raw_params) if p]
    elif isinstance(raw_params, dict):
        for key, value in raw_params.items():
            spec = _param_from(value if isinstance(value, dict) else {"name": key})
            if spec is not None:
                spec.name = str(key)
                params.append(spec)

    return ModelSpec(
        id=str(identifier or name),
        name=str(name or identifier),
        category=category,
        family=_family_of(json.dumps(raw)[:400]),
        params=params,
        verified=True,
    )


def _walk(node: Any, *, source: str, found: dict[str, ModelSpec]) -> None:
    """Recursively hunt for model-shaped objects in decoded JSON."""
    if isinstance(node, dict):
        model = _model_from(node, source=source)
        if model is not None and model.id not in found:
            found[model.id] = model
        for value in node.values():
            _walk(value, source=source, found=found)
    elif isinstance(node, list):
        for item in node:
            _walk(item, source=source, found=found)


def from_json_text(text: str, *, source: str) -> Catalog:
    """Extract models from a JSON document, or from JSON embedded in JS."""
    catalog = Catalog(source=source)
    found: dict[str, ModelSpec] = {}

    try:
        _walk(json.loads(text), source=source, found=found)
    except json.JSONDecodeError:
        # A JS bundle: pull out the balanced literals and try each.
        for blob in find_json_blobs(text):
            try:
                _walk(json.loads(blob), source=source, found=found)
            except json.JSONDecodeError:
                continue

    catalog.models.update(found)
    return catalog


def from_files(paths: list[Path], *, source: str) -> Catalog:
    """Extract from a set of already-retrieved files."""
    catalog = Catalog(source=source)
    for path in paths:
        if path.suffix.lower() not in {".json", ".js", ".mjs", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        catalog.models.update(from_json_text(text, source=source).models)
    return catalog


def from_capture(capture_dir: str | Path) -> Catalog:
    """Extract from an archived Remote Editor bundle (``probe --dump-app``)."""
    root = Path(capture_dir)
    assets = root / "assets"
    search_root = assets if assets.is_dir() else root
    return from_files(sorted(search_root.rglob("*")), source="device")


def from_firmware(unpacked_dir: str | Path) -> Catalog:
    """Extract from an unpacked firmware tree."""
    root = Path(unpacked_dir)
    return from_files(sorted(root.rglob("*")), source="firmware")


# --------------------------------------------------------------------------
# the live object tree
# --------------------------------------------------------------------------

# The bundle and the firmware are both second-hand: they describe what the
# device would do. A subtree dump is the device answering for itself, and it
# turns out to carry the whole catalog in a shape nothing else does - every
# block type, every parameter, every range, every default, and the model
# pickers' full lists.

#: Where the device keeps one node per block type.
PATCH_PREFIX = "/Evil/Engine/Patch/"

#: Where it keeps the module table the chain indexes into.
BLOCKS_PATH = "/Evil/API/Blocks"

#: Properties that are the editor's own bookkeeping. ``order`` is the one that
#: looks most like a slot index and is not: it lists parameter *names*, in the
#: order the UI draws them.
_UI_ONLY = {"order", "labels", "disabled"}

#: Which parameter on a block enumerates models rather than describing a
#: control. These are the only pickers on the device: 53 amps behind
#: ``Amp.Type``, 15 cabs behind ``Cab.CabType``, and the ReValver equivalents.
_MODEL_PICKERS = {
    BlockCategory.AMP: "Type",
    BlockCategory.CAB: "CabType",
}

#: The device's own category names, mapped onto ours. Its vocabulary is finer
#: than ours - it separates Overdrive from Distortion from Delay - so the
#: extra detail is kept as a subcategory rather than thrown away.
_DEVICE_CATEGORIES = {
    "amp": BlockCategory.AMP,
    "cab": BlockCategory.CAB,
    "clone": BlockCategory.CLONE,
}

#: Nodes under Patch that describe the rig rather than sit in it. `Chain` is
#: the important one: its ModuleType1..14 *are* the slots, which makes it the
#: structure a rig is written into, not a block you can put in a slot.
_NOT_BLOCKS = {"Chain", "Rig", "CurrentBlock"}

#: Blocks whose own category is finer than the device's listing.
#:
#: The device files IR under "Cab" and the NAM player under "Clone", because
#: that is where a player looks for them. We keep them apart: an IR is not a
#: cab model, and a NAM capture is not HeadRush's own clone format - they load
#: different files and cost different CPU.
_BY_NAME = {
    "IR": BlockCategory.IR,
    "IR (1024)": BlockCategory.IR,
    "Neural Amp Modeler": BlockCategory.NAM,
    # Fixed ends of the signal path, not blocks anyone chooses.
    "Input": BlockCategory.INPUT,
    "Input Vocal": BlockCategory.INPUT,
    "Output": BlockCategory.OUTPUT,
    "Output Vocal": BlockCategory.OUTPUT,
    "Mix": BlockCategory.MIX,
}


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _unit_from_format(fmt) -> str | None:
    """Pull ``dB`` out of ``"%.1f dB"``, ``%`` out of ``"%.0f %%"``."""
    if not isinstance(fmt, str) or "f" not in fmt:
        return None
    trailing = fmt.rsplit("f", 1)[-1].strip().replace("%%", "%")
    return trailing or None


def param_from_meta(name: str, meta: dict) -> ParamSpec | None:
    """Turn one entry of a node's ``meta.properties`` into a ParamSpec."""
    if not isinstance(meta, dict):
        return None

    options = meta.get("x-options")
    options = options if isinstance(options, dict) else {}
    raw_choices = options.get("strings")
    choices = [str(c) for c in raw_choices] if isinstance(raw_choices, list) else []
    declared = str(meta.get("type", "number")).lower()

    # Order matters. `Doubling` is a boolean that happens to carry the labels
    # Single/Double, and `Type` is an integer that happens to carry 53 amp
    # names - neither is an enum, and treating them as one would reject every
    # legal value. The declared type wins; the strings are labels.
    if declared == "boolean":
        kind = "bool"
    elif declared == "integer":
        kind = "int"
    elif declared == "string":
        # No choices means a free string - a filename, usually. "enum" with an
        # empty choice list accepts anything, which is the honest answer.
        kind = "enum"
    elif choices:
        kind = "enum"
    else:
        kind = "float"

    default = options.get("default")
    if not isinstance(default, (int, float, str, bool)):
        default = None

    low, high = _number(meta.get("minimum")), _number(meta.get("maximum"))

    if kind == "float":
        # The published range is what the knob *reads*, not what goes on the
        # wire. Every continuous control on the Core is 0-1 in the protocol:
        # a Bass at 65% is 0.65, and the 0..100 sitting next to it in the
        # metadata is a display hint. Recording it as the valid range would
        # let `Bass = 65` validate and then peg the control on the device.
        return ParamSpec(
            name=name,
            kind=kind,
            minimum=0.0,
            maximum=1.0,
            default=default,
            unit=_unit_from_format(options.get("format")),
            choices=choices,
            display_min=low,
            display_max=high,
        )

    return ParamSpec(
        name=name,
        kind=kind,
        minimum=low,
        maximum=high,
        default=default,
        unit=_unit_from_format(options.get("format")),
        choices=choices,
    )


def _module_table(subtree: dict) -> dict[str, int]:
    """Name -> index, from the device's own module table."""
    node = subtree.get(BLOCKS_PATH) or {}
    value = node.get("value") if isinstance(node, dict) else None
    names = value.get("ModuleTypes") if isinstance(value, dict) else None
    if not isinstance(names, list):
        return {}
    return {str(name): index for index, name in enumerate(names)}


def _key(name: str) -> str:
    """Loose comparison key - the two listings punctuate differently."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _category_lookup(categories: dict | None) -> dict[str, str]:
    """Block display name -> the device's category name.

    The two listings name the same block differently. A node is ``Amp``; the
    category listing calls it ``Amp (HeadRush)`` and its ReValver sibling
    ``Amp (ReValver)``, while the node for that is ``ReValver_Amp``. So each
    entry is registered under every spelling it might be looked up by.
    """
    if not isinstance(categories, dict):
        return {}

    lookup: dict[str, str] = {}
    for category, blocks in categories.items():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            block = str(block)
            lookup.setdefault(_key(block), str(category))
            qualified = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", block)
            if qualified:
                base, family = qualified.group(1), qualified.group(2)
                lookup.setdefault(_key(base), str(category))
                lookup.setdefault(_key(f"{family} {base}"), str(category))
    return lookup


def from_subtree(
    subtree: str | Path | dict,
    *,
    categories: str | Path | dict | None = None,
    source: str = "device",
) -> Catalog:
    """Extract the catalog from a live object-tree dump.

    ``GET /subtree/`` returns one node per block type under
    ``/Evil/Engine/Patch/``, each carrying its own ``meta.properties``: every
    parameter with its range, its default, and - for the two model pickers -
    the full list of names the device will accept. Cross-referenced with the
    module table at ``/Evil/API/Blocks``, that is the whole catalog, correct
    by construction rather than by inference from a minified bundle.

    Args:
        subtree: A decoded subtree dump, or a path to one.
        categories: The device's own category listing (block names grouped by
            category), or a path to one. Optional, but without it every block
            lands in the generic effect category.
        source: Recorded on the catalog for provenance.
    """
    subtree = _load_json(subtree, "subtree dump")
    categories = _load_json(categories, "category listing") if categories else None

    modules = _module_table(subtree)
    by_block = _category_lookup(categories)
    catalog = Catalog(source=source)

    for path, node in subtree.items():
        if not path.startswith(PATCH_PREFIX) or not isinstance(node, dict):
            continue
        block = path[len(PATCH_PREFIX) :]
        if block in _NOT_BLOCKS:
            continue
        if block.endswith("_2"):
            # The device carries two instances of every block type so a rig
            # can hold two of them. They are identical in every respect that
            # a catalog records, so cataloguing both would double every entry
            # and tell nobody anything.
            continue

        properties = (node.get("meta") or {}).get("properties")
        if not isinstance(properties, dict):
            continue

        # Node names are the display name with spaces replaced: `Green_JRC-OD`.
        display = block.replace("_", " ")
        device_category = by_block.get(_key(display))
        category = _BY_NAME.get(display) or _DEVICE_CATEGORIES.get(
            (device_category or "").lower(), BlockCategory.EFFECT
        )

        params = [
            spec
            for name, meta in sorted(properties.items())
            if name not in _UI_ONLY
            for spec in [param_from_meta(name, meta)]
            if spec is not None
        ]

        catalog.models[display] = ModelSpec(
            id=display,
            name=display,
            category=category,
            family=_family_of(display) or ("headrush" if display in modules else None),
            subcategory=device_category.lower() if device_category else None,
            params=params,
            verified=True,
            device_index=modules.get(display),
        )

        # And every model behind this block's picker, if it has one. These are
        # not modules - you cannot put `82 Lead 800 100W` in a slot - so each
        # records the module and parameter that select it.
        picker = _MODEL_PICKERS.get(category)
        if not picker:
            continue
        offered = properties.get(picker)
        names = ((offered or {}).get("x-options") or {}).get("strings") if isinstance(offered, dict) else None
        if not isinstance(names, list):
            continue

        # The picker itself is not a control, and neither is its second half.
        controls = [p for p in params if p.name not in (picker, f"{picker}2")]
        for index, model_name in enumerate(names):
            model_name = str(model_name)
            catalog.models[model_name] = ModelSpec(
                id=model_name,
                name=model_name,
                category=category,
                family="revalver" if "revalver" in display.lower() else "headrush",
                params=controls,
                verified=True,
                device_index=index,
                selector=f"{display}.{picker}",
            )

    return catalog


def _load_json(value, what: str) -> dict:
    if isinstance(value, dict):
        return value
    # The device serves UTF-8 and the Core's model names include characters
    # that are not in the Windows default codec - reading without saying so
    # fails partway through a 1.1MB dump.
    decoded = json.loads(Path(value).read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError(f"a {what} should decode to an object, got {type(decoded).__name__}")
    return decoded


def from_device_capture(capture_dir: str | Path) -> Catalog:
    """Extract from a live capture directory (``subtree.json`` and friends)."""
    root = Path(capture_dir)
    subtree = root / "subtree.json"
    if not subtree.is_file():
        raise ValueError(
            f"{root} has no subtree.json - that is what a live capture is. "
            "Use --capture for an archived Remote Editor bundle instead."
        )
    categories = root / "device-catalog.json"
    return from_subtree(subtree, categories=categories if categories.is_file() else None)


def extract(
    *,
    capture_dir: str | Path | None = None,
    firmware_dir: str | Path | None = None,
    subtree_dir: str | Path | None = None,
    save_to: str | Path | None = None,
) -> ExtractionReport:
    """Build a catalog from whichever sources are available.

    Args:
        capture_dir: An archived Remote Editor bundle.
        firmware_dir: An unpacked firmware tree.
        subtree_dir: A live capture directory holding ``subtree.json`` - the
            device answering for itself, and the best of the three.
        save_to: Where to write the merged catalog. Defaults to the
            workspace, which is where :func:`rigsmith.rig.catalog.get_catalog`
            looks.
    """
    from datetime import datetime, timezone

    from ..config import get_settings, require_writable

    merged = Catalog(source="merged")
    report = ExtractionReport(catalog=merged)

    # Merged first, deliberately. `merge` keeps what it already holds when
    # both sides are verified, so the source that was right by construction
    # gets to set the terms and the inferred ones only fill gaps.
    if subtree_dir:
        live = from_device_capture(subtree_dir)
        report.conflicts.extend(merged.merge(live))
        report.sources.append(f"live subtree ({len(live.models)} models)")

    if capture_dir:
        device_catalog = from_capture(capture_dir)
        report.conflicts.extend(merged.merge(device_catalog))
        report.sources.append(f"device bundle ({len(device_catalog.models)} models)")

    if firmware_dir:
        firmware_catalog = from_firmware(firmware_dir)
        report.conflicts.extend(merged.merge(firmware_catalog))
        report.sources.append(f"firmware ({len(firmware_catalog.models)} models)")

    if not report.sources:
        raise ValueError(
            "nothing to extract from - give a live capture directory, an "
            "archived bundle, an unpacked firmware tree, or any combination"
        )

    merged.captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if merged.models:
        require_writable("save the extracted catalog")
        settings = get_settings()
        settings.ensure_workspace()
        destination = Path(save_to) if save_to else settings.workspace / "catalog.json"
        merged.save(destination)
        report.notes.append(f"saved to {destination}")
        report.notes.append(
            "validation will now check model ids and parameter ranges against "
            "this catalog instead of running in lenient unverified mode"
        )

    return report

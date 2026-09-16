"""Harvest the real model catalog from the device and the firmware.

The Core ships roughly 97 amp models, each with its own knob set, and no
per-model parameter table is published anywhere. Until this existed,
:mod:`rigsmith.rig.catalog` could only ever serve its seed - which meant
validation ran permanently in "unverified" mode and the parameter-range
checks, the part of the safety net meant to stop a confidently wrong write,
were unreachable in practice.

Two sources, deliberately cross-checked rather than merged blindly:

* the **Remote Editor bundle**, which has to enumerate every model to draw
  its own UI, and
* the **firmware image**, which contains the same data plus whatever the
  bundle leaves out.

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


def extract(
    *,
    capture_dir: str | Path | None = None,
    firmware_dir: str | Path | None = None,
    save_to: str | Path | None = None,
) -> ExtractionReport:
    """Build a catalog from whichever sources are available.

    Args:
        capture_dir: An archived Remote Editor bundle.
        firmware_dir: An unpacked firmware tree.
        save_to: Where to write the merged catalog. Defaults to the
            workspace, which is where :func:`rigsmith.rig.catalog.get_catalog`
            looks.
    """
    from datetime import datetime, timezone

    from ..config import get_settings, require_writable

    merged = Catalog(source="merged")
    report = ExtractionReport(catalog=merged)

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
            "nothing to extract from - give a capture directory, a firmware "
            "directory, or both"
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

"""The model catalog: what blocks exist and what parameters each one takes.

**This catalog is meant to be extracted, not written by hand.** The Core ships
roughly 97 amp models and 82 cabs, and each amp model has its own knob set -
there is no universal amp schema and no published per-model parameter table.
Transcribing that by hand would be both enormous and wrong within one firmware
release.

So :mod:`rigsmith.protocol.catalog_extract` harvests the real catalog from the
device and the firmware, and this module loads it. Until that has run, a small
seed catalog provides the *shape* of the data - categories, families, counts
from the vendor's published model list - so the rest of the system can be
built and tested. Anything from the seed is marked ``verified=False`` and
callers are expected to say so rather than present it as fact.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .model import BlockCategory


@dataclass
class ParamSpec:
    """One parameter of one model.

    :attr:`minimum` and :attr:`maximum` are the range **on the wire**, which
    for the Core's continuous controls is 0-1 regardless of what the knob
    says. A Bass control reading 65% is ``0.65`` in the protocol, and the
    ``0..100`` the device publishes alongside it is what to print, not what to
    send. Sending 65 would peg it.
    """

    name: str
    kind: str = "float"
    """``float``, ``int``, ``bool``, or ``enum``."""
    minimum: float | None = None
    maximum: float | None = None
    default: float | str | bool | None = None
    unit: str | None = None
    choices: list[str] = field(default_factory=list)
    display_min: float | None = None
    display_max: float | None = None
    """What the device shows at each end of the wire range."""
    taper: str = "unknown"
    """How wire maps to display: ``linear``, ``log``, or ``unknown``.

    Not inferable from the metadata, and not guessable from the unit either -
    ``MidFreq`` is linear across 220-3000 Hz while ``SltEQF`` is logarithmic
    across 20-16000 Hz, and both are published identically. It is measured by
    :func:`rigsmith.protocol.calibrate.taper_of` against a live device, and
    stays ``unknown`` until it has been. Conversions refuse rather than guess.
    """

    @property
    def normalised(self) -> bool:
        """True when the wire range is 0-1 but the control is not."""
        return (
            self.kind == "float"
            and (self.minimum, self.maximum) == (0.0, 1.0)
            and self.display_max is not None
            and self.display_max != 1.0
        )

    def to_display(self, wire: float) -> float:
        """Turn a wire value into what the device would print."""
        if self.display_min is None or self.display_max is None:
            return wire
        if self.taper == "linear":
            return self.display_min + wire * (self.display_max - self.display_min)
        if self.taper == "log" and self.display_min > 0:
            return self.display_min * (self.display_max / self.display_min) ** wire
        raise ValueError(
            f"{self.name}: the taper has not been measured, so a wire value "
            "cannot be turned into a reading. Run 'rigsmith catalog calibrate' "
            "against a paired device."
        )

    def to_wire(self, display: float) -> float:
        """Turn a reading into the value to send. The inverse of the above."""
        if self.display_min is None or self.display_max is None:
            return display
        if self.taper == "linear":
            span = self.display_max - self.display_min
            return 0.0 if span == 0 else (display - self.display_min) / span
        if self.taper == "log" and self.display_min > 0 and display > 0:
            import math

            ratio = self.display_max / self.display_min
            return math.log(display / self.display_min) / math.log(ratio)
        raise ValueError(
            f"{self.name}: the taper has not been measured, so a reading "
            "cannot be turned into a value to send. Run "
            "'rigsmith catalog calibrate' against a paired device."
        )

    def validate(self, value) -> str | None:
        """Return an error message, or ``None`` if the value is acceptable."""
        if self.kind == "bool":
            if not isinstance(value, bool):
                return f"{self.name}: expected true/false, got {value!r}"
            return None
        if self.kind == "enum":
            if self.choices and str(value) not in self.choices:
                return (
                    f"{self.name}: {value!r} is not one of "
                    f"{', '.join(self.choices[:8])}"
                )
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"{self.name}: expected a number, got {value!r}"
        if self.kind == "int" and float(value) != int(value):
            return f"{self.name}: expected a whole number, got {value}"
        if self.minimum is not None and value < self.minimum:
            return f"{self.name}: {value} below minimum {self.minimum}"
        if self.maximum is not None and value > self.maximum:
            return f"{self.name}: {value} above maximum {self.maximum}"
        return None


@dataclass
class ModelSpec:
    """One amp, cab, IR, or effect model."""

    id: str
    name: str
    category: BlockCategory
    family: str | None = None
    """``headrush`` or ``revalver`` for amps and cabs."""
    subcategory: str | None = None
    """e.g. ``overdrive``, ``delay``, ``reverb`` for effects."""
    params: list[ParamSpec] = field(default_factory=list)
    cpu_cost: float | None = None
    """Estimated share of the device CPU meter, 0-1. ``None`` if unknown."""
    verified: bool = False
    """True only when this entry came from the device or firmware, not the seed."""
    device_index: int | None = None
    """The number the device itself uses for this model.

    A rig's chain is fourteen integers, not fourteen names: slot six holding
    an amp is ``Chain.ModuleType6 = 1``, because ``1`` is where ``Amp`` sits
    in the device's module table. Knowing the name is not enough to write a
    rig - this is the number that actually goes on the wire.
    """
    selector: str | None = None
    """How to select this model, when it is not a module in its own right.

    The 53 amp models are not modules; they are values of the ``Type``
    parameter on the one ``Amp`` module. Such an entry carries
    ``selector="Amp.Type"``, meaning: put ``Amp`` in the slot, then set its
    ``Type`` to :attr:`device_index`. ``None`` for anything that is itself a
    module.
    """

    def param(self, name: str) -> ParamSpec | None:
        lowered = name.lower()
        return next((p for p in self.params if p.name.lower() == lowered), None)


# --------------------------------------------------------------------------
# CPU cost model
# --------------------------------------------------------------------------

#: Rough per-block CPU shares, as a fraction of the device's meter.
#:
#: The NAM figures come from community measurement on a Core: an empty rig
#: sits near 7%, one NAM capture takes it to about 30% (Full) or 24% (Lite).
#: Those are single-test anecdotes, not vendor specs, so these are used for
#: *warnings* only - the device's own CPU meter remains the authority.
BASELINE_CPU = 0.07

DEFAULT_CPU_COST = {
    BlockCategory.AMP: 0.08,
    BlockCategory.CAB: 0.03,
    BlockCategory.IR: 0.03,
    BlockCategory.CLONE: 0.10,
    BlockCategory.NAM: 0.23,
    BlockCategory.EFFECT: 0.03,
    BlockCategory.INPUT: 0.0,
    BlockCategory.OUTPUT: 0.0,
    BlockCategory.MIX: 0.01,
}

#: A2 Lite measured ~6 points cheaper than Full on the CPU meter.
NAM_LITE_DISCOUNT = 0.06

#: A truncated 1024-sample IR costs materially less than the standard 2048.
IR_TRUNCATED_DISCOUNT = 0.015


# --------------------------------------------------------------------------
# Seed catalog
# --------------------------------------------------------------------------

#: Published model counts per family, used to sanity-check an extraction and
#: to tell the user how complete their catalog is. From the vendor's model
#: list, which changes with firmware - treat as approximate.
PUBLISHED_COUNTS = {
    ("amp", "headrush"): 53,
    ("amp", "revalver"): 44,
    ("cab", "headrush"): 15,
    ("cab", "revalver"): 67,
}

#: Effect categories and their published counts.
PUBLISHED_EFFECT_COUNTS = {
    "vocal": 7,
    "overdrive": 16,
    "distortion_fuzz": 11,
    "eq": 9,
    "compressor": 6,
    "delay": 14,
    "reverb": 7,
    "chorus": 7,
    "phaser_flanger": 10,
    "wah_filter": 8,
    "synth": 4,
    "pitch": 7,
    "volume_dynamics": 5,
    "utility": 3,
}

#: Parameters present on every block via the advanced block edit page.
UNIVERSAL_PARAMS = [
    ParamSpec("input_gain", "float", -12.0, 12.0, 0.0, "dB"),
    ParamSpec("output_gain", "float", -12.0, 12.0, 0.0, "dB"),
]


@dataclass
class Catalog:
    """All known models, however they were obtained."""

    models: dict[str, ModelSpec] = field(default_factory=dict)
    source: str = "seed"
    """``seed``, ``device``, ``firmware``, or ``merged``."""
    firmware: str | None = None
    captured_at: str | None = None
    warnings: list[str] = field(default_factory=list)

    # -- lookup -----------------------------------------------------------

    def get(self, model_id: str) -> ModelSpec | None:
        direct = self.models.get(model_id)
        if direct is not None:
            return direct
        lowered = model_id.lower()
        return next(
            (m for m in self.models.values() if m.id.lower() == lowered or m.name.lower() == lowered),
            None,
        )

    def search(
        self,
        query: str = "",
        *,
        category: BlockCategory | None = None,
        family: str | None = None,
        limit: int = 50,
    ) -> list[ModelSpec]:
        """Substring search over model id and name, newest-agnostic."""
        terms = [t for t in query.lower().split() if t]
        results = []
        for model in self.models.values():
            if category is not None and model.category is not category:
                continue
            if family is not None and (model.family or "").lower() != family.lower():
                continue
            haystack = f"{model.id} {model.name} {model.subcategory or ''}".lower()
            if all(t in haystack for t in terms):
                results.append(model)
        results.sort(key=lambda m: (m.category.value, m.name))
        return results[:limit]

    @property
    def is_verified(self) -> bool:
        """True when every model came from the device or firmware."""
        return bool(self.models) and all(m.verified for m in self.models.values())

    def share_tapers(self, module: str) -> int:
        """Give every model selected on a module the curves measured on it.

        The 53 amp models are values of one ``Amp`` block's ``Type``: their
        knobs are that block's knobs, described by the same node, so a curve
        measured there is theirs too. Only a control with the same name and
        the same displayed range is given one. Returns how many models changed.
        """
        source = self.models.get(module)
        if source is None:
            return 0
        known = {p.name: p for p in source.params if p.taper in ("linear", "log")}
        changed = 0
        for spec in self.models.values():
            if spec is source or not spec.selector or spec.selector.split(".", 1)[0] != module:
                continue
            touched = False
            for param in spec.params:
                measured = known.get(param.name)
                if measured is None or param.taper == measured.taper:
                    continue
                if (param.display_min, param.display_max) != (measured.display_min, measured.display_max):
                    continue
                param.taper = measured.taper
                touched = True
            changed += touched
        return changed

    def share_measured_tapers(self) -> int:
        """:meth:`share_tapers` for every module that has a measured curve."""
        modules = {spec.selector.split(".", 1)[0] for spec in self.models.values() if spec.selector}
        return sum(self.share_tapers(module) for module in sorted(modules))

    def coverage(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for model in self.models.values():
            key = f"{model.category.value}:{model.family or 'any'}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def completeness_report(self) -> list[str]:
        """Compare what we hold against the vendor's published counts."""
        lines = []
        counts = self.coverage()
        for (category, family), expected in sorted(PUBLISHED_COUNTS.items()):
            have = counts.get(f"{category}:{family}", 0)
            mark = "ok" if have >= expected else "--"
            lines.append(f"  {mark}  {category}/{family}: {have}/{expected}")
        return lines

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "firmware": self.firmware,
            "captured_at": self.captured_at,
            "warnings": self.warnings,
            "models": [
                {**asdict(m), "category": m.category.value}
                for m in self.models.values()
            ],
        }

    def save(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        catalog = cls(
            source=data.get("source", "unknown"),
            firmware=data.get("firmware"),
            captured_at=data.get("captured_at"),
            warnings=data.get("warnings", []),
        )
        for raw in data.get("models", []):
            # Tolerate catalogs written before a field existed.
            allowed = {f.name for f in fields(ParamSpec)}
            params = [
                ParamSpec(**{k: v for k, v in p.items() if k in allowed})
                for p in raw.get("params", [])
            ]
            model = ModelSpec(
                id=raw["id"],
                name=raw["name"],
                category=BlockCategory(raw["category"]),
                family=raw.get("family"),
                subcategory=raw.get("subcategory"),
                params=params,
                cpu_cost=raw.get("cpu_cost"),
                verified=raw.get("verified", False),
                device_index=raw.get("device_index"),
                selector=raw.get("selector"),
            )
            catalog.models[model.id] = model
        return catalog

    def merge(self, other: "Catalog") -> list[str]:
        """Merge another catalog in, preferring verified entries.

        Returns a list of conflicts. Disagreements are reported rather than
        silently resolved - if the firmware and the device disagree about a
        model, that is something a human needs to see.
        """
        conflicts: list[str] = []
        for model_id, incoming in other.models.items():
            existing = self.models.get(model_id)
            if existing is None:
                self.models[model_id] = incoming
                continue
            if existing.verified and incoming.verified and existing.name != incoming.name:
                conflicts.append(
                    f"{model_id}: '{existing.name}' ({self.source}) vs "
                    f"'{incoming.name}' ({other.source})"
                )
            if incoming.verified and not existing.verified:
                self.models[model_id] = incoming
            elif incoming.verified and len(incoming.params) > len(existing.params):
                self.models[model_id] = incoming
        self.source = "merged"
        self.warnings.extend(conflicts)
        return conflicts


def seed_catalog() -> Catalog:
    """A minimal, explicitly unverified catalog.

    This exists so the rig model, validation, and MCP layers can be built and
    tested before a device is available. It contains the universal parameters
    and the effect taxonomy - it does **not** invent per-model knob sets, and
    every entry is marked ``verified=False``.
    """
    catalog = Catalog(source="seed")
    catalog.warnings.append(
        "Seed catalog: model names and per-model parameters have NOT been "
        "extracted from a device. Run 'rigsmith catalog extract' against real "
        "hardware before trusting any model id or parameter range."
    )

    # Generic placeholders per category, carrying only the universal params.
    for category in (
        BlockCategory.AMP,
        BlockCategory.CAB,
        BlockCategory.IR,
        BlockCategory.CLONE,
        BlockCategory.NAM,
    ):
        catalog.models[f"generic:{category.value}"] = ModelSpec(
            id=f"generic:{category.value}",
            name=f"Generic {category.value}",
            category=category,
            params=list(UNIVERSAL_PARAMS),
            cpu_cost=DEFAULT_CPU_COST[category],
            verified=False,
        )

    for subcategory in PUBLISHED_EFFECT_COUNTS:
        catalog.models[f"generic:{subcategory}"] = ModelSpec(
            id=f"generic:{subcategory}",
            name=f"Generic {subcategory.replace('_', ' ')}",
            category=BlockCategory.EFFECT,
            subcategory=subcategory,
            params=list(UNIVERSAL_PARAMS),
            cpu_cost=DEFAULT_CPU_COST[BlockCategory.EFFECT],
            verified=False,
        )

    return catalog


_cached: Catalog | None = None


def get_catalog(path: Path | None = None, *, reload: bool = False) -> Catalog:
    """Load the extracted catalog if present, else fall back to the seed."""
    global _cached
    if _cached is not None and not reload:
        return _cached

    if path is None:
        from ..config import get_settings

        path = get_settings().workspace / "catalog.json"

    path = Path(path)
    _cached = Catalog.load(path) if path.exists() else seed_catalog()
    # Curves measured on a module before sharing existed are the models' too.
    _cached.share_measured_tapers()
    return _cached

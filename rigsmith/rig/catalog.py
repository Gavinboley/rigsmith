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
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .model import BlockCategory


@dataclass
class ParamSpec:
    """One parameter of one model."""

    name: str
    kind: str = "float"
    """``float``, ``int``, ``bool``, or ``enum``."""
    minimum: float | None = None
    maximum: float | None = None
    default: float | str | bool | None = None
    unit: str | None = None
    choices: list[str] = field(default_factory=list)

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

    def by_category(self, category: BlockCategory) -> list[ModelSpec]:
        return [m for m in self.models.values() if m.category is category]

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
            params = [ParamSpec(**p) for p in raw.get("params", [])]
            model = ModelSpec(
                id=raw["id"],
                name=raw["name"],
                category=BlockCategory(raw["category"]),
                family=raw.get("family"),
                subcategory=raw.get("subcategory"),
                params=params,
                cpu_cost=raw.get("cpu_cost"),
                verified=raw.get("verified", False),
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
    return _cached

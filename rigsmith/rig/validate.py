"""Validate a rig before anything is written to the device.

The device is the final authority on what is legal. This module exists to
catch the mistakes that are knowable in advance - a 15th block, a scene
pointing at an empty slot, a parameter outside its range, a CPU budget that
will not fit - so an agent gets a useful correction instead of a rig that
half-loads.

Findings carry a severity:

``error``
    Will not work. Writes are blocked.
``warning``
    Legal but probably not intended, or resting on an estimate rather than a
    measurement. Writes proceed.
``info``
    Worth knowing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import (
    BASELINE_CPU,
    DEFAULT_CPU_COST,
    IR_TRUNCATED_DISCOUNT,
    NAM_LITE_DISCOUNT,
    Catalog,
    get_catalog,
)
from .model import (
    FOOTSWITCH_COUNT,
    GAIN_RANGE_DB,
    MAX_SLOTS,
    Block,
    BlockCategory,
    Doubling,
    Rig,
    SceneBlockState,
)

#: Above this estimated CPU share we warn. The device tolerates a full meter
#: badly - pops and clicks - so leaving headroom matters.
CPU_WARN_THRESHOLD = 0.85
CPU_ERROR_THRESHOLD = 1.0


@dataclass
class Finding:
    """One validation result."""

    severity: str
    message: str
    slot: int | None = None
    scene: str | None = None

    def __str__(self) -> str:
        where = ""
        if self.slot is not None:
            where = f" [slot {self.slot}]"
        elif self.scene is not None:
            where = f" [scene {self.scene}]"
        return f"{self.severity}:{where} {self.message}"


@dataclass
class ValidationResult:
    """The outcome of validating a rig."""

    findings: list[Finding] = field(default_factory=list)
    estimated_cpu: float = 0.0
    unknown_models: list[tuple[int, str]] = field(default_factory=list)
    """(slot, model_id) pairs absent from an unverified catalog, reported
    once as a group rather than as one warning per block."""

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, severity: str, message: str, **where) -> None:
        self.findings.append(Finding(severity, message, **where))

    def report(self) -> str:
        if not self.findings:
            return f"Valid. Estimated CPU ~{self.estimated_cpu:.0%}."
        lines = [str(f) for f in self.findings]
        lines.append(f"Estimated CPU ~{self.estimated_cpu:.0%}.")
        return "\n".join(lines)


def estimate_cpu(rig: Rig, catalog: Catalog | None = None) -> float:
    """Estimate the device CPU meter for this rig, as a 0-1 fraction.

    This is an estimate built on community measurements, not a vendor spec.
    It is good enough to catch "this will not fit" before writing, but the
    device's own meter is what actually counts.
    """
    catalog = catalog or get_catalog()
    total = BASELINE_CPU

    for block in rig.blocks:
        if not block.enabled:
            # A bypassed block still occupies its slot, but costs far less.
            continue

        spec = catalog.get(block.model_id)
        cost = spec.cpu_cost if spec and spec.cpu_cost is not None else None
        if cost is None:
            cost = DEFAULT_CPU_COST.get(block.category, 0.03)

        if block.category is BlockCategory.NAM:
            size = str(block.params.get("size", "")).lower()
            if "lite" in size or "nano" in size or "feather" in size:
                cost = max(0.0, cost - NAM_LITE_DISCOUNT)
        elif block.category is BlockCategory.IR:
            length = block.params.get("length") or block.params.get("samples")
            if str(length) in {"1024", "truncated"}:
                cost = max(0.0, cost - IR_TRUNCATED_DISCOUNT)

        # Stereo doubling runs two instances; switch doubling runs one.
        if block.doubling is Doubling.STEREO:
            cost *= 2

        total += cost

    return total


def _validate_block(
    block: Block, rig: Rig, catalog: Catalog, result: ValidationResult
) -> None:
    slot = block.slot

    if not 1 <= slot <= MAX_SLOTS:
        result.add("error", f"slot {slot} is outside 1..{MAX_SLOTS}", slot=slot)

    low, high = GAIN_RANGE_DB
    for label, value in (
        ("input gain", block.input_gain_db),
        ("output gain", block.output_gain_db),
    ):
        if not low <= value <= high:
            result.add(
                "error",
                f"{label} {value} dB is outside {low}..{high} dB",
                slot=slot,
            )

    if block.doubling is not Doubling.NONE:
        if block.category not in {
            BlockCategory.AMP,
            BlockCategory.CAB,
            BlockCategory.IR,
            BlockCategory.CLONE,
            BlockCategory.NAM,
        }:
            result.add(
                "error",
                f"2X doubling is only available on amp/cab/IR/clone/NAM blocks, "
                f"not {block.category.value}",
                slot=slot,
            )
        if not block.doubled_model_id:
            result.add(
                "error",
                f"doubling is set to {block.doubling.value} but no second model given",
                slot=slot,
            )

    if rig.signal_path.is_split and block.lane not in (None, 0, 1):
        result.add("error", f"lane {block.lane} is not 0 or 1", slot=slot)
    if not rig.signal_path.is_split and block.lane is not None:
        result.add(
            "warning",
            "block has a lane assigned but the rig uses a straight path",
            slot=slot,
        )

    # A lane only means something inside the split section. Outside it the
    # signal is common to both paths, so a lane there is a misunderstanding
    # of the routing rather than a harmless extra field.
    if rig.signal_path.is_split and block.lane is not None:
        pre, per_lane, _post = rig.signal_path.layout
        split_start = pre + 1
        split_end = pre + per_lane * 2
        if not split_start <= slot <= split_end:
            result.add(
                "error",
                f"lane {block.lane} is set, but slot {slot} is outside the "
                f"split section (slots {split_start}-{split_end} on a "
                f"{rig.signal_path.value} path) where the signal is common "
                "to both lanes",
                slot=slot,
            )

    spec = catalog.get(block.model_id)
    if spec is None:
        if catalog.is_verified:
            result.add(
                "error",
                f"model '{block.model_id}' is not in the catalog",
                slot=slot,
            )
        # Against an unverified catalog every real model id is "unknown",
        # so the caller collects these and reports them once rather than
        # burying the useful findings under one warning per block.
        else:
            result.unknown_models.append((slot, block.model_id))
        return

    if spec.category is not block.category:
        result.add(
            "error",
            f"model '{block.model_id}' is a {spec.category.value}, "
            f"but the block is declared {block.category.value}",
            slot=slot,
        )

    for name, value in block.params.items():
        param = spec.param(name)
        if param is None:
            if spec.verified:
                result.add(
                    "warning",
                    f"'{name}' is not a known parameter of {spec.name}",
                    slot=slot,
                )
            continue
        error = param.validate(value)
        if error:
            result.add("error", error, slot=slot)


def _validate_scenes(rig: Rig, result: ValidationResult) -> None:
    used_slots = rig.used_slots
    footswitch_scenes: dict[int, list[str]] = {}

    for scene in rig.scenes:
        for slot, state in scene.states.items():
            if slot not in used_slots:
                result.add(
                    "error",
                    f"references slot {slot}, which holds no block",
                    scene=scene.name,
                )
            elif state is SceneBlockState.NO_CHANGE:
                result.add(
                    "info",
                    f"slot {slot} is explicitly NO_CHANGE, which is the default",
                    scene=scene.name,
                )

        for slot in scene.presets:
            if slot not in used_slots:
                result.add(
                    "error",
                    f"recalls a preset for slot {slot}, which holds no block",
                    scene=scene.name,
                )

        if scene.footswitch is not None:
            if not 1 <= scene.footswitch <= FOOTSWITCH_COUNT:
                result.add(
                    "error",
                    f"footswitch {scene.footswitch} is outside 1..{FOOTSWITCH_COUNT}",
                    scene=scene.name,
                )
            else:
                footswitch_scenes.setdefault(scene.footswitch, []).append(scene.name)

        if not scene.states and not scene.presets:
            result.add(
                "warning",
                "scene changes nothing - every block is NO_CHANGE",
                scene=scene.name,
            )

    # A footswitch toggles between at most two scene states.
    for footswitch, names in footswitch_scenes.items():
        if len(names) > 2:
            result.add(
                "error",
                f"footswitch {footswitch} has {len(names)} scenes assigned "
                f"({', '.join(names)}); a footswitch toggles between at most two",
            )

    names = [s.name.lower() for s in rig.scenes]
    duplicates = {n for n in names if names.count(n) > 1}
    for name in sorted(duplicates):
        result.add("error", f"duplicate scene name '{name}'")


def validate_rig(rig: Rig, catalog: Catalog | None = None) -> ValidationResult:
    """Check a rig and return every finding.

    Never raises for rig content - the caller decides what to do with errors.
    """
    catalog = catalog or get_catalog()
    result = ValidationResult()

    if not rig.name or not rig.name.strip():
        result.add("error", "rig has no name")

    # Slots
    slots = [b.slot for b in rig.blocks]
    duplicates = {s for s in slots if slots.count(s) > 1}
    for slot in sorted(duplicates):
        result.add("error", "more than one block occupies this slot", slot=slot)

    if len(rig.blocks) > MAX_SLOTS:
        result.add(
            "error",
            f"rig has {len(rig.blocks)} blocks but only {MAX_SLOTS} slots exist",
        )

    if not 0 <= rig.rig_width <= 100:
        result.add("error", f"rig width {rig.rig_width} is outside 0..100")

    for block in rig.blocks:
        _validate_block(block, rig, catalog, result)

    _validate_scenes(rig, result)

    # Musical sanity, as warnings only - these are conventions, not rules.
    if rig.blocks and not rig.tone_sources:
        result.add(
            "warning",
            "no amp, clone, or NAM block - the rig has no tone source",
        )

    has_speaker = any(
        b.category in {BlockCategory.CAB, BlockCategory.IR} for b in rig.blocks
    )
    if rig.tone_sources and not has_speaker:
        # Amp models and captures need opposite advice here, and saying the
        # same thing about both gets one of them wrong. A device amp model is
        # the amp alone, so it always wants a speaker after it. A NAM capture
        # might be an amp, or might be the whole miked rig - and putting an IR
        # after a full-rig capture is the more common mistake of the two.
        modelled = [
            b for b in rig.tone_sources if b.category in {BlockCategory.AMP,
                                                          BlockCategory.CLONE}
        ]
        captures = [b for b in rig.tone_sources if b.category is BlockCategory.NAM]

        if modelled:
            result.add(
                "warning",
                "an amp block with no cab or IR after it will sound harsh into "
                "a full-range output",
                slot=modelled[0].slot,
            )
        if captures and not modelled:
            result.add(
                "info",
                "no cab or IR after the capture. Whether that is wrong depends "
                "on what was captured: an amp-only capture wants one, and a "
                "full-rig capture already has the cab and mic baked in, where "
                "adding an IR would cab it twice",
                slot=captures[0].slot,
            )

    for source in rig.tone_sources:
        later_speaker = any(
            b.slot > source.slot
            and b.category in {BlockCategory.CAB, BlockCategory.IR}
            for b in rig.blocks
        )
        earlier_speaker = any(
            b.slot < source.slot
            and b.category in {BlockCategory.CAB, BlockCategory.IR}
            for b in rig.blocks
        )
        if earlier_speaker and not later_speaker:
            result.add(
                "warning",
                "cab/IR sits before this tone source; normally it comes after",
                slot=source.slot,
            )

    # CPU
    result.estimated_cpu = estimate_cpu(rig, catalog)
    if result.estimated_cpu >= CPU_ERROR_THRESHOLD:
        result.add(
            "error",
            f"estimated CPU ~{result.estimated_cpu:.0%} exceeds the device budget; "
            "drop a block, use an A2 Lite capture, or a truncated 1024 IR",
        )
    elif result.estimated_cpu >= CPU_WARN_THRESHOLD:
        result.add(
            "warning",
            f"estimated CPU ~{result.estimated_cpu:.0%} is close to the limit; "
            "a full meter can produce pops and clicks. This is an estimate - "
            "check the device's own meter",
        )

    nam_blocks = rig.blocks_by_category(BlockCategory.NAM)
    if len(nam_blocks) > 1:
        result.add(
            "warning",
            f"{len(nam_blocks)} NAM captures in one rig; each costs roughly a "
            "quarter of the CPU meter, so verify on the device",
        )

    if not catalog.is_verified:
        if result.unknown_models:
            names = ", ".join(
                f"{model} (slot {slot})" for slot, model in result.unknown_models
            )
            result.add(
                "info",
                f"{len(result.unknown_models)} model(s) not in the catalog: {names}. "
                "The catalog is unverified - model ids and parameter ranges have "
                "not been extracted from a device - so these were not checked.",
            )
        else:
            result.add(
                "info",
                "catalog is unverified - model ids and parameter ranges have not "
                "been extracted from a device, so those checks were lenient",
            )

    return result

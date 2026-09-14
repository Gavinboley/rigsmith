"""The HeadRush rig model.

Everything here mirrors how the Core actually works, as established from the
user guide and device documentation:

* A **rig** is a preset: 14 block slots plus a signal path and a set of scenes.
* A **block** occupies one slot and holds one model - an amp, cab, IR, clone,
  NAM capture, or effect - with a parameter set specific to that model.
* A **scene** is HeadRush's snapshot. It sets each block to On, Off, or
  No Change, and can additionally recall a block preset. One footswitch can
  carry two scene states.
* **Doubling** ("2X") puts two amps/cabs/IRs in a single slot, either both
  active for width or switchable A/B.

The device is the authority on what is legal; this model exists to catch
mistakes *before* anything is written, and to give an agent a structure it
can reason about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

#: A rig has exactly this many block slots.
MAX_SLOTS = 14

#: The Core has five footswitches.
FOOTSWITCH_COUNT = 5

#: Input and output gain range on the advanced block edit page, in dB.
GAIN_RANGE_DB = (-12.0, 12.0)


class SignalPath(str, Enum):
    """Rig routing. Split paths divide the middle into two parallel lanes.

    The numbers are the documented slot allocation: ``MIDDLE_SPLIT_3_4_3``
    has three slots in, an eight-slot split mix section (four per lane),
    then three slots out.
    """

    STRAIGHT = "straight"
    MIDDLE_SPLIT_3_4_3 = "middle_split_3_4_3"
    MIDDLE_SPLIT_5_3_3 = "middle_split_5_3_3"
    MIDDLE_SPLIT_7_2_3 = "middle_split_7_2_3"

    @property
    def is_split(self) -> bool:
        return self is not SignalPath.STRAIGHT

    @property
    def layout(self) -> tuple[int, int, int]:
        """``(pre_slots, split_slots_per_lane, post_slots)``."""
        return {
            SignalPath.STRAIGHT: (MAX_SLOTS, 0, 0),
            SignalPath.MIDDLE_SPLIT_3_4_3: (3, 4, 3),
            SignalPath.MIDDLE_SPLIT_5_3_3: (5, 3, 3),
            SignalPath.MIDDLE_SPLIT_7_2_3: (7, 2, 3),
        }[self]


class BlockCategory(str, Enum):
    """What kind of model sits in a slot."""

    AMP = "amp"
    CAB = "cab"
    IR = "ir"
    CLONE = "clone"
    """A HeadRush Amp Cloner capture (.ac) - the device's own format."""
    NAM = "nam"
    """A Neural Amp Modeler capture (.nam), A1 or A2."""
    EFFECT = "effect"
    INPUT = "input"
    OUTPUT = "output"
    MIX = "mix"
    """Only present on split signal paths."""

    @property
    def is_tone_source(self) -> bool:
        """True for blocks that define the core voice rather than shape it."""
        return self in {
            BlockCategory.AMP,
            BlockCategory.CLONE,
            BlockCategory.NAM,
        }


class Doubling(str, Enum):
    """The "2X" mode on an amp/cab/IR slot."""

    NONE = "none"
    STEREO = "stereo"
    """Both models active at once, panned for a wider image."""
    SWITCH = "switch"
    """Two models in one slot, switched A/B by footswitch."""


class SceneBlockState(str, Enum):
    """What a scene does to a given block.

    ``NO_CHANGE`` is the important one: it leaves the block exactly as the
    player left it, which is how you build a scene that only touches the
    blocks it cares about.
    """

    ON = "on"
    OFF = "off"
    NO_CHANGE = "no_change"


@dataclass
class EqBand:
    """One band of the 4-band parametric EQ on the advanced block edit page."""

    frequency: float
    gain_db: float = 0.0
    q: float = 1.0
    mode: str | None = None
    """``"shelf"`` or ``"cut"``; only the Low and High bands offer this."""


@dataclass
class Block:
    """One model in one slot."""

    slot: int
    category: BlockCategory
    model_id: str
    """Catalog identifier, e.g. ``"82 LEAD 800 100W"`` or a NAM capture id."""
    model_name: str | None = None
    enabled: bool = True
    params: dict[str, Any] = field(default_factory=dict)
    """Model-specific parameters. Each amp model has its own knob set, so
    this is deliberately open; :mod:`acro.rig.validate` checks it against the
    extracted catalog rather than a hardcoded schema."""

    # Advanced block edit - available on every block regardless of model.
    input_gain_db: float = 0.0
    output_gain_db: float = 0.0
    eq: list[EqBand] = field(default_factory=list)

    # Doubling, only meaningful for amp/cab/IR/clone/NAM slots.
    doubling: Doubling = Doubling.NONE
    doubled_model_id: str | None = None

    led_color: str | None = None
    lane: int | None = None
    """On a split path, which lane (0 or 1) this block sits in."""

    def describe(self) -> str:
        name = self.model_name or self.model_id
        bits = [f"slot {self.slot:2d}", f"{self.category.value:7}", name]
        if not self.enabled:
            bits.append("(bypassed)")
        if self.doubling is not Doubling.NONE:
            bits.append(f"[2X {self.doubling.value}: {self.doubled_model_id}]")
        return "  ".join(bits)


@dataclass
class Scene:
    """A snapshot: per-block on/off/no-change, optionally recalling presets."""

    name: str
    states: dict[int, SceneBlockState] = field(default_factory=dict)
    """Slot index -> state. Slots absent from this map are NO_CHANGE."""
    presets: dict[int, str] = field(default_factory=dict)
    """Slot index -> block preset name to recall when the scene activates."""
    footswitch: int | None = None
    """1-based footswitch this scene is assigned to, if any."""
    led_color: str | None = None

    def state_for(self, slot: int) -> SceneBlockState:
        return self.states.get(slot, SceneBlockState.NO_CHANGE)

    def apply_to(self, blocks: list[Block]) -> list[Block]:
        """Return what ``blocks`` look like with this scene active.

        Used for previewing and for rendering a specific scene during tone
        testing. Does not mutate the input.
        """
        import copy

        result = copy.deepcopy(blocks)
        for block in result:
            state = self.state_for(block.slot)
            if state is SceneBlockState.ON:
                block.enabled = True
            elif state is SceneBlockState.OFF:
                block.enabled = False
        return result


@dataclass
class Rig:
    """A complete preset."""

    name: str
    blocks: list[Block] = field(default_factory=list)
    scenes: list[Scene] = field(default_factory=list)
    signal_path: SignalPath = SignalPath.STRAIGHT
    rig_width: int = 100
    """100 = full stereo, 0 = collapsed to mono."""
    setlist: str | None = None
    slot_number: int | None = None
    """Position in the setlist, used for MIDI Program Change recall."""
    notes: str | None = None
    source_recipe: str | None = None
    """Identifier of the ToneRecipe this rig was compiled from, if any."""

    # -- lookups ----------------------------------------------------------

    def block_at(self, slot: int) -> Block | None:
        return next((b for b in self.blocks if b.slot == slot), None)

    def blocks_by_category(self, category: BlockCategory) -> list[Block]:
        return [b for b in self.blocks if b.category is category]

    def scene_named(self, name: str) -> Scene | None:
        lowered = name.lower()
        return next((s for s in self.scenes if s.name.lower() == lowered), None)

    @property
    def used_slots(self) -> set[int]:
        return {b.slot for b in self.blocks}

    @property
    def free_slots(self) -> list[int]:
        used = self.used_slots
        return [i for i in range(1, MAX_SLOTS + 1) if i not in used]

    @property
    def tone_sources(self) -> list[Block]:
        """Amp, clone, and NAM blocks - what actually defines the voice."""
        return [b for b in self.blocks if b.category.is_tone_source]

    @property
    def is_stereo(self) -> bool:
        """Output is stereo if anything in the rig makes it so.

        Per the documentation that means a split path, any 2X doubling, or a
        stereo effect - and a ``rig_width`` of 0 collapses it back to mono.
        """
        if self.rig_width == 0:
            return False
        if self.signal_path.is_split:
            return True
        return any(b.doubling is not Doubling.NONE for b in self.blocks)

    # -- editing ----------------------------------------------------------

    def add_block(self, block: Block) -> Block:
        """Place a block, auto-assigning a slot if none was given.

        Raises:
            ValueError: if the rig is full or the slot is taken.
        """
        if block.slot is None or block.slot <= 0:
            free = self.free_slots
            if not free:
                raise ValueError(f"rig '{self.name}' is full ({MAX_SLOTS} slots used)")
            block.slot = free[0]
        elif self.block_at(block.slot) is not None:
            raise ValueError(f"slot {block.slot} is already occupied")
        elif not 1 <= block.slot <= MAX_SLOTS:
            raise ValueError(f"slot {block.slot} out of range 1..{MAX_SLOTS}")
        self.blocks.append(block)
        self.blocks.sort(key=lambda b: b.slot)
        return block

    def remove_block(self, slot: int) -> Block:
        block = self.block_at(slot)
        if block is None:
            raise ValueError(f"no block in slot {slot}")
        self.blocks.remove(block)
        for scene in self.scenes:
            scene.states.pop(slot, None)
            scene.presets.pop(slot, None)
        return block

    def move_block(self, from_slot: int, to_slot: int) -> Block:
        """Move a block, shifting the intervening blocks to close the gap.

        Scenes reference blocks by slot number, so re-laying the chain would
        silently repoint every scene at the wrong block. The old-to-new slot
        mapping is therefore applied to all scene state and preset entries as
        part of the move.
        """
        block = self.block_at(from_slot)
        if block is None:
            raise ValueError(f"no block in slot {from_slot}")
        if not 1 <= to_slot <= MAX_SLOTS:
            raise ValueError(f"slot {to_slot} out of range 1..{MAX_SLOTS}")
        if from_slot == to_slot:
            return block

        previous = {id(b): b.slot for b in self.blocks}

        ordered = sorted(self.blocks, key=lambda b: b.slot)
        ordered.remove(block)
        # Re-lay the remaining blocks, inserting at the requested position.
        target = max(1, min(to_slot, MAX_SLOTS))
        rebuilt: list[Block] = []
        slot = 1
        inserted = False
        for existing in ordered:
            if not inserted and slot >= target:
                block.slot = slot
                rebuilt.append(block)
                slot += 1
                inserted = True
            existing.slot = slot
            rebuilt.append(existing)
            slot += 1
        if not inserted:
            block.slot = min(slot, MAX_SLOTS)
            rebuilt.append(block)

        self.blocks = sorted(rebuilt, key=lambda b: b.slot)

        remap = {previous[id(b)]: b.slot for b in self.blocks}
        if any(old != new for old, new in remap.items()):
            for scene in self.scenes:
                scene.states = {remap.get(s, s): v for s, v in scene.states.items()}
                scene.presets = {remap.get(s, s): v for s, v in scene.presets.items()}

        return block

    # -- presentation -----------------------------------------------------

    def describe(self) -> str:
        """A readable signal chain, for agent output and confirmation diffs."""
        lines = [f"Rig: {self.name}"]
        if self.setlist:
            lines.append(f"  setlist: {self.setlist}")
        lines.append(
            f"  path: {self.signal_path.value}  width: {self.rig_width}%  "
            f"{'stereo' if self.is_stereo else 'mono'}"
        )
        lines.append(f"  blocks: {len(self.blocks)}/{MAX_SLOTS}")
        for block in sorted(self.blocks, key=lambda b: b.slot):
            lines.append("    " + block.describe())
        if self.scenes:
            lines.append(f"  scenes: {len(self.scenes)}")
            for scene in self.scenes:
                fs = f"FS{scene.footswitch}" if scene.footswitch else "unassigned"
                on = [s for s, st in scene.states.items() if st is SceneBlockState.ON]
                off = [s for s, st in scene.states.items() if st is SceneBlockState.OFF]
                lines.append(
                    f"    {scene.name:20} {fs:12} on={sorted(on) or '-'} off={sorted(off) or '-'}"
                )
        return "\n".join(lines)

    def signal_chain(self) -> str:
        """One-line chain, e.g. ``Fuzz -> NAM(JMP) -> IR(1960A) -> Delay``."""
        parts = []
        for block in sorted(self.blocks, key=lambda b: b.slot):
            if block.category in {BlockCategory.INPUT, BlockCategory.OUTPUT}:
                continue
            label = block.model_name or block.model_id
            if block.category in {BlockCategory.NAM, BlockCategory.IR, BlockCategory.CLONE}:
                label = f"{block.category.value.upper()}({label})"
            if not block.enabled:
                label = f"[{label}]"
            parts.append(label)
        return " -> ".join(parts) if parts else "(empty)"

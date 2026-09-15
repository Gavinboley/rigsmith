"""Tests for the rig domain model."""

from __future__ import annotations

import pytest

from rigsmith.rig import (
    Block,
    BlockCategory,
    Doubling,
    Rig,
    Scene,
    SceneBlockState,
    SignalPath,
)
from rigsmith.rig.model import MAX_SLOTS


def make_rig(*model_ids: str) -> Rig:
    rig = Rig(name="test")
    for model_id in model_ids:
        rig.add_block(
            Block(slot=0, category=BlockCategory.EFFECT, model_id=model_id)
        )
    return rig


def test_blocks_autoassign_sequential_slots():
    rig = make_rig("a", "b", "c")
    assert [b.slot for b in rig.blocks] == [1, 2, 3]
    assert rig.free_slots[0] == 4


def test_rig_rejects_duplicate_slot():
    rig = make_rig("a")
    with pytest.raises(ValueError, match="already occupied"):
        rig.add_block(Block(slot=1, category=BlockCategory.EFFECT, model_id="b"))


def test_rig_rejects_overfill():
    rig = make_rig(*[f"m{i}" for i in range(MAX_SLOTS)])
    assert len(rig.blocks) == MAX_SLOTS
    with pytest.raises(ValueError, match="is full"):
        rig.add_block(Block(slot=0, category=BlockCategory.EFFECT, model_id="extra"))


def test_remove_block_drops_scene_references():
    rig = make_rig("a", "b")
    rig.scenes.append(
        Scene(name="s", states={1: SceneBlockState.ON, 2: SceneBlockState.OFF})
    )
    rig.remove_block(1)
    assert 1 not in rig.scenes[0].states
    assert rig.scenes[0].states[2] is SceneBlockState.OFF


def test_move_block_remaps_scene_slots():
    """Scenes address blocks by slot, so a move must carry them along.

    Without remapping, moving a block silently repoints every scene at
    whatever block landed in that slot number instead.
    """
    rig = make_rig("muff", "amp", "ir", "delay")
    rig.scenes.append(
        Scene(
            name="verse",
            states={1: SceneBlockState.OFF, 4: SceneBlockState.ON},
            presets={4: "slapback"},
        )
    )

    rig.move_block(4, 1)  # delay to the front

    slots = {b.model_id: b.slot for b in rig.blocks}
    scene = rig.scenes[0]
    assert scene.states[slots["muff"]] is SceneBlockState.OFF
    assert scene.states[slots["delay"]] is SceneBlockState.ON
    assert scene.presets[slots["delay"]] == "slapback"


def test_move_block_is_stable_for_noop():
    rig = make_rig("a", "b", "c")
    rig.move_block(2, 2)
    assert [b.model_id for b in rig.blocks] == ["a", "b", "c"]


def test_move_block_to_end():
    rig = make_rig("a", "b", "c")
    rig.move_block(1, 3)
    assert [b.model_id for b in rig.blocks] == ["b", "c", "a"]


def test_scene_apply_does_not_mutate_source():
    rig = make_rig("a", "b")
    scene = Scene(name="s", states={1: SceneBlockState.OFF})
    applied = scene.apply_to(rig.blocks)
    assert applied[0].enabled is False
    assert rig.blocks[0].enabled is True, "apply_to must not mutate the rig"


def test_scene_no_change_leaves_block_alone():
    rig = make_rig("a", "b")
    rig.blocks[1].enabled = False
    scene = Scene(name="s", states={1: SceneBlockState.ON})
    applied = scene.apply_to(rig.blocks)
    assert applied[0].enabled is True
    assert applied[1].enabled is False, "unlisted blocks default to NO_CHANGE"


def test_stereo_inferred_from_doubling_and_path():
    rig = make_rig("a")
    assert rig.is_stereo is False

    rig.blocks[0].doubling = Doubling.STEREO
    assert rig.is_stereo is True

    rig.rig_width = 0
    assert rig.is_stereo is False, "width 0 collapses to mono"

    rig.rig_width = 100
    rig.blocks[0].doubling = Doubling.NONE
    rig.signal_path = SignalPath.MIDDLE_SPLIT_3_4_3
    assert rig.is_stereo is True


def test_signal_path_layouts_sum_correctly():
    for path in SignalPath:
        pre, split, post = path.layout
        if path is SignalPath.STRAIGHT:
            assert pre == MAX_SLOTS
        else:
            # Two parallel lanes of `split` slots each.
            assert pre + (split * 2) + post == MAX_SLOTS


def test_tone_sources_exclude_effects_and_cabs():
    rig = Rig(name="t")
    rig.add_block(Block(slot=0, category=BlockCategory.EFFECT, model_id="od"))
    rig.add_block(Block(slot=0, category=BlockCategory.NAM, model_id="nam"))
    rig.add_block(Block(slot=0, category=BlockCategory.IR, model_id="ir"))
    assert [b.model_id for b in rig.tone_sources] == ["nam"]


def test_signal_chain_marks_bypassed_blocks():
    rig = make_rig("a", "b")
    rig.blocks[1].enabled = False
    assert rig.signal_chain() == "a -> [b]"

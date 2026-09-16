"""Tests for rig diffing.

The diff is what a user reads before agreeing to write to their hardware, so
a change it fails to mention is a change made without consent. These cover
the fields it used to skip, and the duplicate-block case where it could
attribute one block's edit to another.
"""

from __future__ import annotations

import copy

from rigsmith.rig import Block, BlockCategory, Doubling, Rig, Scene, SceneBlockState
from rigsmith.rig.diff import diff_rigs
from rigsmith.rig.model import EqBand, SignalPath


def rig_with(*models: str) -> Rig:
    rig = Rig(name="t")
    for model in models:
        rig.add_block(Block(slot=0, category=BlockCategory.EFFECT, model_id=model))
    return rig


def descriptions(before: Rig, after: Rig) -> str:
    return " | ".join(c.description for c in diff_rigs(before, after).changes)


# --------------------------------------------------------------------------
# basics
# --------------------------------------------------------------------------


def test_no_changes_is_empty():
    assert diff_rigs(rig_with("a", "b"), rig_with("a", "b")).empty


def test_new_rig_lists_every_block():
    result = diff_rigs(None, rig_with("a", "b"))
    assert not result.empty
    assert "create rig" in result.render()


def test_added_and_removed_blocks():
    before = rig_with("a")
    after = rig_with("a", "b")
    assert "add b" in descriptions(before, after)

    assert "remove b" in descriptions(after, before)


def test_reorder_reads_as_a_move_not_add_and_remove():
    before = rig_with("a", "b", "c")
    after = copy.deepcopy(before)
    after.move_block(3, 1)

    rendered = descriptions(before, after)
    assert "slot" in rendered
    assert "add" not in rendered
    assert "remove" not in rendered


# --------------------------------------------------------------------------
# fields the diff used to skip entirely
# --------------------------------------------------------------------------


def test_eq_changes_are_reported():
    before = rig_with("a")
    before.blocks[0].eq = [EqBand(frequency=100.0, gain_db=0.0, q=1.0)]
    after = copy.deepcopy(before)
    after.blocks[0].eq[0].gain_db = -3.5

    rendered = descriptions(before, after)
    assert "EQ band 1" in rendered
    assert "-3.5" in rendered


def test_added_eq_band_is_reported():
    before = rig_with("a")
    after = copy.deepcopy(before)
    after.blocks[0].eq = [EqBand(frequency=800.0)]
    assert "EQ bands 0 -> 1" in descriptions(before, after)


def test_led_colour_change_is_reported():
    before = rig_with("a")
    after = copy.deepcopy(before)
    after.blocks[0].led_color = "red"
    assert "LED" in descriptions(before, after)


def test_lane_change_is_reported():
    before = rig_with("a")
    before.signal_path = SignalPath.MIDDLE_SPLIT_3_4_3
    after = copy.deepcopy(before)
    after.blocks[0].lane = 1
    assert "lane" in descriptions(before, after)


def test_setlist_change_is_reported():
    before = rig_with("a")
    after = copy.deepcopy(before)
    after.setlist = "Live set"
    assert "setlist" in descriptions(before, after)


def test_signal_path_and_width_changes_are_reported():
    before = rig_with("a")
    after = copy.deepcopy(before)
    after.signal_path = SignalPath.MIDDLE_SPLIT_5_3_3
    after.rig_width = 40

    rendered = descriptions(before, after)
    assert "signal path" in rendered
    assert "rig width" in rendered


def test_doubling_change_is_reported():
    before = rig_with("a")
    after = copy.deepcopy(before)
    after.blocks[0].doubling = Doubling.STEREO
    after.blocks[0].doubled_model_id = "b"
    assert "2X" in descriptions(before, after)


# --------------------------------------------------------------------------
# duplicate blocks
# --------------------------------------------------------------------------


def test_duplicate_models_do_not_cross_attribute_changes():
    """Two instances of the same pedal is an ordinary chain.

    Matching purely by model id paired them arbitrarily, so an edit to one
    could be reported against the other.
    """
    before = rig_with("od", "amp", "od")
    after = copy.deepcopy(before)
    after.block_at(3).params["level"] = 0.8  # the SECOND overdrive

    changes = diff_rigs(before, after).changes
    param_changes = [c for c in changes if c.kind == "param"]
    assert len(param_changes) == 1
    assert "slot 3" in param_changes[0].description


def test_duplicate_models_report_a_removal_against_the_right_slot():
    before = rig_with("od", "amp", "od")
    after = copy.deepcopy(before)
    after.remove_block(1)

    rendered = descriptions(before, after)
    assert "remove" in rendered
    # The surviving overdrive kept slot 3, so it must not be reported as moved.
    assert "od: slot" not in rendered


# --------------------------------------------------------------------------
# scenes
# --------------------------------------------------------------------------


def test_scene_added_and_removed():
    before = rig_with("a", "b")
    after = copy.deepcopy(before)
    after.scenes.append(
        Scene(name="Lead", states={1: SceneBlockState.ON}, footswitch=2)
    )
    assert "scene added: 'Lead'" in descriptions(before, after)
    assert "scene removed: 'Lead'" in descriptions(after, before)


def test_scene_state_change_is_reported():
    before = rig_with("a", "b")
    before.scenes.append(Scene(name="V", states={1: SceneBlockState.ON}))
    after = copy.deepcopy(before)
    after.scenes[0].states[2] = SceneBlockState.OFF
    assert "states" in descriptions(before, after)


def test_footswitch_reassignment_is_reported():
    before = rig_with("a")
    before.scenes.append(Scene(name="V", states={1: SceneBlockState.ON}, footswitch=1))
    after = copy.deepcopy(before)
    after.scenes[0].footswitch = 3
    assert "footswitch 1 -> 3" in descriptions(before, after)


def test_summary_counts_by_kind():
    before = rig_with("a")
    after = rig_with("a", "b")
    assert "add" in diff_rigs(before, after).summary()

"""Regression tests for bugs found in the phase-2 review.

Each of these existed because no test covered the case. They are kept
together so the specific failure each one guards against stays legible.
"""

from __future__ import annotations

import numpy as np
import pytest

from rigsmith.rig import Block, BlockCategory, Rig, Scene, SceneBlockState


def chain(*pairs: tuple[int, str]) -> Rig:
    """Build a rig from explicit (slot, model_id) pairs, gaps included."""
    rig = Rig(name="t")
    for slot, model_id in pairs:
        rig.add_block(
            Block(slot=slot, category=BlockCategory.EFFECT, model_id=model_id)
        )
    return rig


def layout(rig: Rig) -> list[tuple[int, str]]:
    return [(b.slot, b.model_id) for b in sorted(rig.blocks, key=lambda b: b.slot)]


# --------------------------------------------------------------------------
# move_block: honoured the wrong destination and renumbered bystanders
# --------------------------------------------------------------------------


def test_move_block_lands_in_the_slot_requested():
    """It used to treat to_slot as a position in the compacted order.

    On a gapped rig, move_block(1, 4) put the block in slot 3.
    """
    rig = chain((1, "a"), (2, "b"), (5, "e"))
    rig.move_block(1, 4)
    assert rig.block_at(4).model_id == "a"


def test_move_block_leaves_uninvolved_blocks_alone():
    """Blocks nowhere near the move used to be silently renumbered.

    move_block(1, 4) renumbered 'e' from slot 5 to slot 2, despite 'e' having
    nothing to do with the move.
    """
    rig = chain((1, "a"), (2, "b"), (5, "e"))
    rig.move_block(1, 4)
    assert layout(rig) == [(2, "b"), (4, "a"), (5, "e")]


def test_move_block_preserves_gaps():
    """Empty slots are deliberate spaces, not untidiness to compact away."""
    rig = chain((1, "a"), (7, "g"))
    rig.move_block(1, 3)
    assert layout(rig) == [(3, "a"), (7, "g")]


def test_move_block_onto_an_occupied_slot_cascades_to_the_nearest_gap_only():
    rig = chain((1, "a"), (2, "b"), (5, "e"))
    rig.move_block(1, 5)
    # 'e' slides into the gap at 4; 'b' is beyond that gap and stays put.
    assert layout(rig) == [(2, "b"), (4, "e"), (5, "a")]


def test_move_block_still_reorders_a_contiguous_chain():
    """The common case must keep working."""
    rig = chain((1, "a"), (2, "b"), (3, "c"), (4, "d"))
    rig.move_block(4, 1)
    assert [m for _, m in layout(rig)] == ["d", "a", "b", "c"]

    rig = chain((1, "a"), (2, "b"), (3, "c"), (4, "d"))
    rig.move_block(1, 4)
    assert [m for _, m in layout(rig)] == ["b", "c", "d", "a"]


def test_move_block_keeps_scenes_pointing_at_the_same_blocks_across_a_gap_move():
    rig = chain((1, "a"), (2, "b"), (5, "e"))
    rig.scenes.append(
        Scene(
            name="v",
            states={1: SceneBlockState.OFF, 5: SceneBlockState.ON},
            presets={5: "slap"},
        )
    )

    rig.move_block(1, 4)

    slots = {b.model_id: b.slot for b in rig.blocks}
    scene = rig.scenes[0]
    assert scene.states[slots["a"]] is SceneBlockState.OFF
    assert scene.states[slots["e"]] is SceneBlockState.ON
    assert scene.presets[slots["e"]] == "slap"


def test_move_block_rejects_an_out_of_range_destination():
    rig = chain((1, "a"))
    with pytest.raises(ValueError, match="out of range"):
        rig.move_block(1, 99)


# --------------------------------------------------------------------------
# reamp: an unmeasured latency was indistinguishable from a measured zero
# --------------------------------------------------------------------------


def test_capture_defaults_to_latency_measured():
    from rigsmith.match.render_device import Capture

    capture = Capture(wet=np.zeros(10), dry=None, sample_rate=48000)
    assert capture.latency_measured
    assert capture.safe_for_aligned_scoring


def test_unmeasured_latency_blocks_aligned_scoring_and_says_so():
    """A silent zero would let aligned ESR report contaminated numbers.

    Sample-wise metrics swing by orders of magnitude under a few milliseconds
    of offset, so an unmeasured capture must be flagged, not quietly treated
    as perfectly aligned.
    """
    from rigsmith.match.render_device import Capture

    capture = Capture(
        wet=np.zeros(48000),
        dry=None,
        sample_rate=48000,
        latency_samples=0,
        latency_measured=False,
    )
    assert not capture.safe_for_aligned_scoring

    described = capture.describe()
    assert "LATENCY NOT MEASURED" in described
    assert "aligned" in described.lower()


def test_measured_latency_is_reported_as_compensated():
    from rigsmith.match.render_device import Capture

    capture = Capture(
        wet=np.zeros(48000),
        dry=None,
        sample_rate=48000,
        latency_samples=480,
        latency_measured=True,
    )
    described = capture.describe()
    assert "compensated" in described
    assert "10.0 ms" in described

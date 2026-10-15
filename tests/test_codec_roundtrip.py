"""Nothing about a rig may be lost on the way to JSON and back.

A dropped field is worse than a missing feature: the rig you exported is not
the rig you reload, and the diff you approve before a write is computed
against whatever survived. So this enumerates the model's own fields rather
than listing the ones somebody remembered.
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from rigsmith.rig import Block, BlockCategory, Doubling, Rig, Scene, SceneBlockState
from rigsmith.rig.codec import rig_from_dict, rig_to_dict
from rigsmith.rig.model import EqBand, SignalPath

#: Fields the codec deliberately does not carry, and why.
NOT_SERIALISED: dict[str, dict[str, str]] = {
    "Block": {},
    "Scene": {},
    "Rig": {},
}


def fully_populated() -> Rig:
    """A rig with every field set to something other than its default."""
    rig = Rig(
        name="Cherub Rock",
        signal_path=SignalPath.MIDDLE_SPLIT_3_4_3,
        rig_width=60,
        tempo=128.5,
        setlist="Siamese Dream",
        slot_number=7,
        notes="the fuzz is doing the work",
        source_recipe="cherub-rock",
    )
    rig.add_block(
        Block(
            slot=1,
            category=BlockCategory.EFFECT,
            model_id="big-muff",
            model_name="Big Muff Pi",
            enabled=False,
            params={"drive": 0.72, "tone": 0.4},
            input_gain_db=-2.5,
            output_gain_db=1.5,
            eq=[EqBand(frequency=120.0, gain_db=-3.0, q=0.7, mode="shelf")],
            doubling=Doubling.STEREO,
            doubled_model_id="big-muff-b",
            led_color="red",
            lane=2,
        )
    )
    rig.scenes.append(
        Scene(
            name="Lead",
            states={1: SceneBlockState.ON},
            presets={1: "hot"},
            footswitch=3,
            led_color="blue",
        )
    )
    return rig


def test_every_field_survives_a_round_trip():
    original = fully_populated()
    restored = rig_from_dict(rig_to_dict(original))

    lost: list[str] = []
    for owner, (before, after) in {
        "Rig": (original, restored),
        "Block": (original.blocks[0], restored.blocks[0]),
        "Scene": (original.scenes[0], restored.scenes[0]),
    }.items():
        for descriptor in fields(type(before)):
            if descriptor.name in NOT_SERIALISED[owner]:
                continue
            if descriptor.name in ("blocks", "scenes"):
                continue  # compared through their own entries
            if getattr(before, descriptor.name) != getattr(after, descriptor.name):
                lost.append(
                    f"{owner}.{descriptor.name}: "
                    f"{getattr(before, descriptor.name)!r} -> "
                    f"{getattr(after, descriptor.name)!r}"
                )

    assert not lost, "the codec dropped or altered these: " + "; ".join(lost)


def test_the_populated_fixture_really_is_populated():
    """Otherwise the round-trip test passes by comparing defaults to defaults."""
    rig = fully_populated()
    defaults = Rig(name="Cherub Rock")
    same_as_default = [
        descriptor.name
        for descriptor in fields(Rig)
        if descriptor.name not in ("name", "blocks", "scenes")
        and getattr(rig, descriptor.name) == getattr(defaults, descriptor.name)
    ]
    assert not same_as_default, (
        "these Rig fields are still at their defaults, so the round-trip test "
        f"is not exercising them: {same_as_default}"
    )

    block_defaults = Block(slot=1, category=BlockCategory.EFFECT, model_id="big-muff")
    untested = [
        descriptor.name
        for descriptor in fields(Block)
        if descriptor.name not in ("slot", "category", "model_id")
        and getattr(rig.blocks[0], descriptor.name)
        == getattr(block_defaults, descriptor.name)
    ]
    assert not untested, f"these Block fields are still at their defaults: {untested}"


def test_an_eq_band_survives_intact():
    """EQ is a nested list of dataclasses - the easiest thing to flatten away."""
    restored = rig_from_dict(rig_to_dict(fully_populated()))
    band = restored.blocks[0].eq[0]
    assert (band.frequency, band.gain_db, band.q, band.mode) == (120.0, -3.0, 0.7, "shelf")


def test_scene_state_keys_come_back_as_integers():
    """JSON object keys are strings; slots are numbers everywhere else."""
    restored = rig_from_dict(rig_to_dict(fully_populated()))
    assert set(restored.scenes[0].states) == {1}
    assert all(isinstance(slot, int) for slot in restored.scenes[0].states)
    assert all(isinstance(slot, int) for slot in restored.scenes[0].presets)


def test_a_rig_from_a_newer_format_is_refused_rather_than_guessed_at():
    payload = rig_to_dict(fully_populated())
    payload["format_version"] = 999

    with pytest.raises(Exception) as caught:
        rig_from_dict(payload)
    assert "999" in str(caught.value) or "version" in str(caught.value).lower()

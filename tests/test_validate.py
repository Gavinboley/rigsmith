"""Tests for rig validation and the CPU estimate."""

from __future__ import annotations

from rigsmith.rig import Block, BlockCategory, Doubling, Rig, Scene, SceneBlockState
from rigsmith.rig.catalog import ModelSpec, ParamSpec, seed_catalog
from rigsmith.rig.validate import estimate_cpu, validate_rig


def catalog():
    return seed_catalog()


def base_rig() -> Rig:
    rig = Rig(name="ok")
    rig.add_block(
        Block(slot=0, category=BlockCategory.NAM, model_id="generic:nam",
              params={"size": "A2 Lite"})
    )
    rig.add_block(Block(slot=0, category=BlockCategory.IR, model_id="generic:ir"))
    return rig


def messages(result) -> str:
    return "\n".join(f.message for f in result.findings)


def test_valid_rig_has_no_errors():
    assert validate_rig(base_rig(), catalog()).ok


def test_unnamed_rig_is_an_error():
    rig = base_rig()
    rig.name = "  "
    assert not validate_rig(rig, catalog()).ok


def test_gain_outside_range_is_an_error():
    rig = base_rig()
    rig.blocks[0].input_gain_db = 13.0
    result = validate_rig(rig, catalog())
    assert not result.ok
    assert "input gain" in messages(result)


def test_doubling_rejected_on_effect_block():
    rig = base_rig()
    rig.add_block(
        Block(slot=0, category=BlockCategory.EFFECT, model_id="generic:delay",
              doubling=Doubling.STEREO, doubled_model_id="x")
    )
    result = validate_rig(rig, catalog())
    assert "2X doubling is only available" in messages(result)


def test_doubling_requires_second_model():
    rig = base_rig()
    rig.blocks[0].doubling = Doubling.SWITCH
    result = validate_rig(rig, catalog())
    assert "no second model given" in messages(result)


def test_scene_pointing_at_empty_slot_is_an_error():
    rig = base_rig()
    rig.scenes.append(Scene(name="s", states={11: SceneBlockState.ON}))
    result = validate_rig(rig, catalog())
    assert not result.ok
    assert "holds no block" in messages(result)


def test_footswitch_out_of_range_is_an_error():
    rig = base_rig()
    rig.scenes.append(Scene(name="s", states={1: SceneBlockState.ON}, footswitch=6))
    result = validate_rig(rig, catalog())
    assert "outside 1..5" in messages(result)


def test_footswitch_allows_two_scenes_but_not_three():
    rig = base_rig()
    rig.scenes.append(Scene(name="a", states={1: SceneBlockState.ON}, footswitch=1))
    rig.scenes.append(Scene(name="b", states={1: SceneBlockState.OFF}, footswitch=1))
    assert validate_rig(rig, catalog()).ok

    rig.scenes.append(Scene(name="c", states={2: SceneBlockState.ON}, footswitch=1))
    result = validate_rig(rig, catalog())
    assert not result.ok
    assert "toggles between at most two" in messages(result)


def test_duplicate_scene_names_rejected():
    rig = base_rig()
    rig.scenes.append(Scene(name="Lead", states={1: SceneBlockState.ON}))
    rig.scenes.append(Scene(name="lead", states={2: SceneBlockState.ON}))
    result = validate_rig(rig, catalog())
    assert "duplicate scene name" in messages(result)


def test_missing_speaker_warns_but_does_not_block():
    rig = Rig(name="amp only")
    rig.add_block(Block(slot=0, category=BlockCategory.NAM, model_id="generic:nam"))
    result = validate_rig(rig, catalog())
    assert result.ok, "a missing cab is a convention, not a hardware error"
    assert "harsh" in messages(result)


def test_cab_before_amp_warns():
    rig = Rig(name="reversed")
    rig.add_block(Block(slot=0, category=BlockCategory.IR, model_id="generic:ir"))
    rig.add_block(Block(slot=0, category=BlockCategory.NAM, model_id="generic:nam"))
    result = validate_rig(rig, catalog())
    assert "normally it comes after" in messages(result)


def test_cpu_estimate_flags_overload():
    rig = Rig(name="heavy")
    for _ in range(5):
        rig.add_block(
            Block(slot=0, category=BlockCategory.NAM, model_id="generic:nam")
        )
    result = validate_rig(rig, catalog())
    assert not result.ok
    assert "exceeds the device budget" in messages(result)


def test_a2_lite_costs_less_than_full():
    full = Rig(name="full")
    full.add_block(Block(slot=0, category=BlockCategory.NAM, model_id="generic:nam"))

    lite = Rig(name="lite")
    lite.add_block(
        Block(slot=0, category=BlockCategory.NAM, model_id="generic:nam",
              params={"size": "A2 Lite"})
    )
    assert estimate_cpu(lite, catalog()) < estimate_cpu(full, catalog())


def test_truncated_ir_costs_less():
    standard = Rig(name="std")
    standard.add_block(Block(slot=0, category=BlockCategory.IR, model_id="generic:ir"))

    truncated = Rig(name="trunc")
    truncated.add_block(
        Block(slot=0, category=BlockCategory.IR, model_id="generic:ir",
              params={"length": "1024"})
    )
    assert estimate_cpu(truncated, catalog()) < estimate_cpu(standard, catalog())


def test_bypassed_block_does_not_cost_cpu():
    rig = base_rig()
    hot = estimate_cpu(rig, catalog())
    rig.blocks[0].enabled = False
    assert estimate_cpu(rig, catalog()) < hot


def test_stereo_doubling_doubles_cost():
    single = base_rig()
    doubled = base_rig()
    doubled.blocks[0].doubling = Doubling.STEREO
    doubled.blocks[0].doubled_model_id = "generic:nam"
    assert estimate_cpu(doubled, catalog()) > estimate_cpu(single, catalog())


def test_verified_catalog_rejects_unknown_model():
    cat = seed_catalog()
    for model in cat.models.values():
        model.verified = True
    cat.models["real:amp"] = ModelSpec(
        id="real:amp",
        name="Real Amp",
        category=BlockCategory.AMP,
        params=[ParamSpec("gain", "float", 0.0, 1.0, 0.5)],
        verified=True,
    )

    rig = Rig(name="t")
    rig.add_block(Block(slot=0, category=BlockCategory.AMP, model_id="nope"))
    result = validate_rig(rig, cat)
    assert not result.ok
    assert "not in the catalog" in messages(result)


def test_param_range_checked_against_verified_catalog():
    cat = seed_catalog()
    cat.models["real:amp"] = ModelSpec(
        id="real:amp",
        name="Real Amp",
        category=BlockCategory.AMP,
        params=[ParamSpec("gain", "float", 0.0, 1.0, 0.5)],
        verified=True,
    )

    rig = Rig(name="t")
    rig.add_block(
        Block(slot=0, category=BlockCategory.AMP, model_id="real:amp",
              params={"gain": 4.0})
    )
    result = validate_rig(rig, cat)
    assert not result.ok
    assert "above maximum" in messages(result)


def test_unverified_catalog_is_lenient_about_unknown_models():
    rig = Rig(name="t")
    rig.add_block(Block(slot=0, category=BlockCategory.AMP, model_id="unknown-amp"))
    result = validate_rig(rig, seed_catalog())
    assert result.ok, "seed catalog must not block real models it has never seen"


# --------------------------------------------------------------------------
# split paths
# --------------------------------------------------------------------------


def test_lane_outside_the_split_section_is_rejected():
    """SignalPath.layout was documented and tested but enforced nowhere.

    On a 3-4-3 path the split covers slots 4-11; outside it the signal is
    common to both lanes, so a lane there is a misunderstanding of the
    routing rather than a harmless extra field.
    """
    from rigsmith.rig.model import SignalPath

    rig = Rig(name="split", signal_path=SignalPath.MIDDLE_SPLIT_3_4_3)
    rig.add_block(
        Block(slot=1, category=BlockCategory.EFFECT, model_id="generic:overdrive", lane=0)
    )
    result = validate_rig(rig, catalog())
    assert not result.ok
    assert "outside the split section" in messages(result)


def test_lane_inside_the_split_section_is_accepted():
    from rigsmith.rig.model import SignalPath

    rig = Rig(name="split", signal_path=SignalPath.MIDDLE_SPLIT_3_4_3)
    rig.add_block(
        Block(slot=5, category=BlockCategory.EFFECT, model_id="generic:overdrive", lane=0)
    )
    rig.add_block(
        Block(slot=9, category=BlockCategory.EFFECT, model_id="generic:delay", lane=1)
    )
    assert validate_rig(rig, catalog()).ok


def test_split_section_bounds_follow_the_declared_layout():
    """7-2-3 has a much narrower split than 3-4-3."""
    from rigsmith.rig.model import SignalPath

    rig = Rig(name="narrow", signal_path=SignalPath.MIDDLE_SPLIT_7_2_3)
    # Slot 5 is inside 3-4-3's split but outside 7-2-3's (slots 8-11).
    rig.add_block(
        Block(slot=5, category=BlockCategory.EFFECT, model_id="generic:delay", lane=0)
    )
    assert "outside the split section" in messages(validate_rig(rig, catalog()))


def test_int_parameter_rejects_a_fractional_value():
    cat = seed_catalog()
    cat.models["stepped"] = ModelSpec(
        id="stepped",
        name="Stepped",
        category=BlockCategory.EFFECT,
        params=[ParamSpec("taps", "int", 1, 8, 4)],
        verified=True,
    )
    rig = Rig(name="t")
    rig.add_block(
        Block(slot=0, category=BlockCategory.EFFECT, model_id="stepped",
              params={"taps": 3.5})
    )
    assert "whole number" in messages(validate_rig(rig, cat))

"""The tone sheet reads like the device's screen, not like the wire."""

from __future__ import annotations

from rigsmith.rig import sheet
from rigsmith.rig.catalog import Catalog, ModelSpec, ParamSpec
from rigsmith.rig.model import Block, BlockCategory, Rig, Scene, SceneBlockState
from rigsmith.tune.candidates import DELAY_DIVISIONS


def percent(name: str, taper: str = "linear") -> ParamSpec:
    return ParamSpec(name=name, kind="float", minimum=0.0, maximum=1.0, display_min=0.0, display_max=100.0, unit="%", taper=taper)


def catalog() -> Catalog:
    cat = Catalog(source="test")
    cat.models["92 Treadplate Modern"] = ModelSpec(
        id="92 Treadplate Modern", name="92 Treadplate Modern", category=BlockCategory.AMP, selector="Amp.Type",
        params=[percent("Bass"), percent("GainA"), percent("Master", "unknown"), ParamSpec(name="Type", kind="int", minimum=0, maximum=52)],
    )
    cat.models["M104 Analog Delay"] = ModelSpec(
        id="M104 Analog Delay", name="M104 Analog Delay", category=BlockCategory.EFFECT,
        params=[
            ParamSpec(name="DelayTime", kind="float", minimum=0.0, maximum=1.0, display_min=20.0, display_max=800.0, unit="ms"),
            ParamSpec(name="DelayTime_Synced", kind="int", minimum=0, maximum=16, choices=list(DELAY_DIVISIONS)),
            ParamSpec(name="Sync", kind="bool"), percent("Mix"),
        ],
    )
    cat.models["G EQ"] = ModelSpec(
        id="G EQ", name="G EQ", category=BlockCategory.EFFECT,
        params=[ParamSpec(name="Gain1_6kHz", kind="float", minimum=0.0, maximum=1.0, display_min=-15.0, display_max=15.0, unit="dB")],
    )
    return cat


def song_rig() -> Rig:
    rig = Rig(name="Going Under - verse", tempo=82.0)
    rig.blocks = [
        Block(slot=1, category=BlockCategory.AMP, model_id="92 Treadplate Modern", params={"Master": 0.4, "Bass": 0.5, "GainA": 0.62, "Type": 36}),
        Block(slot=2, category=BlockCategory.EFFECT, model_id="M104 Analog Delay", params={"DelayTime": 0.5, "Sync": True, "DelayTime_Synced": 9, "Mix": 0.3}),
        Block(slot=3, category=BlockCategory.EFFECT, model_id="G EQ", params={"Gain1_6kHz": 0.6}),
    ]
    rig.scenes = [Scene(name="verse", states={1: SceneBlockState.ON, 2: SceneBlockState.OFF}, footswitch=1)]
    return rig


def test_a_sheet_reads_like_the_screen_in_front_panel_order():
    text = sheet.build(song_rig(), catalog()).markdown()
    assert "Gain: 62%" in text and "Bass: 50%" in text
    assert text.index("Gain:") < text.index("Bass:") < text.index("Master:"), "front-panel order, not alphabetical"
    assert "Master: ~40%" in text, "an unmeasured taper is read as linear, and marked"
    assert "~ marks a reading" in text
    assert "Type" not in text, "the model picker is the block's name, not a control"


def test_the_measured_entry_wins_and_an_unknown_control_is_a_knob_position():
    cat = catalog()
    # The model's own entry has not measured Bass; the module's has.
    cat.models["Amp"] = ModelSpec(id="Amp", name="Amp", category=BlockCategory.AMP, params=[percent("Bass"), percent("Master")])
    cat.models["92 Treadplate Modern"].params = [percent("Bass", "unknown")]
    text = sheet.build(song_rig(), cat).markdown()
    assert "Bass: 50%" in text and "Master: 40%" in text
    assert "Gain: ~62% of travel" in text, "no range anywhere: still a position, and marked"


def test_a_synced_delay_shows_its_division_not_its_fallback_time():
    text = sheet.build(song_rig(), catalog()).markdown()
    assert "Time: 3/16, synced to the rig's tempo" in text
    assert "410 ms" not in text and "Sync" not in text
    assert "82 BPM, fixed" in text


def test_graphic_bands_and_scenes_are_named_the_way_the_device_names_them():
    assert sheet.label("Gain60Hz") == "60 Hz" and sheet.label("Gain1_6kHz") == "1.6 kHz"
    built = sheet.build(song_rig(), catalog())
    text = built.markdown()
    assert "1.6 kHz: ~+3.0 dB" in text
    assert "FS1 'verse': on 92 Treadplate Modern; off M104 Analog Delay" in text
    assert "set on the device by hand" in text


def test_the_page_is_self_contained():
    page = sheet.build(song_rig(), catalog()).html()
    assert page.startswith("<!doctype html>") and "3/16" in page
    assert "<script" not in page and "http" not in page.split("</style>")[0], "no external resources"
    assert "prefers-color-scheme: dark" in page and "@media print" in page

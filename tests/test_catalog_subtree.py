"""Reading the catalog out of what the device says about itself.

The bundle and the firmware both have to be guessed at: minified JavaScript,
balanced-brace scanning, hoping a dict that has an id and a name and something
category-shaped is a model. A subtree dump needs none of that. The device
publishes one node per block type with a full parameter schema attached, and
the two model pickers carry their own option lists.

The fixture here is synthetic and deliberately small, but its *shape* is
copied exactly from a real dump off a paired Core - including the parts that
previously caused trouble: a boolean that carries two labels, an integer that
carries fifty-three, and a node called ``Chain`` that is not a block at all.
"""

from __future__ import annotations

import json

import pytest

from rigsmith.protocol.catalog_extract import (
    extract,
    from_device_capture,
    from_subtree,
)
from rigsmith.rig.model import BlockCategory

# --------------------------------------------------------------------------
# a subtree the shape of the real one
# --------------------------------------------------------------------------

AMPS = ["59 Tweed Lux", "82 Lead 800 100W", "82 Lead 800 Bass Mod", "92 Treadplate Modern"]
CABS = ["1x12 Tweed Lux", "4x12 Classic 30W", "4x12 65W"]
MICS = ["Dyn 57", "Cond 87", "Ribbon 121"]

#: Index is the number the chain uses. `Amp 2` is the second instance of the
#: same block, which is why the extractor has to skip the `_2` nodes.
MODULE_TYPES = [
    "Empty Slot",
    "Amp",
    "Amp 2",
    "Cab",
    "Cab 2",
    "IR",
    "IR 2",
    "Neural Amp Modeler",
    "Neural Amp Modeler 2",
    "Amp Clone",
    "Amp Clone 2",
    "Green JRC-OD",
    "Green JRC-OD 2",
]


def percent(default=0.5):
    return {
        "maximum": 100.0,
        "minimum": 0.0,
        "type": "number",
        "x-options": {"default": default, "format": "%.0f %%", "grid": 1.0},
    }


def switch(*labels, default=False):
    return {"type": "boolean", "x-options": {"default": default, "strings": list(labels)}}


def picker(options):
    return {
        "maximum": float(len(options) - 1),
        "minimum": 0.0,
        "type": "integer",
        "x-options": {"strings": list(options)},
    }


def node(properties, methods=None, value=None):
    meta = {"properties": properties, "type": "object"}
    if methods:
        meta["x-methods"] = methods
    return {"meta": meta, "value": value if value is not None else {}}


@pytest.fixture()
def subtree() -> dict:
    amp_props = {
        "Type": picker(AMPS),
        "Type2": picker(AMPS),
        "GainA": percent(),
        "Bass": percent(),
        "Master": percent(),
        "Doubling": switch("Single", "Double"),
        "DoubleMode": switch("Stereo", "Switch"),
        "On": switch("Off", "On", default=True),
        # UI bookkeeping the extractor has to drop. `order` is the trap: it
        # lists parameter names, and reads like a slot index.
        "order": {"type": "array"},
        "labels": {"type": "array"},
        "disabled": {"type": "array"},
    }
    cab_props = {
        "CabType": picker(CABS),
        "CabType2": picker(CABS),
        "MicType": picker(MICS),
        "Balance": percent(),
        "Doubling": switch("Single", "Double"),
    }
    return {
        "/Evil/API/Blocks": node({}, value={"ModuleTypes": MODULE_TYPES}),
        "/Evil/Engine/Patch/Amp": node(amp_props),
        "/Evil/Engine/Patch/Amp_2": node(amp_props),
        "/Evil/Engine/Patch/Cab": node(cab_props),
        "/Evil/Engine/Patch/Cab_2": node(cab_props),
        "/Evil/Engine/Patch/IR": node({"Truncate": switch("2048", "1024")}),
        "/Evil/Engine/Patch/Neural_Amp_Modeler": node(
            {"LoadedClone": {"type": "string", "x-options": {"startPath": "NAM"}}}
        ),
        "/Evil/Engine/Patch/Amp_Clone": node({"Gain": percent(), "Chug": percent(0.0)}),
        "/Evil/Engine/Patch/Green_JRC-OD": node({"Drive": percent(), "Tone": percent()}),
        "/Evil/Engine/Patch/Input": node({"Gain": percent()}),
        # Not a block: its ModuleType1..14 *are* the slots.
        "/Evil/Engine/Patch/Chain": node(
            {f"ModuleType{n}": {"maximum": 12.0, "minimum": 0.0, "type": "integer"} for n in range(1, 15)},
            methods={"setModuleTypeInternal": {}},
        ),
        "/Evil/Engine/Patch/Rig": node({"Tempo": percent()}),
    }


@pytest.fixture()
def categories() -> dict:
    # The device names the same block differently here than in the tree: the
    # node is `Amp`, this calls it `Amp (HeadRush)`.
    return {
        "Amp": ["Amp (HeadRush)"],
        "Cab": ["Cab (HeadRush)", "IR"],
        "Clone": ["Amp Clone", "Neural Amp Modeler"],
        "Overdrive": ["Green JRC-OD"],
    }


@pytest.fixture()
def catalog(subtree, categories):
    return from_subtree(subtree, categories=categories)


# --------------------------------------------------------------------------
# what it finds
# --------------------------------------------------------------------------


def test_every_entry_is_verified(catalog):
    """The whole point: this is the device talking, not a guess about it."""
    assert catalog.is_verified
    assert all(model.verified for model in catalog.models.values())


def test_the_pickers_become_models(catalog):
    for name in AMPS:
        assert catalog.models[name].category is BlockCategory.AMP
    for name in CABS:
        assert catalog.models[name].category is BlockCategory.CAB


def test_a_picker_model_records_how_to_select_it(catalog):
    """You cannot put an amp model in a slot - it is a value on a module."""
    treadplate = catalog.models["92 Treadplate Modern"]
    assert treadplate.selector == "Amp.Type"
    assert treadplate.device_index == AMPS.index("92 Treadplate Modern")


def test_a_module_records_the_number_the_chain_uses(catalog):
    """A rig's chain is fourteen integers; the name alone cannot write one."""
    assert catalog.models["Amp"].device_index == MODULE_TYPES.index("Amp")
    assert catalog.models["Green JRC-OD"].device_index == MODULE_TYPES.index("Green JRC-OD")
    assert catalog.models["Amp"].selector is None


def test_the_second_instance_is_not_catalogued_twice(catalog):
    """`Amp_2` duplicates `Amp` exactly; listing it would double everything."""
    assert "Amp 2" not in catalog.models
    assert "Cab 2" not in catalog.models


def test_the_chain_is_not_a_block(catalog):
    """It is where a rig is written to, not something you can put in a slot."""
    assert "Chain" not in catalog.models
    assert "Rig" not in catalog.models


def test_categories_survive_the_two_spellings(catalog):
    """The node is `Amp`; the category listing calls it `Amp (HeadRush)`."""
    assert catalog.models["Amp"].category is BlockCategory.AMP
    assert catalog.models["Cab"].category is BlockCategory.CAB


def test_a_capture_player_is_not_filed_as_a_clone_or_a_cab(catalog):
    """The device files both under Clone and Cab; they load different files."""
    assert catalog.models["Neural Amp Modeler"].category is BlockCategory.NAM
    assert catalog.models["Amp Clone"].category is BlockCategory.CLONE
    assert catalog.models["IR"].category is BlockCategory.IR


def test_structural_nodes_keep_their_own_category(catalog):
    assert catalog.models["Input"].category is BlockCategory.INPUT


# --------------------------------------------------------------------------
# parameters
# --------------------------------------------------------------------------


def test_ui_bookkeeping_is_not_a_parameter(catalog):
    names = {p.name for p in catalog.models["Amp"].params}
    assert not names & {"order", "labels", "disabled"}
    assert "GainA" in names


def test_a_switch_with_labels_is_still_a_switch(catalog):
    """`Doubling` is a boolean that happens to be drawn as Single/Double."""
    doubling = catalog.models["Amp"].param("Doubling")
    assert doubling.kind == "bool"
    assert doubling.choices == ["Single", "Double"]
    assert doubling.validate(True) is None
    assert doubling.validate("Double") is not None


def test_a_picker_is_an_integer_not_an_enum(catalog):
    """It carries 53 names but the value on the wire is an index."""
    kind = catalog.models["Amp"].param("Type")
    assert kind.kind == "int"
    assert kind.validate(2) is None
    assert kind.validate(len(AMPS) + 5) is not None


def test_the_wire_range_is_recorded_not_the_one_on_the_knob(catalog):
    """The bug this exists to prevent.

    The device publishes ``0..100`` beside a tone control and then expects
    ``0.65``. Recording the published range as the valid one would let
    ``Bass = 65`` sail through validation and peg the control on the device,
    with nothing anywhere saying so.
    """
    gain = catalog.models["Amp"].param("GainA")
    assert (gain.minimum, gain.maximum) == (0.0, 1.0)
    assert (gain.display_min, gain.display_max) == (0.0, 100.0)
    assert gain.unit == "%"
    assert gain.normalised

    assert gain.validate(0.65) is None
    assert gain.validate(65) is not None, "a knob reading is not a wire value"


def test_a_taper_is_unknown_until_it_has_been_measured(catalog):
    """It is not in the metadata and it is not implied by the unit."""
    gain = catalog.models["Amp"].param("GainA")
    assert gain.taper == "unknown"
    with pytest.raises(ValueError, match="has not been measured"):
        gain.to_display(0.65)


def test_integers_and_switches_are_not_normalised(catalog):
    """Only the continuous controls are 0-1; a picker index is itself."""
    kind = catalog.models["Amp"].param("Type")
    assert (kind.minimum, kind.maximum) == (0.0, float(len(AMPS) - 1))
    assert not kind.normalised


def test_a_picker_model_carries_the_controls_but_not_the_picker(catalog):
    """A specific amp has knobs; "which amp" is not one of them."""
    params = {p.name for p in catalog.models["82 Lead 800 100W"].params}
    assert {"GainA", "Bass", "Master", "Doubling"} <= params
    assert "Type" not in params and "Type2" not in params


# --------------------------------------------------------------------------
# loading it off disk
# --------------------------------------------------------------------------


def test_it_reads_a_capture_directory(tmp_path, subtree, categories):
    directory = tmp_path / "live"
    directory.mkdir()
    (directory / "subtree.json").write_text(json.dumps(subtree), encoding="utf-8")
    (directory / "device-catalog.json").write_text(json.dumps(categories), encoding="utf-8")

    catalog = from_device_capture(directory)
    assert catalog.models["92 Treadplate Modern"].category is BlockCategory.AMP


def test_it_reads_utf8_regardless_of_the_platform_default(tmp_path, subtree, categories):
    """A real dump has model names Windows' default codec cannot decode, and
    it fails a megabyte in - long after anything has looked like it worked."""
    subtree["/Evil/Engine/Patch/Kaften_Gate"] = node({"Thresh": percent()})
    categories["Utility"] = ["Käften Gate", "– em dash —"]

    directory = tmp_path / "live"
    directory.mkdir()
    (directory / "subtree.json").write_text(json.dumps(subtree, ensure_ascii=False), encoding="utf-8")
    (directory / "device-catalog.json").write_text(
        json.dumps(categories, ensure_ascii=False), encoding="utf-8"
    )

    assert from_device_capture(directory).models["Kaften Gate"]


def test_a_directory_without_a_dump_says_which_flag_to_use(tmp_path):
    with pytest.raises(ValueError, match="subtree.json"):
        from_device_capture(tmp_path)


def test_extract_saves_it_and_says_validation_changed(tmp_path, monkeypatch, subtree, categories):
    from rigsmith import config

    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    try:
        directory = tmp_path / "live"
        directory.mkdir()
        (directory / "subtree.json").write_text(json.dumps(subtree), encoding="utf-8")
        (directory / "device-catalog.json").write_text(json.dumps(categories), encoding="utf-8")

        report = extract(subtree_dir=directory)

        assert report.catalog.is_verified
        assert any("live subtree" in source for source in report.sources)
        assert any("lenient unverified mode" in note for note in report.notes)
        assert (config.get_settings().workspace / "catalog.json").is_file()
    finally:
        config.set_settings(None)


def test_extract_with_nothing_to_read_says_what_it_wanted(tmp_path):
    with pytest.raises(ValueError, match="live capture directory"):
        extract()

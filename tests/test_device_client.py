"""Tests for protocol maps and the native device client.

The client is driven entirely by a map, so these cover both: that a map
expresses the device's API faithfully as data, and that the client turns
rigsmith's intentions into the right requests.

Everything runs against the simulator in-process over an ASGI transport, so
no port is bound and no hardware is needed - which is the whole reason the
simulator exists.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="needs pip install 'rigsmith[ui]'")

import httpx  # noqa: E402

from rigsmith.core.errors import Invalid, NotFound  # noqa: E402
from rigsmith.device.client import DeviceClient  # noqa: E402
from rigsmith.protocol import map as protocol_maps  # noqa: E402
from rigsmith.protocol.map import Operation, ProtocolMap  # noqa: E402
from rigsmith.protocol.simulator import DeviceState, build_app  # noqa: E402
from rigsmith.rig.model import (  # noqa: E402
    Block,
    BlockCategory,
    Rig,
    Scene,
    SceneBlockState,
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A DeviceClient wired straight into a simulator app."""
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    from rigsmith import config

    config.set_settings(config.Settings())

    app = build_app(DeviceState())
    transport = httpx.ASGITransport(app=app)
    yield DeviceClient(
        "http://device", protocol_maps.get("simulator"), transport=transport
    )
    config.set_settings(None)


def sample_rig() -> Rig:
    rig = Rig(name="Cherub Rock")
    for category, model in [
        ("effect", "big-fuzz"),
        ("amp", "82-lead-800-100w"),
        ("cab", "4x12-65w"),
    ]:
        rig.add_block(
            Block(slot=0, category=BlockCategory(category), model_id=model)
        )
    rig.scenes.append(
        Scene(name="Verse", states={1: SceneBlockState.OFF}, footswitch=1)
    )
    return rig


# --------------------------------------------------------------------------
# protocol maps
# --------------------------------------------------------------------------


def test_simulator_map_is_shipped_and_marked_unverified():
    """An invented map must never pass for knowledge of the real device."""
    simulator = protocol_maps.get("simulator")
    assert simulator.verified is False
    assert "UNVERIFIED" in simulator.caveat()


def test_map_exposes_the_operations_the_client_needs():
    simulator = protocol_maps.get("simulator")
    for operation in (
        "status", "list_rigs", "get_rig", "create_rig",
        "add_block", "set_scene", "catalog",
    ):
        assert simulator.supports(operation), f"{operation} is missing"


def test_unknown_operation_lists_the_real_ones():
    simulator = protocol_maps.get("simulator")
    with pytest.raises(NotFound) as excinfo:
        simulator.operation("teleport")
    assert "create_rig" in str(excinfo.value)


def test_field_names_translate_to_the_device_vocabulary():
    simulator = protocol_maps.get("simulator")
    assert simulator.field("model_id") == "modelId"
    assert simulator.field("unmapped") == "unmapped"


def test_path_templates_are_filled():
    operation = Operation(name="get", method="GET", path="/rigs/{rig_id}")
    assert operation.render_path({"rig_id": 7}) == "/rigs/7"


def test_a_missing_path_value_says_which_one():
    operation = Operation(name="get", method="GET", path="/rigs/{rig_id}")
    with pytest.raises(Invalid, match="rig_id"):
        operation.render_path({})


def test_body_placeholders_preserve_types():
    """A dict placeholder must stay a dict, not become its repr."""
    operation = Operation(
        name="add",
        method="POST",
        path="/x",
        body={"params": "{params}", "slot": "{slot}", "fixed": 1},
    )
    body = operation.render_body({"params": {"gain": 5}, "slot": 3})
    assert body == {"params": {"gain": 5}, "slot": 3, "fixed": 1}


def test_body_omits_values_the_caller_did_not_supply():
    operation = Operation(
        name="add", method="POST", path="/x",
        body={"a": "{a}", "b": "{b}"},
    )
    assert operation.render_body({"a": 1}) == {"a": 1}


def test_unknown_firmware_is_refused_rather_than_guessed():
    """Requests shaped for one firmware sent to another can corrupt a rig."""
    with pytest.raises(NotFound) as excinfo:
        protocol_maps.for_firmware("99.0.0")
    assert "protocol record" in str(excinfo.value)


def test_firmware_matches_on_major_minor():
    simulator = protocol_maps.get("simulator")
    assert simulator.firmware == "5.1.0-sim"
    assert protocol_maps.for_firmware("5.1.0-sim").name == "simulator"


def test_map_rejects_a_spec_without_operations():
    with pytest.raises(Invalid, match="operations"):
        ProtocolMap.from_dict({"name": "broken"})


def test_map_rejects_an_operation_without_a_method(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(
        json.dumps({"name": "bad", "operations": {"x": {"path": "/x"}}}),
        encoding="utf-8",
    )
    with pytest.raises(Invalid, match="method"):
        ProtocolMap.load(path)


def test_render_surfaces_the_caveat():
    rendered = protocol_maps.get("simulator").render()
    assert "verified: False" in rendered
    assert "UNVERIFIED" in rendered


# --------------------------------------------------------------------------
# reads
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reads_status(client):
    async with client as device:
        status = await device.status()
    assert status["firmware"] == "5.1.0"


@pytest.mark.asyncio
async def test_unwraps_a_wrapped_list(client):
    """The map says responses arrive under a key; the client unwraps it."""
    async with client as device:
        rigs = await device.list_rigs()
    assert isinstance(rigs, list)
    assert rigs and "name" in rigs[0]


@pytest.mark.asyncio
async def test_reads_the_catalog(client):
    async with client as device:
        catalog = await device.catalog()
    assert len(catalog) > 20


@pytest.mark.asyncio
async def test_missing_rig_raises_not_found(client):
    async with client as device:
        with pytest.raises(NotFound):
            await device.get_rig(999)


@pytest.mark.asyncio
async def test_using_the_client_unopened_is_an_error():
    device = DeviceClient("http://x", protocol_maps.get("simulator"))
    from rigsmith.core.errors import Unavailable

    with pytest.raises(Unavailable, match="not open"):
        await device.status()


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_writes_a_whole_rig(client):
    async with client as device:
        report = await device.write_rig(sample_rig())
        assert report.ok, report.render()
        built = await device.get_rig(report.rig_id)

    assert built["name"] == "Cherub Rock"
    assert [b["modelId"] for b in built["blocks"]] == [
        "big-fuzz", "82-lead-800-100w", "4x12-65w"
    ]
    assert built["scenes"][0]["name"] == "Verse"


@pytest.mark.asyncio
async def test_write_reports_every_step(client):
    async with client as device:
        report = await device.write_rig(sample_rig())

    operations = [step.operation for step in report.steps]
    assert operations == [
        "create_rig", "add_block", "add_block", "add_block", "set_scene"
    ]
    assert all(step.ok for step in report.steps)


@pytest.mark.asyncio
async def test_write_surfaces_an_unverified_map(client):
    """A write performed through a guessed protocol has to say so."""
    async with client as device:
        report = await device.write_rig(sample_rig())
    assert report.verified_map is False
    assert "UNVERIFIED" in report.render()


@pytest.mark.asyncio
async def test_write_stops_at_the_first_refusal(client):
    """Pressing on would bury the first failure under later ones."""
    rig = Rig(name="Too big")
    for index in range(14):
        rig.add_block(
            Block(slot=0, category=BlockCategory.EFFECT, model_id=f"p{index}")
        )
    # A 15th block the device will refuse.
    rig.blocks.append(
        Block(slot=14, category=BlockCategory.EFFECT, model_id="one-too-many")
    )

    async with client as device:
        report = await device.write_rig(rig)

    assert not report.ok
    assert report.steps[-1].operation == "write"
    assert not report.steps[-1].ok
    assert "stopped" in report.render().lower()


@pytest.mark.asyncio
async def test_scene_no_change_entries_are_not_sent(client):
    """NO_CHANGE is the absence of an instruction, not an instruction."""
    rig = Rig(name="Scenes")
    for model in ("a", "b"):
        rig.add_block(
            Block(slot=0, category=BlockCategory.EFFECT, model_id=model)
        )
    rig.scenes.append(
        Scene(
            name="V",
            states={1: SceneBlockState.ON, 2: SceneBlockState.NO_CHANGE},
        )
    )

    async with client as device:
        report = await device.write_rig(rig)
        built = await device.get_rig(report.rig_id)

    assert built["scenes"][0]["states"] == {"1": "on"}


@pytest.mark.asyncio
async def test_write_report_serialises(client):
    async with client as device:
        report = await device.write_rig(sample_rig())
    data = report.to_dict()
    assert data["ok"] is True
    assert data["verified_map"] is False
    assert len(data["steps"]) == 5


def test_a_write_report_carries_its_caveats_as_text_not_just_a_flag():
    """A JSON consumer needs the same warning a terminal gets. 'verified_map:
    false' is a boolean anyone can skip past; a sentence is not."""
    from rigsmith.device.client import Step, WriteReport

    report = WriteReport(
        rig_name="Cherub Rock",
        rig_id=3,
        steps=[Step(operation="create_rig", ok=True, detail="id 3")],
        verified_map=False,
        map_name="simulator",
    )

    assert any("UNVERIFIED" in note for note in report.caveats)
    assert any("UNVERIFIED" in note for note in report.to_dict()["caveats"])
    # And the rendered text still says it, for the terminal.
    assert "UNVERIFIED" in report.render()


def test_a_verified_map_that_succeeded_has_nothing_to_warn_about():
    from rigsmith.device.client import Step, WriteReport

    report = WriteReport(
        rig_name="R",
        steps=[Step(operation="create_rig", ok=True)],
        verified_map=True,
        map_name="fw-5.1",
    )
    assert report.caveats == []


def test_a_partial_write_says_the_device_holds_half_a_rig():
    from rigsmith.device.client import Step, WriteReport

    report = WriteReport(
        rig_name="R",
        steps=[
            Step(operation="create_rig", ok=True),
            Step(operation="add_block", ok=False, detail="refused"),
        ],
        verified_map=True,
        map_name="fw-5.1",
    )
    notes = " ".join(report.caveats)
    assert "partial rig" in notes
    assert "Review it before retrying" in notes

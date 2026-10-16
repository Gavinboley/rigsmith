"""Writing a rig to a device that is an object tree rather than a REST API.

Both bugs covered here were found against real hardware, and both are the
quiet kind: the write reported success at every step, the values read back
correct, and the rig on the device was wrong anyway.

* ``makeNewRig`` returns immediately and loads the blank rig *afterwards*.
  Everything written in between goes into the rig that is still open, reads
  back perfectly, and is wiped when the blank one arrives. What gets saved is
  empty.
* Setting several properties in one request is not the same as setting them
  one at a time - some of them reset others. A cab's microphone written
  alongside its ``Doubling`` stayed on the wrong model.
"""

from __future__ import annotations

import httpx
import pytest

from rigsmith.device.client import DeviceClient
from rigsmith.protocol.map import ProtocolMap
from rigsmith.rig.catalog import Catalog, ModelSpec, ParamSpec
from rigsmith.rig.model import Block, BlockCategory, Doubling, Rig

MAP = ProtocolMap.from_dict(
    {
        "name": "fake-core",
        "base": "/api/v1",
        "verified": True,
        "field_names": {"rig_id": "loadedID", "rig_name": "loadedName"},
        "operations": {
            "loaded_rig": {"method": "GET", "path": "/object-properties/Evil/API/Rigs"},
            "list_rigs": {"method": "GET", "path": "/object-properties/Evil/API/Rigs"},
            "get_chain": {
                "method": "GET",
                "path": "/object-properties/Evil/Engine/Patch/Chain",
            },
            "set_chain": {
                "method": "PUT",
                "path": "/object-properties/Evil/Engine/Patch/Chain",
                "body": "{properties}",
            },
            "get_block": {
                "method": "GET",
                "path": "/object-properties/Evil/Engine/Patch/{block}",
            },
            "set_block": {
                "method": "PUT",
                "path": "/object-properties/Evil/Engine/Patch/{block}",
                "body": "{properties}",
            },
            "new_rig": {
                "method": "POST",
                "path": "/object-method/Evil/API/Rigs/makeNewRig",
                "args": ["{colour}"],
                "unwrap": "methodReturnValue",
            },
            "save_rig_as": {
                "method": "POST",
                "path": "/object-method/Evil/API/Rigs/saveRigAs",
                "args": ["{name}", "{colour}"],
                "unwrap": "methodReturnValue",
            },
        },
        "procedures": {
            "write_rig": {"steps": [{"operation": "new_rig", "with": {}}]}
        },
    }
)


def catalog() -> Catalog:
    cat = Catalog(source="test")
    percent = lambda n: ParamSpec(  # noqa: E731
        name=n, kind="float", minimum=0.0, maximum=1.0,
        display_min=0.0, display_max=100.0, taper="linear",
    )
    cat.models["Amp"] = ModelSpec(
        id="Amp", name="Amp", category=BlockCategory.AMP, verified=True,
        device_index=1,
        params=[percent("GainA"), percent("Bass"),
                ParamSpec(name="Type", kind="int", minimum=0, maximum=52),
                ParamSpec(name="Doubling", kind="bool"),
                ParamSpec(name="DoubleMode", kind="bool"),
                ParamSpec(name="On", kind="bool")],
    )
    for index, name in ((25, "800 Bass Mod"), (36, "Treadplate Modern")):
        cat.models[name] = ModelSpec(
            id=name, name=name, category=BlockCategory.AMP, verified=True,
            device_index=index, selector="Amp.Type",
            params=cat.models["Amp"].params,
        )
    cat.models["Spring Reverb"] = ModelSpec(
        id="Spring Reverb", name="Spring Reverb", category=BlockCategory.EFFECT, verified=True,
        device_index=7, params=[percent("Mix"), ParamSpec(name="On", kind="bool")],
    )
    return cat


def rig_with_a_doubled_amp() -> Rig:
    rig = Rig(name="Going Under")
    rig.blocks = [
        Block(
            slot=3, category=BlockCategory.AMP, model_id="800 Bass Mod",
            doubling=Doubling.STEREO, doubled_model_id="Treadplate Modern",
            params={"GainA": 0.74, "Bass": 0.6},
        )
    ]
    return rig


class FakeCore:
    """A Core that behaves the way the real one actually does."""

    def __init__(self, *, new_rig_delay: int = 0, resets: dict | None = None):
        self.new_rig_delay = new_rig_delay
        """How many requests pass before the blank rig actually lands."""
        self.resets = resets or {}
        """``{property: property_it_clobbers}`` when written together."""

        self.rig_id = "old-rig"
        self.loaded_name = "Bleach"
        self.chain = {f"ModuleType{n}": 0 for n in range(1, 15)}
        self.chain["ModuleType1"] = 99  # something was already open
        self.blocks: dict[str, dict] = {}
        self.saved: dict | None = None
        self._pending: int | None = None
        self.requests = 0

    def _tick(self):
        self.requests += 1
        if self._pending is None:
            return
        self._pending -= 1
        if self._pending <= 0:
            # The blank rig finally arrives - and wipes everything.
            self._pending = None
            self.rig_id = "new-rig"
            self.loaded_name = ""
            self.chain = {f"ModuleType{n}": 0 for n in range(1, 15)}
            self.blocks = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        self._tick()
        path = request.url.path
        body = {}
        if request.content:
            import json

            body = json.loads(request.content)

        if path.endswith("/Evil/API/Rigs"):
            return httpx.Response(
                200, json={"loadedID": self.rig_id, "loadedName": self.loaded_name,
                           "RigNames": [], "RigIds": []},
            )
        if path.endswith("/Chain"):
            if request.method == "PUT":
                self.chain.update(body)
                return httpx.Response(200, json={})
            return httpx.Response(200, json=self.chain)
        if "makeNewRig" in path:
            self._pending = max(self.new_rig_delay, 1)
            if self.new_rig_delay == 0:
                self._pending = None
                self.rig_id = "new-rig"
                self.loaded_name = ""
                self.chain = {f"ModuleType{n}": 0 for n in range(1, 15)}
                self.blocks = {}
            return httpx.Response(200, json={"methodReturnValue": True})
        if "saveRigAs" in path:
            self.saved = {
                "name": body["arguments"][0],
                "chain": dict(self.chain),
                "blocks": {k: dict(v) for k, v in self.blocks.items()},
            }
            self.loaded_name = body["arguments"][0]
            self.rig_id = "saved-rig"
            return httpx.Response(200, json={"methodReturnValue": True})

        block = path.rsplit("/", 1)[-1]
        state = self.blocks.setdefault(block, {})
        if request.method == "PUT":
            for name, value in body.items():
                state[name] = value
                clobbered = self.resets.get(name)
                if clobbered and clobbered in body:
                    state[clobbered] = 0
            return httpx.Response(200, json={})
        return httpx.Response(200, json=state)


@pytest.fixture()
def patched_catalog(monkeypatch):
    monkeypatch.setattr("rigsmith.rig.catalog.get_catalog", lambda *a, **k: catalog())


# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_doubled_amp_reaches_the_device(patched_catalog):
    """2X is one block with two models, and it never used to be sent at all."""
    core = FakeCore()
    async with DeviceClient(
        "http://fake", MAP, transport=httpx.MockTransport(core.handle)
    ) as client:
        report = await client.write_rig(rig_with_a_doubled_amp())

    assert report.ok, report.render()
    amp = core.saved["blocks"]["Amp"]
    assert amp["Type"] == 25
    assert amp["Type2"] == 36, "the second model is the whole point of 2X"
    assert amp["Doubling"] is True
    assert amp["DoubleMode"] is False, "stereo, not switch"
    assert amp["GainA"] == 0.74


@pytest.mark.asyncio
async def test_the_chain_is_written_as_module_indexes(patched_catalog):
    core = FakeCore()
    async with DeviceClient(
        "http://fake", MAP, transport=httpx.MockTransport(core.handle)
    ) as client:
        await client.write_rig(rig_with_a_doubled_amp())

    chain = core.saved["chain"]
    assert chain["ModuleType3"] == 1, "slot 3 holds the Amp module"
    assert chain["ModuleType1"] == 0, "the previous rig's block is gone"


@pytest.mark.asyncio
async def test_it_waits_for_a_slow_new_rig_instead_of_saving_a_blank(patched_catalog):
    """The bug. Everything read back correct and the saved rig was empty."""
    core = FakeCore(new_rig_delay=3)
    async with DeviceClient(
        "http://fake", MAP, transport=httpx.MockTransport(core.handle)
    ) as client:
        report = await client.write_rig(rig_with_a_doubled_amp())

    assert report.ok, report.render()
    assert core.saved is not None
    assert core.saved["chain"]["ModuleType3"] == 1, (
        "the rig was saved before the blank landed, so the blank wiped it"
    )
    assert core.saved["blocks"]["Amp"]["Type"] == 25


@pytest.mark.asyncio
async def test_a_new_rig_that_never_lands_fails_loudly(patched_catalog, monkeypatch):
    """Better to refuse than to save whatever happened to be open."""
    monkeypatch.setattr(DeviceClient, "NEW_RIG_TIMEOUT", 0.5)
    core = FakeCore(new_rig_delay=10_000)
    async with DeviceClient(
        "http://fake", MAP, transport=httpx.MockTransport(core.handle)
    ) as client:
        report = await client.write_rig(rig_with_a_doubled_amp())

    assert not report.ok
    assert core.saved is None, "nothing should have been saved"
    assert any("did not finish loading" in step.detail for step in report.steps)


@pytest.mark.asyncio
async def test_a_property_that_gets_clobbered_is_written_again(patched_catalog):
    """Writing Doubling alongside a picker reset the picker, silently."""
    core = FakeCore(resets={"Doubling": "Type"})
    async with DeviceClient(
        "http://fake", MAP, transport=httpx.MockTransport(core.handle)
    ) as client:
        report = await client.write_rig(rig_with_a_doubled_amp())

    assert report.ok, report.render()
    assert core.saved["blocks"]["Amp"]["Type"] == 25, (
        "the reconcile pass should have put it back"
    )
    assert not any("stayed" in note for note in report.notes)


@pytest.mark.asyncio
async def test_a_value_the_device_keeps_refusing_is_reported(patched_catalog):
    """If it will not stick, say so rather than let the rig look right."""
    core = FakeCore()
    original = core.handle

    def stubborn(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "PUT" and request.url.path.endswith("/Amp"):
            core.blocks["Amp"]["Type"] = 0
        return response

    async with DeviceClient(
        "http://fake", MAP, transport=httpx.MockTransport(stubborn)
    ) as client:
        report = await client.write_rig(rig_with_a_doubled_amp())

    assert any("Type stayed" in note for note in report.notes), report.notes
    assert any("refused" in step.detail for step in report.steps)
    # Saved, but not the draft - and the verdict says so, not just a note.
    assert report.ok and not report.exact and report.refused == ["Amp.Type"]
    assert "kept 1 value(s) of its own" in report.render() and report.to_dict()["exact"] is False


def test_scenes_are_read_raw_and_written_as_instructions():
    from rigsmith.core.devices import parse_scenes, render_scenes
    from rigsmith.device.client import scene_instructions
    from rigsmith.rig.model import Block, BlockCategory, Rig, Scene, SceneBlockState

    values = {"ModeNew1": 2, "UserFootSwitchText1": "Verse", "Scene1_1_Effect": "Amp", "Scene1_1_Mode": 1,
              "Scene1_2_Effect": "Cab", "Scene1_2_Mode": 2, "Scene1_3_Effect": "Empty Slot", "Scene1_3_Mode": 0,
              "ModeNew2": 0, "Scene2_1_Effect": "Amp", "Scene2_1_Mode": 0}
    rows = parse_scenes(values)
    assert rows[0]["mode"] == "Scene" and rows[0]["label"] == "Verse"
    assert [(s["block"], s["mode"]) for s in rows[0]["slots"]] == [("Amp", 1), ("Cab", 2)]
    text = render_scenes(rows)
    assert "FS1 [Scene] 'Verse': 1:Amp=1, 2:Cab=2" in text and "FS2" not in text

    rig = Rig(name="song")
    rig.blocks = [Block(slot=1, category=BlockCategory.AMP, model_id="82 Lead 800 100W"), Block(slot=2, category=BlockCategory.AMP, model_id="64 Black Lux Norm")]
    rig.scenes = [Scene(name="verse", states={1: SceneBlockState.ON, 2: SceneBlockState.OFF}, footswitch=1)]
    note = scene_instructions(rig)
    assert "not written over the network" in note and "FS1 'verse': on 82 Lead 800 100W (1); off 64 Black Lux Norm (2)" in note


@pytest.mark.asyncio
async def test_the_rig_tempo_goes_on_the_rig_node_as_fixed(patched_catalog):
    """A synced delay is only right at the song's tempo, so the rig carries it."""
    rig = rig_with_a_doubled_amp()
    rig.tempo = 140.0
    core = FakeCore()
    async with DeviceClient(
        "http://fake", MAP, transport=httpx.MockTransport(core.handle)
    ) as client:
        report = await client.write_rig(rig)

    assert report.ok, report.render()
    node = core.saved["blocks"]["Rig"]
    assert node["TempoFromMaster"] is False, "Fixed - the rig loads with its own tempo"
    assert node["Tempo"] == pytest.approx((140 - 30) / 210, abs=1e-6)
    assert any(step.operation == "set_rig" for step in report.steps)


def test_rig_tempo_on_the_wire_matches_what_the_core_stores():
    from rigsmith.device.writer import rig_properties

    # The Core's own default, read from a device: 120 BPM is 0.4285714.
    assert rig_properties(Rig(name="x", tempo=120.0))["Tempo"] == pytest.approx(0.4285714, abs=1e-6)
    assert rig_properties(Rig(name="x")) == {}, "no tempo leaves the device's alone"


@pytest.mark.asyncio
async def test_a_refused_rig_tempo_is_reported_and_the_rig_still_saved(patched_catalog):
    rig = rig_with_a_doubled_amp()
    rig.tempo = 95.0
    core = FakeCore()
    original = core.handle

    def refuses_tempo(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith("/Rig"):
            return httpx.Response(400, text="no such property")
        return original(request)

    async with DeviceClient(
        "http://fake", MAP, transport=httpx.MockTransport(refuses_tempo)
    ) as client:
        report = await client.write_rig(rig)

    assert core.saved is not None, "the blocks matter more than the tempo"
    assert any("95 BPM, Tempo: Fixed" in note for note in report.notes), report.notes


@pytest.mark.asyncio
async def test_the_rig_being_replaced_goes_only_after_its_replacement_is_saved(patched_catalog):
    """Deleting first lost the old rig whenever anything after it failed."""
    core = FakeCore()
    calls = []
    original = core.handle

    def watching(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if "deleteRig" in request.url.path:
            return httpx.Response(200, json={"methodReturnValue": True})
        return original(request)

    import copy

    replacing = copy.deepcopy(MAP)
    replacing.operations["delete_rig"] = ProtocolMap.from_dict({"name": "x", "base": "/api/v1", "operations": {"delete_rig": {
        "method": "POST", "path": "/object-method/Evil/API/Rigs/deleteRig", "args": ["{rig_id}"], "unwrap": "methodReturnValue"}}}).operations["delete_rig"]
    async with DeviceClient("http://fake", replacing, transport=httpx.MockTransport(watching)) as client:
        report = await client.write_rig(rig_with_a_doubled_amp(), replace_id="old-id")
    assert report.ok, report.render()
    assert calls.index("deleteRig") > calls.index("saveRigAs")


def test_a_guessed_second_instance_says_so():
    """Second instances sit after the first in the table - a convention, so it is reported."""
    from rigsmith.device.writer import plan_rig

    rig = Rig(name="two amps")
    rig.blocks = [
        Block(slot=1, category=BlockCategory.AMP, model_id="800 Bass Mod"),
        Block(slot=2, category=BlockCategory.AMP, model_id="Treadplate Modern"),
    ]
    first, second = plan_rig(rig, catalog())
    assert not first.notes and second.module_index == 2
    assert any("taken as the one after" in note for note in second.notes)


def _with_properties():
    import copy

    mapped = copy.deepcopy(MAP)
    extra = ProtocolMap.from_dict({"name": "x", "base": "/api/v1", "operations": {
        "get_properties": {"method": "GET", "path": "/object-properties{node}"},
        "set_properties": {"method": "PUT", "path": "/object-properties{node}", "body": "{properties}"},
    }})
    mapped.operations.update(extra.operations)
    return mapped


def scene_rig():
    from rigsmith.rig.model import Scene, SceneBlockState

    rig = rig_with_a_doubled_amp()
    rig.blocks.append(Block(slot=5, category=BlockCategory.AMP, model_id="Treadplate Modern"))
    rig.scenes = [
        Scene(name="verse", states={3: SceneBlockState.ON, 5: SceneBlockState.OFF}, footswitch=1),
        Scene(name="chorus", states={3: SceneBlockState.OFF, 5: SceneBlockState.ON}, footswitch=2),
    ]
    return rig


def test_the_on_and_off_numbers_are_learned_from_a_scene_set_by_hand():
    from rigsmith.core.devices import learn_scene_modes, parse_scenes

    rows = parse_scenes({"ModeNew1": 2, "Scene1_1_Effect": "Amp", "Scene1_1_Mode": 2, "Scene1_2_Effect": "Cab", "Scene1_2_Mode": 1})
    assert learn_scene_modes(rows) == {"on": 2, "off": 1, "no_change": 0}
    for values in (
        {"ModeNew1": 0, "Scene1_1_Effect": "Amp", "Scene1_1_Mode": 2, "Scene1_2_Effect": "Cab", "Scene1_2_Mode": 1},
        {"ModeNew1": 2, "Scene1_1_Effect": "Amp", "Scene1_1_Mode": 1, "Scene1_2_Effect": "Cab", "Scene1_2_Mode": 1},
        {"ModeNew1": 2, "Scene1_1_Effect": "Amp", "Scene1_1_Mode": 1},
    ):
        with pytest.raises(Exception):
            learn_scene_modes(parse_scenes(values))


@pytest.mark.asyncio
async def test_with_the_numbers_learned_a_write_carries_its_scenes(patched_catalog, monkeypatch):
    monkeypatch.setattr("rigsmith.device.client._scene_modes", lambda: {"on": 1, "off": 2, "no_change": 0})
    core = FakeCore()
    async with DeviceClient("http://fake", _with_properties(), transport=httpx.MockTransport(core.handle)) as client:
        report = await client.write_rig(scene_rig())
    assert report.exact, report.render()
    switch = core.saved["blocks"]["FootSwitch"]
    assert switch["ModeNew1"] == 2 and switch["UserFootSwitchText1"] == "verse"
    assert switch["Scene1_3_Mode"] == 1 and switch["Scene1_5_Mode"] == 2
    assert switch["Scene2_3_Mode"] == 2 and switch["Scene2_5_Mode"] == 1
    assert not any("not written over the network" in note for note in report.notes)


@pytest.mark.asyncio
async def test_a_scene_value_the_device_drops_is_reported(patched_catalog, monkeypatch):
    monkeypatch.setattr("rigsmith.device.client._scene_modes", lambda: {"on": 1, "off": 2, "no_change": 0})
    core = FakeCore()
    original = core.handle

    def drops_labels(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "PUT" and request.url.path.endswith("/FootSwitch"):
            core.blocks["FootSwitch"].pop("UserFootSwitchText1", None)
        return response

    async with DeviceClient("http://fake", _with_properties(), transport=httpx.MockTransport(drops_labels)) as client:
        report = await client.write_rig(scene_rig())
    assert report.ok and not report.exact and "FootSwitch.UserFootSwitchText1" in report.refused


def test_a_live_nudge_only_moves_knobs_on_the_rig_it_belongs_to(patched_catalog, monkeypatch, tmp_path):
    """Sent to whatever rig was loaded, a nudge would retune somebody else's sound."""
    import asyncio

    from rigsmith import config
    from rigsmith.core import devices
    from rigsmith.core.errors import Conflict
    from rigsmith.device import client as client_module

    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    core = FakeCore()
    real = client_module.DeviceClient

    class Faked(real):
        def __init__(self, base_url, protocol, **kwargs):
            super().__init__(base_url, MAP, transport=httpx.MockTransport(core.handle), **kwargs)

    monkeypatch.setattr(client_module, "DeviceClient", Faked)
    rig = rig_with_a_doubled_amp()
    with pytest.raises(Conflict):
        asyncio.run(devices.send_live(rig, 3, {"Treble": 0.6}, host="fake"))
    assert "Amp" not in core.blocks, "nothing was sent to the wrong rig"
    core.loaded_name = rig.name
    sent = asyncio.run(devices.send_live(rig, 3, {"Bass": 0.7}, host="fake"))
    assert sent["node"] == "Amp" and core.blocks["Amp"]["Bass"] == 0.7 and not sent["refused"]
    # "Too much reverb" turns the reverb's own node, not the amp's.
    rig.blocks.append(Block(slot=5, category=BlockCategory.EFFECT, model_id="Spring Reverb", params={"Mix": 0.3}))
    sent = asyncio.run(devices.send_live(rig, 5, {"Mix": 0.22}, host="fake"))
    assert sent["node"] == "Spring_Reverb" and core.blocks["Spring_Reverb"]["Mix"] == 0.22 and not sent["refused"]
    assert "Mix" not in core.blocks["Amp"]
    config.set_settings(None)

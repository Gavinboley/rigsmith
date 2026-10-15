"""Measuring the knob curves against a Core, from the app as well as the CLI.

Every continuous control is 0-1 on the wire; what the knob reads is a curve
nobody publishes. Calibration reads both at once from the loaded rig and
saves the curve that fits - here against a fake Core whose MidFreq is linear
and whose slot-EQ frequency is logarithmic, as the real one's are.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from rigsmith import config
from rigsmith.core import devices
from rigsmith.rig.catalog import Catalog, ModelSpec, ParamSpec, get_catalog
from rigsmith.rig.model import BlockCategory


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    cat = Catalog(source="device")
    cat.models["Amp"] = ModelSpec(
        id="Amp", name="Amp", category=BlockCategory.AMP, verified=True, device_index=1,
        params=[
            ParamSpec(name="MidFreq", kind="float", minimum=0.0, maximum=1.0, display_min=220.0, display_max=3000.0, unit="Hz"),
            ParamSpec(name="SltEQF", kind="float", minimum=0.0, maximum=1.0, display_min=20.0, display_max=16000.0, unit="Hz"),
        ],
    )
    for name, index in (("92 Treadplate Modern", 36), ("64 Fox AC30", 7)):
        cat.models[name] = ModelSpec(
            id=name, name=name, category=BlockCategory.AMP, verified=True, device_index=index, selector="Amp.Type",
            params=[
                ParamSpec(name="MidFreq", kind="float", minimum=0.0, maximum=1.0, display_min=220.0, display_max=3000.0, unit="Hz"),
                ParamSpec(name="SltEQF", kind="float", minimum=0.0, maximum=1.0, display_min=20.0, display_max=10000.0, unit="Hz"),
            ],
        )
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    cat.save(tmp_path / "ws" / "catalog.json")
    yield tmp_path
    config.set_settings(None)


def fake_core(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/Patch/Chain"):
        return httpx.Response(200, json={"ModuleType1": 1, **{f"ModuleType{n}": 0 for n in range(2, 15)}})
    if path.endswith("/Evil/API/Blocks"):
        return httpx.Response(200, json={"ModuleTypes": ["Empty", "Amp"]})
    if path.endswith("/Patch/Amp"):
        return httpx.Response(200, json={"MidFreq": 0.25, "SltEQF": 0.5})
    if "serializeBlock" in path:
        shown = {"MidFreq": {"type": 0, "value": 220 + 0.25 * 2780}, "SltEQF": {"type": 0, "value": 20 * 800 ** 0.5}}
        return httpx.Response(200, json={"methodReturnValue": json.dumps({"data": {"Amp": {"children": shown}}})})
    return httpx.Response(404)


def test_the_curves_are_measured_and_saved(workspace, monkeypatch):
    from rigsmith.device import client as client_module

    real = client_module.DeviceClient

    class Faked(real):
        def __init__(self, base_url, protocol, **kwargs):
            super().__init__(base_url, protocol, transport=httpx.MockTransport(fake_core), **kwargs)

    monkeypatch.setattr(client_module, "DeviceClient", Faked)
    before = devices.catalog_status()
    assert before["verified"] and before["measured"] == 0 and before["controls"] == 6

    result = asyncio.run(devices.calibrate_catalog("192.0.2.1"))
    assert result["blocks"] == ["Amp"] and result["measured"] == 2 and result["saved"]
    cat = get_catalog(reload=True)
    amp = cat.models["Amp"]
    assert amp.param("MidFreq").taper == "linear" and amp.param("SltEQF").taper == "log"
    # The amp models are the Amp block's Type: its knobs are theirs.
    for name in ("92 Treadplate Modern", "64 Fox AC30"):
        assert cat.models[name].param("MidFreq").taper == "linear"
        assert cat.models[name].param("SltEQF").taper == "unknown", "a different displayed range is not the same control"
    assert any("2 model(s) selected on Amp" in line for line in result["lines"])
    assert devices.catalog_status()["measured"] == 4


def test_an_unextracted_catalog_is_refused_before_the_device_is_touched(tmp_path, monkeypatch):
    from rigsmith.core.errors import Invalid

    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "empty"))
    config.set_settings(config.Settings())
    with pytest.raises(Invalid) as refused:
        asyncio.run(devices.calibrate_catalog("192.0.2.1"))
    assert "extract" in (refused.value.hint or "")
    config.set_settings(None)


def test_curves_measured_on_a_block_reach_its_models_when_the_catalog_loads(workspace):
    """A catalog calibrated before sharing existed still benefits from it."""
    cat = Catalog.load(workspace / "ws" / "catalog.json")
    cat.models["Amp"].param("MidFreq").taper = "linear"
    cat.save(workspace / "ws" / "catalog.json")
    loaded = get_catalog(reload=True)
    assert loaded.models["92 Treadplate Modern"].param("MidFreq").taper == "linear"
    assert Catalog.load(workspace / "ws" / "catalog.json").models["64 Fox AC30"].param("MidFreq").taper == "unknown", "the file is not rewritten"

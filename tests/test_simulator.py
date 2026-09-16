"""Tests for the device simulator.

The simulator's value is that the whole device-facing toolchain can be
exercised without hardware, so most of what is asserted here is that the
*other* tools work against it - probe extraction, catalog extraction - rather
than that the simulator itself is correct in isolation.

It is not the real protocol and nothing here claims otherwise. When real
traffic is captured, replay_app serves that instead.
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("fastapi", reason="the simulator needs pip install 'rigsmith[ui]'")

from starlette.testclient import TestClient  # noqa: E402

from rigsmith.protocol.simulator import (  # noqa: E402
    DeviceState,
    build_app,
    build_bundle_js,
    build_catalog,
    replay_app,
)


@pytest.fixture()
def device(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    from rigsmith import config

    config.set_settings(config.Settings())
    with TestClient(build_app(DeviceState())) as client:
        yield client
    config.set_settings(None)


# --------------------------------------------------------------------------
# honesty
# --------------------------------------------------------------------------


def test_it_declares_itself_simulated(device):
    """Nothing should mistake this for a real device."""
    assert device.get("/api/v1/status").json()["simulated"] is True


def test_the_websocket_hello_also_declares_it(device):
    with device.websocket_connect("/api/v1/ws") as socket:
        assert socket.receive_json()["simulated"] is True


# --------------------------------------------------------------------------
# behaving like a device
# --------------------------------------------------------------------------


def test_serves_an_editor_bundle(device):
    page = device.get("/")
    assert page.status_code == 200
    assert "HeadRush Remote Editor" in page.text
    assert device.get("/app.js").status_code == 200


def test_lists_and_creates_rigs(device):
    assert len(device.get("/api/v1/rigs").json()["rigs"]) == 2

    created = device.post("/api/v1/rigs", json={"name": "Cherub Rock"}).json()
    assert created["name"] == "Cherub Rock"
    assert len(device.get("/api/v1/rigs").json()["rigs"]) == 3


def test_unnamed_rig_is_refused(device):
    assert device.post("/api/v1/rigs", json={}).status_code == 400


def test_missing_rig_is_404(device):
    assert device.get("/api/v1/rigs/999").status_code == 404


def test_blocks_are_added_in_slot_order(device):
    rig = device.post("/api/v1/rigs", json={"name": "T"}).json()
    for model in ("big-fuzz", "82-lead-800-100w", "4x12-65w"):
        device.post(f"/api/v1/rigs/{rig['id']}/blocks", json={"modelId": model})

    blocks = device.get(f"/api/v1/rigs/{rig['id']}").json()["blocks"]
    assert [b["slot"] for b in blocks] == [1, 2, 3]


def test_it_enforces_the_fourteen_slot_limit(device):
    """The one real constraint worth mirroring, since rigs are built against it."""
    rig = device.post("/api/v1/rigs", json={"name": "Full"}).json()
    for _ in range(14):
        response = device.post(
            f"/api/v1/rigs/{rig['id']}/blocks", json={"modelId": "tape-echo"}
        )
        assert response.status_code == 200

    overflow = device.post(
        f"/api/v1/rigs/{rig['id']}/blocks", json={"modelId": "tape-echo"}
    )
    assert overflow.status_code == 409


def test_occupied_slot_is_refused(device):
    rig = device.post("/api/v1/rigs", json={"name": "T"}).json()
    device.post(f"/api/v1/rigs/{rig['id']}/blocks", json={"modelId": "a", "slot": 3})
    clash = device.post(
        f"/api/v1/rigs/{rig['id']}/blocks", json={"modelId": "b", "slot": 3}
    )
    assert clash.status_code == 409


def test_cpu_rises_with_block_count(device):
    idle = device.get("/api/v1/status").json()["cpu"]
    rigs = device.get("/api/v1/rigs").json()["rigs"]
    current = rigs[0]["id"]
    for _ in range(4):
        device.post(f"/api/v1/rigs/{current}/blocks", json={"modelId": "tape-echo"})
    assert device.get("/api/v1/status").json()["cpu"] > idle


def test_websocket_round_trips_a_command(device):
    with device.websocket_connect("/api/v1/ws") as socket:
        socket.receive_json()  # hello
        socket.send_text(json.dumps({"type": "select_rig", "payload": {"id": 2}}))
        reply = socket.receive_json()
        assert reply["type"] == "rig_changed"
        assert reply["id"] == 2


# --------------------------------------------------------------------------
# what it unblocks
# --------------------------------------------------------------------------


def test_its_bundle_yields_endpoints_to_the_extractor():
    """The point of the simulator: the extractor gets realistic input."""
    from rigsmith.protocol.capture import analyse_bundle

    bundle = build_bundle_js(build_catalog())
    endpoints, _, websockets, _, message_types = analyse_bundle(
        {"http://device/app.js": bundle}
    )

    paths = {e.path for e in endpoints}
    assert "/api/v1/rigs" in paths
    assert "/api/v1/status" in paths
    assert any(e.evidence == "call" for e in endpoints)
    assert "/api/v1/ws" in websockets
    assert {"select_rig", "footswitch"} <= set(message_types)


def test_its_bundle_yields_a_catalog_to_the_extractor(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    from rigsmith import config

    config.set_settings(config.Settings())

    assets = tmp_path / "capture" / "assets"
    assets.mkdir(parents=True)
    (assets / "app.js").write_text(build_bundle_js(build_catalog()), encoding="utf-8")

    from rigsmith.protocol.catalog_extract import extract

    report = extract(capture_dir=tmp_path / "capture")
    assert report.model_count > 20

    from rigsmith.rig.catalog import get_catalog

    catalog = get_catalog(reload=True)
    assert catalog.is_verified
    amp = next(m for m in catalog.models.values() if m.category.value == "amp")
    assert amp.param("gain") is not None
    config.set_settings(None)


def test_catalog_uses_real_model_names():
    """Names are factual; parameters are invented. Realistic input matters."""
    names = {m["name"] for m in build_catalog()}
    assert "82 LEAD 800 100W" in names
    assert "4X12 65W" in names


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def write_recording(path, entries):
    with path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "meta", "upstream": "http://x"}) + "\n")
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")
    return path


def test_replay_serves_recorded_responses(tmp_path):
    recording = write_recording(
        tmp_path / "traffic.jsonl",
        [
            {
                "kind": "http", "seq": 1, "at": 0, "method": "GET",
                "path": "/api/v1/status", "status": 200,
                "response_body": '{"firmware":"5.1.0","real":true}',
                "headers": {"content-type": "application/json"},
            }
        ],
    )
    with TestClient(replay_app(recording)) as client:
        response = client.get("/api/v1/status")
        assert response.status_code == 200
        assert response.json()["real"] is True


def test_replay_reports_what_was_never_captured(tmp_path):
    recording = write_recording(
        tmp_path / "traffic.jsonl",
        [{"kind": "http", "seq": 1, "at": 0, "method": "GET",
          "path": "/api/v1/status", "status": 200, "response_body": "{}"}],
    )
    with TestClient(replay_app(recording)) as client:
        response = client.get("/api/v1/rigs")
        assert response.status_code == 404
        assert "never captured" in response.json()["hint"]


def test_replay_returns_repeats_in_the_order_they_happened(tmp_path):
    """A sequence of differing responses to one endpoint must replay in order."""
    recording = write_recording(
        tmp_path / "traffic.jsonl",
        [
            {"kind": "http", "seq": 1, "at": 0, "method": "GET", "path": "/x",
             "status": 200, "response_body": '{"n":1}'},
            {"kind": "http", "seq": 2, "at": 1, "method": "GET", "path": "/x",
             "status": 200, "response_body": '{"n":2}'},
        ],
    )
    with TestClient(replay_app(recording)) as client:
        assert client.get("/x").json()["n"] == 1
        assert client.get("/x").json()["n"] == 2
        # Past the end it holds the last response rather than failing.
        assert client.get("/x").json()["n"] == 2


def test_replay_rejects_an_empty_recording(tmp_path):
    from rigsmith.core.errors import Invalid

    empty = tmp_path / "traffic.jsonl"
    empty.write_text(json.dumps({"kind": "meta"}) + "\n", encoding="utf-8")
    with pytest.raises(Invalid, match="no HTTP exchanges"):
        replay_app(empty)


def test_replay_rejects_a_missing_recording(tmp_path):
    from rigsmith.core.errors import NotFound

    with pytest.raises(NotFound):
        replay_app(tmp_path / "absent.jsonl")

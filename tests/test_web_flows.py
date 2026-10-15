"""The page's newer flows, end to end over HTTP.

Each test walks one panel's path the way the browser does - upload, start a
job, poll it, act on the result - on the simulated bench, so a renamed field
or a route that forgot a path check fails here rather than in front of
someone with a guitar in their hands.
"""

from __future__ import annotations

import io
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the UI is optional; pip install 'rigsmith[ui]'")
pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

from starlette.testclient import TestClient  # noqa: E402

from rigsmith.match.render_offline import save_audio  # noqa: E402
from rigsmith.web.server import build_app  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_song import song, with_drums  # noqa: E402

RATE = 48000


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("RIGSMITH_MODE", "local")  # nothing in a test goes online
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)

    from rigsmith import config
    from rigsmith.core import drafts

    config.set_settings(config.Settings())
    drafts.set_store(drafts.DraftStore(tmp_path / "ws" / "drafts"))
    with TestClient(build_app()) as test_client:
        yield test_client
    config.set_settings(None)
    drafts.set_store(None)


def finish(client, job_id, timeout=120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            assert job["state"] == "succeeded", job
            return job["result"]
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish")


def upload(client, path: Path) -> str:
    with open(path, "rb") as handle:
        response = client.post("/api/upload", files={"file": (path.name, io.BytesIO(handle.read()), "audio/wav")})
    assert response.status_code == 200, response.text
    return response.json()["path"]


def test_settings_switch_modes_and_report_an_absent_server(client):
    assert client.get("/api/settings").json()["mode"] == "local"
    changed = client.patch("/api/settings", json={"local_llm": {"base_url": "http://127.0.0.1:9/v1"}, "default_profile": ""}).json()
    assert changed["local_llm"]["base_url"].endswith(":9/v1") and changed["default_profile"] is None
    models = client.get("/api/settings/models").json()
    assert models["reachable"] is False and models["models"] == []
    status = client.get("/api/chat/providers").json()
    assert status["local"]["reachable"] is False


def test_an_upload_refuses_what_is_not_audio(client, tmp_path):
    response = client.post("/api/upload", files={"file": ("evil.py", io.BytesIO(b"print(1)"), "text/plain")})
    assert response.status_code == 400
    wav = save_audio(tmp_path / "riff.wav", song(seconds_per_section=4.0)[0][: RATE * 2], RATE)
    first, second = upload(client, wav), upload(client, wav)
    assert first != second and Path(second).name == "riff-2.wav"


def test_a_guitar_is_learned_and_played_from_the_page(client):
    learned = client.post("/api/learn/demo", params={"protocol_id": "quick", "into": "demo"}).json()
    assert learned["report"]["samples_added"] > 0
    listed = client.get("/api/learn/profiles").json()["profiles"]
    assert [p["profile_id"] for p in listed] == ["demo"]
    played = client.post("/api/learn/profiles/demo/play", json={"notes": "@140 E2 G2 (A2+E3):4 x2", "label": "riff"}).json()
    assert played["url"].startswith("/api/audio")
    assert client.get("/api/learn/protocols").json()["protocols"]

    # The guitar's own pickup, and the same riff as if on a Strat.
    known = client.get("/api/learn/pickups").json()
    assert {t["name"] for t in known["types"]} >= {"strat", "paf", "p90"}
    assert known["guitars"][0]["profile_id"] == "demo"
    strat = client.post("/api/learn/profiles/demo/play", json={"notes": "@140 E2 G2 A2", "like": "strat", "amount": 0.5}).json()
    assert "50% of the way toward strat" in strat["pickup"]
    swapped = client.post("/api/learn/swap", json={"source": played["path"], "like": "p90", "profile_id": "demo", "file_as_take": True}).json()
    assert swapped["take_id"] and swapped["url"].startswith("/api/audio")
    assert client.post("/api/learn/swap", json={"source": played["path"], "like": "kazoo"}).status_code == 400


def test_the_loop_is_set_up_checked_and_used_on_the_bench(client, tmp_path):
    routes = client.post("/api/loop/demo").json()["routes"]
    assert {r["kind"] for r in routes} >= {"analog", "usb-reamp"}
    devices = client.get("/api/loop/devices").json()
    assert devices["hardware"] and not devices["error"]
    listed = client.get("/api/loop/routes").json()
    assert listed["default"]
    tone = client.post("/api/loop/tone").json()
    assert tone["connected"], tone

    calibration = finish(client, client.post("/api/loop/calibrate", json={}).json()["job_id"])
    assert calibration["latency_samples"] is not None

    di = save_audio(tmp_path / "di.wav", song(seconds_per_section=4.0)[0][: RATE * 3, 0], RATE)
    returned = finish(client, client.post("/api/loop/reamp", json={"di": upload(client, di), "label": "bench"}).json()["job_id"])
    assert returned["url"].startswith("/api/audio")


def test_a_song_is_mapped_chosen_and_tuned_from_the_page(client, tmp_path):
    stem, _ = song(seconds_per_section=8.0)
    mix = upload(client, save_audio(tmp_path / "mix.wav", with_drums(stem), RATE))
    isolated = upload(client, save_audio(tmp_path / "stem.wav", stem, RATE))
    made = client.post("/api/songs", json={"title": "Fixture", "artist": "Synthetic", "mix": mix, "stem": isolated}).json()
    song_id = made["song_id"]
    assert set(made["urls"]) >= {"mix", "stem"}

    mapped = finish(client, client.post(f"/api/songs/{song_id}/analyse").json()["job_id"])
    assert mapped["structure"]["sections"] and mapped["parts"]
    part = next(p for p in mapped["parts"] if p["layer"] != "stem")
    chosen = client.post(f"/api/songs/{song_id}/choose", json={"part_ids": [part["part_id"]], "only": True}).json()
    assert [p["part_id"] for p in chosen["parts"] if p["chosen"]] == [part["part_id"]]
    assert "Fixture" in client.get(f"/api/songs/{song_id}/brief").json()["brief"]

    target = client.get(f"/api/songs/{song_id}/parts/{part['part_id']}/audio").json()["path"]
    di = save_audio(tmp_path / "di.wav", stem[: RATE * 6, 0], RATE)
    run = finish(
        client,
        client.post("/api/tune", json={"target": target, "di": upload(client, di), "budget": 6, "method": "random", "knobs": ["Amp.GainA", "Amp.Treble"]}).json()["job_id"],
    )
    assert run["result"]["trials"] and run["best_url"]
    assert client.get("/api/tune/runs").json()["runs"][0]["run_id"] == run["run_id"]


def test_a_chat_and_a_dispatched_agent_run_over_http(client, monkeypatch):
    from rigsmith.agent import providers
    from test_agent import call, local, scripted

    transport, _ = scripted([call("list_songs"), {"role": "assistant", "content": "No songs yet."}, {"role": "assistant", "content": "Mapped nothing."}])
    monkeypatch.setattr(providers, "from_settings", lambda *a, **k: local(transport))

    roles = {r["name"]: r for r in client.get("/api/chat/roles").json()["roles"]}
    assert roles["assistant"]["tool_count"] > 10
    made = client.post("/api/chat", json={}).json()
    client.post(f"/api/chat/{made['chat_id']}/messages", json={"text": "what songs are there?"})
    deadline = time.time() + 10
    while client.get(f"/api/chat/{made['chat_id']}").json()["busy"] and time.time() < deadline:
        time.sleep(0.05)
    transcript = client.get(f"/api/chat/{made['chat_id']}").json()["transcript"]
    assert any(e.get("text") == "No songs yet." for e in transcript)

    run_id = client.post("/api/chat/agents", json={"role": "song-mapper", "task": "map nothing"}).json()["run_id"]
    while client.get(f"/api/chat/agents/{run_id}").json()["state"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert client.get(f"/api/chat/agents/{run_id}").json()["state"] == "done"
    assert client.delete(f"/api/chat/{made['chat_id']}").json()["deleted"] == made["chat_id"]


def test_autopilot_runs_from_the_page_to_a_draft(client, tmp_path):
    stem, _ = song(seconds_per_section=8.0)
    mix = upload(client, save_audio(tmp_path / "mix.wav", with_drums(stem), RATE))
    isolated = upload(client, save_audio(tmp_path / "stem.wav", stem, RATE))
    client.post("/api/learn/demo", params={"protocol_id": "quick", "into": "demo"})

    run = client.post("/api/autopilot", json={"title": "Fixture", "mix": mix, "stem": isolated, "profile_id": "demo", "research": False, "candidates": 3, "tune_budget": 6}).json()
    deadline = time.time() + 240
    while run["state"] in ("queued", "running") and time.time() < deadline:
        time.sleep(0.25)
        run = client.get(f"/api/autopilot/{run['run_id']}").json()
    assert run["state"] == "done", run["describe"]
    assert run["draft_id"] and run["results"] and all(r.get("url") for r in run["results"] if r.get("path"))
    assert run["target_url"] and run["notes"]
    assert client.get("/api/autopilot").json()["runs"][0]["run_id"] == run["run_id"]


def test_numpy_from_the_core_never_breaks_a_response():
    import numpy as np

    from rigsmith.web.plain import plain

    value = plain({"ok": np.bool_(True), "level": np.float32(-3.5), "curve": np.arange(3), "where": Path("a"), "bad": float("nan"), "tags": {"x"}})
    assert value == {"ok": True, "level": -3.5, "curve": [0, 1, 2], "where": "a", "bad": None, "tags": ["x"]}
    assert type(value["ok"]) is bool


def test_pairing_is_listed_without_its_cookie(client):
    from rigsmith.device.session import Session, save_session

    assert client.get("/api/device/sessions").json()["sessions"] == []
    save_session(Session(host="192.0.2.7", cookie="secret-cookie", server="AZ0X Web Server/0.6.2", paired_at=1_700_000_000))
    listed = client.get("/api/device/sessions")
    assert "secret-cookie" not in listed.text
    assert listed.json()["sessions"][0]["host"] == "192.0.2.7"
    assert client.post("/api/device/forget", json={}).status_code == 400
    assert client.post("/api/device/forget", json={"host": "192.0.2.7"}).json()["forgotten"] is True
    assert client.get("/api/device/sessions").json()["sessions"] == []

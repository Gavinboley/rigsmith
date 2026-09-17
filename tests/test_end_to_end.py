"""One run through everything, the way a person would use it.

The unit tests prove the parts. This proves they still compose: research a
tone with its sources, compile it into a rig, refuse to write it without a
backup, write it once that is waived, measure a candidate over one passage,
find out what is still wrong with it, decide by ear, and put the verdict back
on the research it came from.

It is deliberately one long test rather than many small ones. The thing under
test is the sequence - each step consuming what the last produced - and
splitting it up would test the steps again while testing the sequence not at
all.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="the UI is optional; pip install 'rigsmith[ui]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import uvicorn  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from rigsmith import config  # noqa: E402
from rigsmith.core import drafts  # noqa: E402
from rigsmith.protocol.simulator import build_app as build_simulator  # noqa: E402
from rigsmith.web.server import build_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def simulator() -> str:
    """A stand-in device, so the write path has somewhere to write."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(build_simulator(), host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()

    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "the simulator never came up"

    yield f"127.0.0.1:{port}"

    server.should_exit = True


@pytest.fixture()
def client(tmp_path, monkeypatch, simulator):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)

    settings = config.Settings()
    settings.device_host = simulator
    config.set_settings(settings)
    drafts.set_store(drafts.DraftStore(tmp_path / "drafts"))

    with TestClient(build_app()) as test_client:
        yield test_client

    config.set_settings(None)
    drafts.set_store(None)


def tone(path: Path, kind: str, seconds: float = 3.0) -> Path:
    rate = 48000
    t = np.arange(int(seconds * rate)) / rate
    clean = np.sin(2 * np.pi * 196 * t) + 0.4 * np.sin(2 * np.pi * 392 * t)
    signal = {"dark": clean * 0.6, "bright": np.tanh(clean * 9.0)}[kind]
    path.parent.mkdir(parents=True, exist_ok=True)
    # Float, so the test measures what it wrote rather than what PCM_16 clipped.
    sf.write(str(path), signal.astype("float32"), rate, subtype="FLOAT")
    return path


def finished(client, job_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            return job
        time.sleep(0.05)
    pytest.fail(f"job {job_id} never finished")


def test_research_a_tone_build_it_write_it_and_check_it(client, tmp_path):
    # -- research ----------------------------------------------------------
    created = client.post(
        "/api/recipes",
        json={"title": "Cherub Rock", "artist": "Smashing Pumpkins", "year": 1993},
    ).json()
    assert created["recipe_id"] == "cherub-rock"

    unsourced = client.post(
        "/api/recipes/cherub-rock/gear",
        json={"role": "fuzz", "name": "Big Muff Pi", "confidence": "stated"},
    )
    assert unsourced.status_code == 400, "a documented claim needs a document"

    client.post(
        "/api/recipes/cherub-rock/gear",
        json={
            "role": "fuzz", "name": "Big Muff Pi", "category": "effect",
            "confidence": "stated", "source_title": "Siamese Dream liner notes",
        },
    )
    recipe = client.post(
        "/api/recipes/cherub-rock/gear",
        json={
            "role": "amp", "name": "82 LEAD 800 100W", "category": "amp",
            "confidence": "reported", "source_title": "Guitar World",
        },
    ).json()
    assert len(recipe["gear"]) == 2
    assert recipe["verified"] is False, "nothing has been tested yet"

    client.post(
        "/api/recipes/cherub-rock/resolve",
        json={"role": "amp", "model_id": "82-lead-800-100w"},
    )

    # -- compile -----------------------------------------------------------
    compiled = client.post("/api/recipes/cherub-rock/compile").json()
    draft_id = compiled["draft_id"]
    assert draft_id
    # Conventional chain order, not the order it was researched in.
    assert [placed["role"] for placed in compiled["placed"]] == ["fuzz", "amp"]

    draft = client.get(f"/api/drafts/{draft_id}").json()
    assert len(draft["rig"]["blocks"]) == 2

    # -- preview -----------------------------------------------------------
    preview = client.post(f"/api/drafts/{draft_id}/write").json()
    assert preview["written"] is False
    assert preview["diff_rendered"], "a preview has to show what it is previewing"
    assert preview["validation"]["estimated_cpu"] > 0

    # -- the backup gate ---------------------------------------------------
    gated = client.post(
        f"/api/drafts/{draft_id}/write", params={"confirm": True}
    ).json()
    job = finished(client, gated["job_id"])
    assert job["state"] == "succeeded", job.get("error")
    assert job["result"]["written"] is False
    assert "Refusing to write without a backup" in job["result"]["message"]

    # -- and the write, once it is waived deliberately ---------------------
    waived = client.post(
        f"/api/drafts/{draft_id}/write",
        params={"confirm": True, "allow_without_backup": True},
    ).json()
    result = finished(client, waived["job_id"])["result"]

    assert result["written"] is True, result["message"]
    assert "NO BACKUP WAS TAKEN" in result["message"]
    # The map is invented, and every surface has to say so.
    assert any("UNVERIFIED" in note for note in result["report"]["caveats"])

    # -- measure -----------------------------------------------------------
    media = tmp_path / "media"
    target = tone(media / "stem.wav", "bright")
    near = tone(media / "near.wav", "bright")
    far = tone(media / "far.wav", "dark")

    scored = client.post(
        "/api/match/score",
        json={
            "target_path": str(target),
            "candidate_paths": {"near": str(near), "far": str(far)},
            "target_region": [0.5, 2.5],
            "candidate_region": [0.5, 2.5],
        },
    ).json()
    assert [entry["label"] for entry in scored["ranking"]][0] == "near"
    assert all(0.0 <= entry["composite"] <= 1.0 for entry in scored["ranking"])

    bands = client.post(
        "/api/match/bands",
        json={"target_path": str(target), "candidate_path": str(far)},
    ).json()
    assert any(band["audible"] for band in bands["bands"]), bands["rendered"]

    # -- listen ------------------------------------------------------------
    session = client.post(
        "/api/ab",
        json={
            "session_id": "e2e",
            "target_label": "the record",
            "candidates": {"near": str(near), "far": str(far)},
        },
    ).json()
    assert session["leaks"] == [], "every clip should have been blinded"

    trial = session["trial"]
    payload = json.dumps(trial)
    assert "near" not in payload and "far.wav" not in payload
    for side in ("left_audio", "right_audio"):
        assert client.get(trial[side]).status_code == 200

    client.post(
        "/api/ab/e2e/verdict",
        json={"trial_id": trial["trial_id"], "verdict": "left"},
    )
    standings = client.get("/api/ab/e2e/results").json()["standings"]
    assert standings

    # -- and back to the research it came from -----------------------------
    client.post(
        "/api/recipes/cherub-rock/measurements",
        json={
            "label": standings[0]["label"],
            "target": "stem.wav",
            "kind": "listen",
            "won": True,
            "trials": 1,
        },
    )
    final = client.get("/api/recipes/cherub-rock").json()
    assert final["verified"] is True
    assert "blind test: chosen" in final["rendered"]

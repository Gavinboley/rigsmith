"""Tests for the web interface.

These drive the same HTTP endpoints the browser does, so the UI and its
server cannot drift apart silently. The static-asset test in particular
guards against a page that references a file which was renamed or never
vendored - which fails in a way that is invisible until someone opens it.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi", reason="the UI is optional; pip install 'rigsmith[ui]'")

from starlette.testclient import TestClient  # noqa: E402

from rigsmith.web.server import build_app, static_dir  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)

    from rigsmith import config
    from rigsmith.core import drafts

    config.set_settings(config.Settings())
    drafts.set_store(drafts.DraftStore(tmp_path / "drafts"))
    with TestClient(build_app()) as test_client:
        yield test_client
    config.set_settings(None)
    drafts.set_store(None)


# --------------------------------------------------------------------------
# serving
# --------------------------------------------------------------------------


def test_index_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "rigsmith" in response.text


def test_every_asset_the_page_references_resolves(client):
    """A missing asset fails silently in the browser until someone looks."""
    page = client.get("/").text
    references = set(re.findall(r'(?:src|href)="(/static/[^"]+)"', page))
    assert references, "the page should reference its own assets"

    for reference in sorted(references):
        assert client.get(reference).status_code == 200, f"{reference} is missing"


def test_javascript_module_graph_is_complete(client):
    """Every module imported by another module must be served."""
    seen: set[str] = set()
    queue = ["/static/js/app.js"]

    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)

        response = client.get(path)
        assert response.status_code == 200, f"{path} is missing"

        for target in re.findall(r'from\s+"([^"]+)"', response.text):
            if not target.startswith("."):
                continue
            base = path.rsplit("/", 1)[0]
            resolved = base
            for part in target.split("/"):
                if part == ".":
                    continue
                if part == "..":
                    resolved = resolved.rsplit("/", 1)[0]
                else:
                    resolved = f"{resolved}/{part}"
            queue.append(resolved)

    assert len(seen) >= 6, f"expected the whole module graph, walked {seen}"


def test_vendored_assets_are_real_libraries():
    """Guards against a 404 page having been saved over a library."""
    vendor = static_dir() / "vendor"
    expected = {
        "sortable.min.js": "Sortable",
        "uplot.iife.min.js": "uPlot",
        "wavesurfer.min.js": "WaveSurfer",
        "wavesurfer.regions.min.js": "Regions",
    }
    for name, global_name in expected.items():
        path = vendor / name
        assert path.is_file(), f"{name} is not vendored"
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "<html" not in text[:200].lower(), f"{name} looks like an error page"
        assert global_name in text, f"{name} does not define {global_name}"


def test_the_javascript_parses(client):
    """The module-graph test proves the files resolve, not that they run.

    A syntax error in a panel is otherwise invisible until someone opens that
    tab in a browser, which no test does.
    """
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed; this check is advisory")

    sources = sorted(static_dir().glob("js/**/*.js"))
    assert len(sources) >= 8, f"expected the whole front end, found {sources}"
    for source in sources:
        result = subprocess.run(
            [node, "--check", str(source)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{source.name} does not parse: {result.stderr}"


# --------------------------------------------------------------------------
# api
# --------------------------------------------------------------------------


def test_health_and_capabilities(client):
    health = client.get("/api/health").json()
    assert "version" in health and "workspace" in health

    caps = client.get("/api/capabilities").json()
    assert "catalog" in caps and "tone3000" in caps
    assert set(caps["catalog"]) >= {"source", "models", "verified"}


def test_build_a_rig_through_the_api(client):
    created = client.post("/api/drafts", json={"name": "Cherub Rock"}).json()
    draft_id = created["draft_id"]
    assert created["free_slots"][0] == 1

    for category, model in [
        ("effect", "Big Muff"),
        ("nam", "JMP-1959"),
        ("ir", "1960A"),
        ("effect", "Delay"),
    ]:
        payload = client.post(
            f"/api/drafts/{draft_id}/blocks",
            json={"category": category, "model_id": model},
        ).json()

    assert payload["chain"] == "Big Muff -> NAM(JMP-1959) -> IR(1960A) -> Delay"
    assert payload["validation"]["estimated_cpu"] > 0


def test_move_block_through_the_api_preserves_scene_targets(client):
    draft_id = client.post("/api/drafts", json={"name": "M"}).json()["draft_id"]
    for model in ("muff", "amp", "ir", "delay"):
        client.post(
            f"/api/drafts/{draft_id}/blocks",
            json={"category": "effect", "model_id": model},
        )
    client.post(
        f"/api/drafts/{draft_id}/scenes",
        json={"name": "V", "off_slots": [1], "on_slots": [4]},
    )

    payload = client.post(
        f"/api/drafts/{draft_id}/blocks/4/move", json={"to_slot": 1}
    ).json()

    slots = {b["model_id"]: b["slot"] for b in payload["rig"]["blocks"]}
    states = payload["rig"]["scenes"][0]["states"]
    assert states[str(slots["muff"])] == "off"
    assert states[str(slots["delay"])] == "on"


def test_scene_round_trip(client):
    draft_id = client.post("/api/drafts", json={"name": "S"}).json()["draft_id"]
    for index in range(4):
        client.post(
            f"/api/drafts/{draft_id}/blocks",
            json={"category": "effect", "model_id": f"p{index}"},
        )

    payload = client.post(
        f"/api/drafts/{draft_id}/scenes",
        json={"name": "Lead", "on_slots": [1, 2], "footswitch": 3},
    ).json()
    scene = payload["rig"]["scenes"][0]
    assert scene["footswitch"] == 3
    assert scene["states"]["1"] == "on"

    client.delete(f"/api/drafts/{draft_id}/scenes/Lead")
    assert client.get(f"/api/drafts/{draft_id}").json()["rig"]["scenes"] == []


def test_duplicate_and_delete_through_the_api(client):
    draft_id = client.post("/api/drafts", json={"name": "Original"}).json()["draft_id"]
    copy = client.post(f"/api/drafts/{draft_id}/duplicate").json()
    assert copy["draft_id"] != draft_id
    assert len(client.get("/api/drafts").json()["drafts"]) == 2

    assert client.delete(f"/api/drafts/{copy['draft_id']}").status_code == 200
    assert len(client.get("/api/drafts").json()["drafts"]) == 1


def test_preview_reports_changes(client):
    draft_id = client.post("/api/drafts", json={"name": "P"}).json()["draft_id"]
    client.post(
        f"/api/drafts/{draft_id}/blocks", json={"category": "nam", "model_id": "Amp"}
    )
    preview = client.get(f"/api/drafts/{draft_id}/preview").json()
    assert preview["changes"]
    assert "add" in preview["summary"]


# --------------------------------------------------------------------------
# error mapping
# --------------------------------------------------------------------------


def test_missing_draft_is_404_with_a_hint(client):
    client.post("/api/drafts", json={"name": "Real"})
    response = client.get("/api/drafts/ghost")
    assert response.status_code == 404
    body = response.json()
    assert "ghost" in body["error"]
    assert "real" in (body.get("hint") or body["error"]).lower()


def test_invalid_category_is_400(client):
    draft_id = client.post("/api/drafts", json={"name": "B"}).json()["draft_id"]
    response = client.post(
        f"/api/drafts/{draft_id}/blocks",
        json={"category": "nonsense", "model_id": "x"},
    )
    assert response.status_code == 400
    assert "nonsense" in response.json()["error"]


def test_occupied_slot_is_409(client):
    draft_id = client.post("/api/drafts", json={"name": "C"}).json()["draft_id"]
    client.post(
        f"/api/drafts/{draft_id}/blocks",
        json={"category": "effect", "model_id": "a", "slot": 1},
    )
    response = client.post(
        f"/api/drafts/{draft_id}/blocks",
        json={"category": "effect", "model_id": "b", "slot": 1},
    )
    assert response.status_code == 409


def test_readonly_mode_is_403(client, monkeypatch):
    draft_id = client.post("/api/drafts", json={"name": "R"}).json()["draft_id"]

    from rigsmith import config

    monkeypatch.setenv("RIGSMITH_READONLY", "1")
    config.set_settings(config.Settings())

    response = client.post(f"/api/drafts/{draft_id}/export")
    assert response.status_code == 403


def test_audio_outside_the_workspace_is_refused(client):
    response = client.get("/api/audio", params={"path": "C:/Windows/win.ini"})
    assert response.status_code == 403


def test_audio_inside_the_workspace_is_served(client, tmp_path):
    audio = tmp_path / "audio"
    audio.mkdir(exist_ok=True)
    target = audio / "take.wav"
    target.write_bytes(b"RIFF....WAVE")

    response = client.get("/api/audio", params={"path": str(target)})
    assert response.status_code == 200


# --------------------------------------------------------------------------
# live socket
# --------------------------------------------------------------------------


def test_websocket_sends_job_state(client):
    with client.websocket_connect("/ws") as socket:
        message = socket.receive_json()
        assert message["type"] == "jobs"
        assert isinstance(message["jobs"], list)


def test_websocket_reports_a_running_job(client):
    from rigsmith.core.jobs import get_manager

    def slow(progress):
        progress.update(0.5, "halfway")
        return "done"

    get_manager().submit("test job", slow)

    with client.websocket_connect("/ws") as socket:
        for _ in range(5):
            message = socket.receive_json()
            if message["type"] == "jobs" and message["jobs"]:
                assert message["jobs"][0]["name"] == "test job"
                return
    pytest.fail("the socket never reported the job")


# --------------------------------------------------------------------------
# recipes
# --------------------------------------------------------------------------


def test_recipe_round_trip_through_the_api(client):
    created = client.post(
        "/api/recipes",
        json={"title": "Cherub Rock", "artist": "Smashing Pumpkins"},
    ).json()
    assert created["recipe_id"] == "cherub-rock"

    listed = client.get("/api/recipes").json()["recipes"]
    assert listed[0]["title"] == "Cherub Rock"

    fetched = client.get("/api/recipes/cherub-rock").json()
    assert fetched["artist"] == "Smashing Pumpkins"


def test_gear_confidence_is_enforced_through_the_api(client):
    client.post("/api/recipes", json={"title": "T"})

    refused = client.post(
        "/api/recipes/t/gear",
        json={"role": "amp", "name": "JMP", "confidence": "stated"},
    )
    assert refused.status_code == 400
    assert "no source" in refused.json()["error"]

    accepted = client.post(
        "/api/recipes/t/gear",
        json={
            "role": "amp",
            "name": "JMP",
            "confidence": "stated",
            "source_title": "Liner notes",
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["confidence"] == 1.0


def test_unsupported_gear_is_reported_to_the_ui(client):
    """The panel marks these, so the API has to surface them."""
    client.post("/api/recipes", json={"title": "T"})
    payload = client.post(
        "/api/recipes/t/gear", json={"role": "cab", "name": "4x12"}
    ).json()
    assert payload["unsupported"] == ["cab"]


def test_compile_returns_the_placed_blocks(client):
    client.post("/api/recipes", json={"title": "T"})
    client.post(
        "/api/recipes/t/gear",
        json={"role": "delay", "name": "Delay", "category": "effect"},
    )
    client.post(
        "/api/recipes/t/gear",
        json={"role": "amp", "name": "JMP", "category": "nam"},
    )

    result = client.post("/api/recipes/t/compile").json()
    # Conventional chain order, not the order they were researched in.
    assert [p["role"] for p in result["placed"]] == ["amp", "delay"]


def test_missing_recipe_is_404(client):
    assert client.get("/api/recipes/ghost").status_code == 404


def test_recipe_delete(client):
    client.post("/api/recipes", json={"title": "Doomed"})
    assert client.delete("/api/recipes/doomed").status_code == 200
    assert client.get("/api/recipes").json()["recipes"] == []


# --------------------------------------------------------------------------
# waveforms and regions
# --------------------------------------------------------------------------


@pytest.fixture()
def outside_file(tmp_path_factory):
    """An audio file somewhere the workspace guard would normally refuse."""
    sf = pytest.importorskip("soundfile")
    import numpy as np

    elsewhere = tmp_path_factory.mktemp("music")
    path = elsewhere / "a take.wav"
    tone = np.sin(2 * np.pi * 196 * np.arange(48000 * 3) / 48000)
    sf.write(str(path), tone.astype("float32"), 48000)
    return path


def test_opening_a_file_is_what_makes_it_servable(client, outside_file):
    """The path guard stays shut until the user names a file themselves."""
    assert client.get("/api/audio", params={"path": str(outside_file)}).status_code == 403

    opened = client.post("/api/match/open", json={"path": str(outside_file)})
    assert opened.status_code == 200
    body = opened.json()
    assert body["duration"] == pytest.approx(3.0, abs=0.01)
    assert body["sample_rate"] == 48000
    assert "%20" in body["url"], "a path with a space has to survive the round trip"

    assert client.get("/api/audio", params={"path": str(outside_file)}).status_code == 200


def test_opening_a_file_that_is_not_there_is_404(client, tmp_path):
    response = client.post("/api/match/open", json={"path": str(tmp_path / "ghost.wav")})
    assert response.status_code == 404


def test_spectrum_honours_the_region(client, outside_file):
    whole = client.get("/api/match/spectrum", params={"path": str(outside_file)}).json()
    part = client.get(
        "/api/match/spectrum",
        params={"path": str(outside_file), "start": 0.5, "end": 2.0},
    ).json()
    assert whole["region"] is None
    assert part["region"] == [0.5, 2.0]
    assert len(part["freqs"]) == len(whole["freqs"])


def test_a_region_too_short_to_measure_is_400(client, outside_file):
    response = client.get(
        "/api/match/spectrum",
        params={"path": str(outside_file), "start": 0.5, "end": 0.6},
    )
    assert response.status_code == 400
    assert "too short" in response.json()["error"]


def test_scoring_accepts_regions_and_reports_them_back(client, outside_file):
    response = client.post(
        "/api/match/score",
        json={
            "target_path": str(outside_file),
            "candidate_paths": {"same": str(outside_file)},
            "target_region": [0.5, 2.0],
            "candidate_region": [0.5, 2.0],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["target_region"] == [0.5, 2.0]
    assert body["ranking"][0]["label"] == "same"


def test_aligned_mode_refuses_mismatched_regions_through_the_api(client, outside_file):
    response = client.post(
        "/api/match/score",
        json={
            "target_path": str(outside_file),
            "candidate_paths": {"same": str(outside_file)},
            "mode": "aligned",
            "target_region": [0.0, 1.0],
            "candidate_region": [1.0, 2.0],
        },
    )
    assert response.status_code == 400
    assert "same region on both sides" in response.json()["error"]


def test_scoring_a_file_also_makes_it_servable(client, outside_file):
    """So the winner can be drawn without a second round trip to open it."""
    client.post(
        "/api/match/score",
        json={
            "target_path": str(outside_file),
            "candidate_paths": {"same": str(outside_file)},
        },
    )
    assert client.get("/api/audio", params={"path": str(outside_file)}).status_code == 200

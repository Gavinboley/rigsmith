"""Tests for the web interface.

These drive the same HTTP endpoints the browser does, so the UI and its
server cannot drift apart silently. The static-asset test in particular
guards against a page that references a file which was renamed or never
vendored - which fails in a way that is invisible until someone opens it.
"""

from __future__ import annotations

import re
import time

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
    client.post("/api/match/open", json={"path": str(outside_file)})
    whole = client.get("/api/match/spectrum", params={"path": str(outside_file)}).json()
    part = client.get(
        "/api/match/spectrum",
        params={"path": str(outside_file), "start": 0.5, "end": 2.0},
    ).json()
    assert whole["region"] is None
    assert part["region"] == [0.5, 2.0]
    assert len(part["freqs"]) == len(whole["freqs"])


def test_a_region_too_short_to_measure_is_400(client, outside_file):
    client.post("/api/match/open", json={"path": str(outside_file)})
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


# --------------------------------------------------------------------------
# the render layer, which the UI previously could not reach at all
# --------------------------------------------------------------------------


def test_offline_search_starts_a_job_rather_than_blocking(client, tmp_path):
    """Rendering twenty captures is minutes; the page must not hang on it."""
    pytest.importorskip("soundfile")
    import numpy as np
    import soundfile as sf

    di = tmp_path / "di.wav"
    sf.write(str(di), np.zeros(4800, dtype="float32"), 48000)

    response = client.post(
        "/api/match/search",
        json={
            "di_path": str(di),
            "target_path": str(di),
            "captures": {"nothing": str(tmp_path / "absent.nam")},
        },
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            break
        time.sleep(0.05)
    else:  # pragma: no cover - only on a wedged machine
        pytest.fail("the job never finished")

    # Every candidate failed, which is a failed job and not a crashed server.
    assert job["state"] == "failed"
    assert "absent.nam" in (job["error"] or "") or "no candidate" in (job["error"] or "")


def test_a_job_result_that_is_already_json_is_not_stringified(client):
    """A Python repr in `result` is unparseable by the page that asked for it."""
    from rigsmith.core.jobs import get_manager

    job = get_manager().submit("shaped", lambda progress: {"ranking": [1, 2]})
    for _ in range(100):
        if job.finished:
            break
        time.sleep(0.02)

    payload = client.get(f"/api/jobs/{job.id}").json()
    assert payload["result"] == {"ranking": [1, 2]}


def test_isolate_starts_a_job(client, tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"RIFF....WAVE")
    response = client.post("/api/match/isolate", json={"audio_path": str(audio)})
    assert response.status_code == 200
    assert response.json()["job_id"]


def test_render_without_anything_to_render_through_is_400(client, tmp_path):
    di = tmp_path / "di.wav"
    di.write_bytes(b"RIFF....WAVE")
    response = client.post("/api/match/render", json={"di_path": str(di)})
    assert response.status_code == 400
    assert "nothing to render through" in response.json()["error"]


def test_an_ab_session_can_be_started_from_the_page(client, tmp_path):
    """Score then listen was previously a CLI-only handoff."""
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    for path in (first, second):
        path.write_bytes(b"RIFF....WAVE")

    created = client.post(
        "/api/ab",
        json={
            "session_id": "from-the-page",
            "target_label": "the record",
            "candidates": {"a": str(first), "b": str(second)},
        },
    )
    assert created.status_code == 200

    session = client.get("/api/ab/from-the-page").json()
    assert session["trial"], "a fresh session should offer a trial"
    # The clips have to be reachable, or the player has nothing to play.
    for side in ("left_audio", "right_audio"):
        assert client.get(session["trial"][side]).status_code == 200

    # And nothing in what the page receives may say which is which. A URL
    # carrying a path would put the answer in the DOM and in the address bar.
    payload = str(session["trial"])
    assert "a.wav" not in payload and "b.wav" not in payload, payload
    assert str(first.parent) not in payload
    assert session["trial"]["left_audio"].startswith("/api/ab/from-the-page/audio/")


def test_a_blinded_clip_url_that_does_not_exist_is_404(client, tmp_path):
    first, second = tmp_path / "a.wav", tmp_path / "b.wav"
    for path in (first, second):
        path.write_bytes(b"RIFF....WAVE")
    client.post(
        "/api/ab",
        json={
            "session_id": "s",
            "target_label": "t",
            "candidates": {"a": str(first), "b": str(second)},
        },
    )
    assert client.get("/api/ab/s/audio/nonsense").status_code == 404


def test_tone3000_login_is_reachable_from_the_page(client):
    """Sign-in used to be a CLI-only step in an otherwise complete panel."""
    response = client.post("/api/tone3000/login")
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    # Without a client id it fails fast and says what to set - it must not sit
    # there holding a connection open waiting on a browser that never opened.
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            break
        time.sleep(0.05)
    assert job["finished"]
    if job["state"] == "failed":
        assert "client id" in (job["error"] or "").lower()


# --------------------------------------------------------------------------
# picking paths
# --------------------------------------------------------------------------


def test_browse_lists_a_directory(client, tmp_path):
    (tmp_path / "songs").mkdir()
    (tmp_path / "riff.wav").write_bytes(b"RIFF")
    (tmp_path / "notes.txt").write_text("not audio", encoding="utf-8")
    (tmp_path / ".hidden").write_bytes(b"")

    payload = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    assert [d["name"] for d in payload["directories"]] == ["songs"]
    # Only files of the asked-for kind, and nothing hidden.
    assert [f["name"] for f in payload["files"]] == ["riff.wav"]
    assert payload["parent"]
    assert payload["roots"], "the picker needs somewhere to start"
    # Named, not shown as raw paths - a row of absolute paths is unreadable.
    assert {"name", "path"} == set(payload["roots"][0])
    assert payload["roots"][0]["name"] == "workspace"


def test_browse_filters_by_kind(client, tmp_path):
    (tmp_path / "riff.wav").write_bytes(b"RIFF")
    (tmp_path / "amp.nam").write_text("{}", encoding="utf-8")

    audio = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    captures = client.get(
        "/api/browse", params={"path": str(tmp_path), "kind": "capture"}
    ).json()
    everything = client.get(
        "/api/browse", params={"path": str(tmp_path), "kind": "any"}
    ).json()

    assert [f["name"] for f in audio["files"]] == ["riff.wav"]
    assert [f["name"] for f in captures["files"]] == ["amp.nam"]
    assert len(everything["files"]) == 2


def test_browsing_to_a_file_shows_the_folder_it_is_in(client, tmp_path):
    """So a path already in the box opens the picker somewhere useful."""
    target = tmp_path / "riff.wav"
    target.write_bytes(b"RIFF")
    payload = client.get("/api/browse", params={"path": str(target)}).json()
    assert payload["path"] == str(tmp_path.resolve())


def test_browse_does_not_return_file_contents(client, tmp_path):
    """Listing a name is not the same as being allowed to read the file."""
    secret = tmp_path / "riff.wav"
    secret.write_bytes(b"RIFF-the-contents")
    payload = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    assert "the-contents" not in str(payload)
    assert set(payload["files"][0]) == {"name", "path", "size"}


def test_browse_rejects_an_unknown_kind(client, tmp_path):
    response = client.get(
        "/api/browse", params={"path": str(tmp_path), "kind": "nonsense"}
    )
    assert response.status_code == 400
    assert "audio, capture, ir, any" in response.json()["hint"]


# --------------------------------------------------------------------------
# MIDI, which the panel used to describe rather than do
# --------------------------------------------------------------------------


def test_midi_routes_exist_and_report_a_missing_backend_cleanly(client):
    """Without mido these are 503 with an install hint, not a 500."""
    calls = [
        ("/api/device/midi/select", {"program": 3}),
        ("/api/device/midi/footswitch", {"footswitch": 1}),
        ("/api/device/midi/block", {"slot": 1, "enabled": True}),
    ]
    for path, body in calls:
        response = client.post(path, json=body)
        assert response.status_code in (200, 503), f"{path} -> {response.status_code}"
        if response.status_code == 503:
            assert "midi" in str(response.json()).lower()


def test_midi_program_is_range_checked_before_any_port_is_opened(client):
    """A bad program number is the caller's mistake, not a missing device."""
    response = client.post("/api/device/midi/select", json={"program": 200})
    assert response.status_code == 400
    assert "0-127" in response.json()["error"]


def test_midi_verify_needs_a_real_draft(client):
    response = client.post(
        "/api/device/midi/verify", json={"draft_id": "ghost"}
    )
    assert response.status_code in (404, 503)


# --------------------------------------------------------------------------
# downloading captures - the step between finding one and using one
# --------------------------------------------------------------------------


def test_download_starts_a_job_and_reports_a_missing_login(client):
    response = client.post("/api/tone3000/download", json={"tone_id": "123"})
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            break
        time.sleep(0.05)
    assert job["finished"]
    # No credentials here, so it fails with the reason rather than hanging.
    assert job["state"] == "failed"
    assert "tone3000" in (job["error"] or "").lower()


def test_the_nam_directory_is_protected_from_pruning(client):
    """A downloaded capture is what a rig points at, and re-fetching it needs
    the account that got it."""
    areas = {area["name"]: area for area in client.get("/api/workspace").json()["areas"]}
    assert "nam" in areas
    assert areas["nam"]["prunable"] is False


def test_measurements_reach_the_recipe_through_the_api(client):
    client.post("/api/recipes", json={"title": "Cherub Rock"})

    scored = client.post(
        "/api/recipes/cherub-rock/measurements",
        json={
            "label": "jcm800-hot",
            "target": "stem.wav",
            "composite": 0.0312,
            "mode": "distribution",
            "region": "41.20s-58.00s",
            "won": True,
        },
    )
    assert scored.status_code == 200
    # A metric win is a ranking, not a decision.
    assert scored.json()["verified"] is False

    listened = client.post(
        "/api/recipes/cherub-rock/measurements",
        json={"label": "jcm800-hot", "target": "stem.wav", "kind": "listen",
              "won": True, "trials": 3},
    )
    assert listened.json()["verified"] is True
    assert len(listened.json()["measurements"]) == 2

    listing = client.get("/api/recipes").json()["recipes"][0]
    assert listing["measurements"] == 2
    assert listing["verified"] is True


def test_a_score_measurement_without_a_score_is_400(client):
    client.post("/api/recipes", json={"title": "T"})
    response = client.post(
        "/api/recipes/t/measurements", json={"label": "a", "target": "b"}
    )
    assert response.status_code == 400
    assert "composite" in response.json()["error"]


def test_band_comparison_through_the_api(client, outside_file):
    response = client.post(
        "/api/match/bands",
        json={
            "target_path": str(outside_file),
            "candidate_path": str(outside_file),
            "target_region": [0.5, 2.0],
            "candidate_region": [0.5, 2.0],
        },
    )
    assert response.status_code == 200
    body = response.json()

    names = [band["name"] for band in body["bands"]]
    assert "presence" in names and "fizz" in names
    # A file against itself differs nowhere.
    assert not any(band["audible"] for band in body["bands"])
    assert "within 1.5 dB" in body["rendered"]


def test_band_comparison_makes_its_files_servable(client, outside_file):
    """So the waveform beside the bands can be drawn without asking again."""
    client.post(
        "/api/match/bands",
        json={"target_path": str(outside_file), "candidate_path": str(outside_file)},
    )
    assert client.get("/api/audio", params={"path": str(outside_file)}).status_code == 200


# --------------------------------------------------------------------------
# writing, which is the one irreversible thing
# --------------------------------------------------------------------------


def test_previewing_a_write_is_synchronous_and_shows_the_diff(client):
    draft_id = client.post("/api/drafts", json={"name": "W"}).json()["draft_id"]
    client.post(
        f"/api/drafts/{draft_id}/blocks", json={"category": "nam", "model_id": "JMP"}
    )

    preview = client.post(f"/api/drafts/{draft_id}/write").json()
    assert preview["written"] is False
    assert "job_id" not in preview, "a preview touches nothing; it need not be a job"
    # The page has to be able to show what it is asking about.
    assert preview["diff_rendered"]
    assert preview["validation"]["estimated_cpu"] > 0
    assert preview["validation"]["ok"] is True


def test_a_confirmed_write_is_a_job(client):
    """Finding the device can take ten seconds; a page that stops for that
    long looks broken rather than busy."""
    draft_id = client.post("/api/drafts", json={"name": "W"}).json()["draft_id"]
    client.post(
        f"/api/drafts/{draft_id}/blocks", json={"category": "nam", "model_id": "JMP"}
    )

    started = client.post(
        f"/api/drafts/{draft_id}/write", params={"confirm": True}
    ).json()
    assert started["job_id"]

    from rigsmith.core.jobs import get_manager

    get_manager().cancel(started["job_id"])


def test_a_write_preview_on_a_missing_draft_is_404(client):
    assert client.post("/api/drafts/ghost/write").status_code == 404


# --------------------------------------------------------------------------
# takes - the user's own recordings
# --------------------------------------------------------------------------


def test_a_take_round_trips_through_the_api(client, outside_file):
    added = client.post(
        "/api/takes",
        json={
            "path": str(outside_file),
            "kind": "di",
            "guitar": "Les Paul Standard",
            "pickup": "bridge",
        },
    ).json()
    assert added["guitar"] == "Les Paul Standard"
    assert added["reamps"] is True

    listing = client.get("/api/takes").json()
    assert listing["guitars"] == ["Les Paul Standard"]
    assert "di" in listing["kinds"], "the page needs the vocabulary too"

    take_id = added["take_id"]
    fetched = client.get(f"/api/takes/{take_id}").json()
    assert fetched["url"], "the page has to be able to play it"
    assert client.get(fetched["url"]).status_code == 200


def test_a_takes_labels_can_be_corrected_through_the_api(client, outside_file):
    added = client.post("/api/takes", json={"path": str(outside_file)}).json()
    assert added["guitar"] is None

    updated = client.patch(
        f"/api/takes/{added['take_id']}", json={"guitar": "Telecaster"}
    ).json()
    assert updated["guitar"] == "Telecaster"


def test_an_unreadable_take_is_400_not_500(client, tmp_path):
    broken = tmp_path / "broken.wav"
    broken.write_bytes(b"not audio")

    response = client.post("/api/takes", json={"path": str(broken)})
    assert response.status_code == 400
    assert "broken.wav" in response.json()["error"]


def test_a_missing_take_is_404(client):
    assert client.get("/api/takes/ghost").status_code == 404


def test_device_audio_reports_a_missing_drive_rather_than_failing(client):
    """Looking is a question, not an action, so it answers rather than 503s."""
    payload = client.get("/api/device/audio").json()
    assert payload["files"] == []
    if payload["error"]:
        assert "HeadRush" in payload["error"] or "storage" in payload["error"].lower()


def test_the_picker_only_offers_what_this_install_can_read(client, tmp_path):
    (tmp_path / "riff.wav").write_bytes(b"RIFF")
    (tmp_path / "voice.m4a").write_bytes(b"\x00")
    (tmp_path / "notes.docx").write_bytes(b"\x00")

    payload = client.get("/api/browse", params={"path": str(tmp_path)}).json()
    names = [entry["name"] for entry in payload["files"]]
    assert "riff.wav" in names
    assert "notes.docx" not in names, "offering a file that cannot be opened is a trap"
    from rigsmith.match.source import _have_av

    assert ("voice.m4a" in names) == _have_av(), "m4a is readable exactly when FFmpeg (PyAV) is installed"


def test_recording_a_take_is_a_job_that_reports_a_missing_interface(client):
    """It waits on somebody playing, so it cannot be a request - and with no
    audio interface it has to fail with a reason rather than hang."""
    response = client.post("/api/takes/record", json={"duration_s": 1.0})
    assert response.status_code == 200
    job_id = response.json()["job_id"]

    for _ in range(200):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["finished"]:
            break
        time.sleep(0.05)
    assert job["finished"]
    if job["state"] == "failed":
        assert job["error"], "a failure has to say what went wrong"


def test_the_rig_tempo_is_set_and_cleared_through_the_api(client):
    draft = client.post("/api/drafts", json={"name": "Tempo"}).json()
    set_ = client.patch(f"/api/drafts/{draft['draft_id']}", json={"tempo": 132.5}).json()
    assert set_["rig"]["tempo"] == 132.5
    assert client.patch(f"/api/drafts/{draft['draft_id']}", json={"tempo": 500}).status_code == 400
    cleared = client.patch(f"/api/drafts/{draft['draft_id']}", json={"tempo": 0}).json()
    assert "tempo" not in cleared["rig"], "0 means follow the device's tempo"


def test_a_songs_tempo_is_corrected_through_the_api(client):
    created = client.post("/api/songs", json={"title": "Tempo"}).json()
    assert client.post(f"/api/songs/{created['song_id']}/tempo", json={"factor": 2}).status_code == 400, "nothing to double yet"
    set_ = client.post(f"/api/songs/{created['song_id']}/tempo", json={"bpm": 88}).json()
    assert set_["structure"]["tempo"] == 88 and set_["structure"]["tempo_source"] == "hand"
    halved = client.post(f"/api/songs/{created['song_id']}/tempo", json={"factor": 0.5}).json()
    assert halved["structure"]["tempo"] == 44


def test_a_drafts_tone_sheet_is_text_and_a_printable_page(client):
    draft = client.post("/api/drafts", json={"name": "Sheet"}).json()
    text = client.get(f"/api/drafts/{draft['draft_id']}/sheet").json()
    assert text["title"] == "Sheet" and text["markdown"].startswith("# Sheet")
    page = client.get(f"/api/drafts/{draft['draft_id']}/sheet.html")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "<h1>Sheet</h1>" in page.text
    assert client.get("/api/drafts/ghost/sheet.html").status_code == 404


def test_a_drafts_blocks_come_with_their_readings(client):
    draft = client.post("/api/drafts", json={"name": "Readings"}).json()
    added = client.post(f"/api/drafts/{draft['draft_id']}/blocks", json={"category": "amp", "model_id": "Some Amp", "params": {"GainA": 0.62, "Type": 3}}).json()
    rows = added["readings"]["1"]
    assert ["Gain", "~62% of travel"] in rows, rows
    assert not any(label == "Type" for label, _ in rows), "the model picker is not a control"


def test_a_parts_di_says_whose_playing_it_is(client, tmp_path):
    sf = pytest.importorskip("soundfile")
    import numpy as np

    from rigsmith.core import songs, takes
    from rigsmith.core.songs import Part

    made = songs.create("Whose DI")
    made.parts = [Part("verse-1-stem", 0, "verse", "stem", 0.0, 5.0), Part("chorus-2-stem", 1, "chorus", "stem", 5.0, 10.0)]
    made.save()
    for name, origin, scale in (("mine", "di-box", 0.2), ("synth", "synth", 0.1)):
        path = tmp_path / f"{name}.wav"
        sf.write(str(path), (scale * np.sin(np.arange(48000) / 48000 * 2 * np.pi * 110)).astype("float32"), 48000)
        takes.add(path, take_id=name, kind="di", origin=origin, copy=False)
    made = songs.get(made.song_id)
    made.part("verse-1-stem").di_take, made.part("chorus-2-stem").di_take = "mine", "synth"
    made.save()
    parts = {p["part_id"]: p for p in client.get(f"/api/songs/{made.song_id}").json()["parts"]}
    assert parts["verse-1-stem"]["di"]["kind"] == "yours" and parts["chorus-2-stem"]["di"]["kind"] == "rendered"
    assert client.get(parts["verse-1-stem"]["di"]["url"]).status_code == 200


def test_the_api_describes_itself(client):
    """/api/docs is linked from the app; its schema was a 500."""
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/api/drafts" in paths and "/" not in paths


def test_the_page_can_learn_the_scene_numbers(client, monkeypatch):
    from rigsmith.core import appsettings, devices
    from rigsmith.core.jobs import get_manager

    async def scene_set_by_hand(host=None):
        return devices.parse_scenes({"ModeNew1": 2, "Scene1_1_Effect": "Amp", "Scene1_1_Mode": 1, "Scene1_2_Effect": "Cab", "Scene1_2_Mode": 2})

    monkeypatch.setattr(devices, "read_scenes", scene_set_by_hand)
    job = client.post("/api/device/scenes", json={"learn": True}).json()
    running = get_manager().get(job["job_id"])
    assert running.wait(10) and "Learned: on is 1, off is 2" in running.result["render"], running.error
    assert appsettings.load().scene_modes == {"on": 1, "off": 2, "no_change": 0}


def test_a_draft_is_nudged_by_ear_through_the_api(client):
    draft = client.post("/api/drafts", json={"name": "Ears"}).json()
    client.post(f"/api/drafts/{draft['draft_id']}/blocks", json={"category": "amp", "model_id": "Some Amp", "params": {"GainA": 0.5}})
    nudged = client.post(f"/api/drafts/{draft['draft_id']}/nudge", json={"how": "more gain"}).json()
    assert nudged["rig"]["blocks"][0]["params"]["GainA"] == 0.58 and "GainA 50% -> 58%" in nudged["message"]

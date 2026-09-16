"""Tests for source separation and the traffic recorder.

Demucs is not installed here (it is a large optional dependency), so the
separation tests stub the subprocess. That is the right boundary anyway: what
matters is that rigsmith invokes the six-stem model, handles a failure
legibly, and never lets a separated stem be mistaken for ground truth.

The recorder is exercised against a real in-process upstream.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from rigsmith.core.errors import Unavailable
from rigsmith.match.separate import (
    DEFAULT_MODEL,
    SIX_STEMS,
    SeparationUnavailable,
    demucs_available,
    separate,
)

# --------------------------------------------------------------------------
# separation
# --------------------------------------------------------------------------


def test_the_six_stem_model_is_the_default():
    """Four-stem models fold guitar into "other", which is useless here."""
    assert DEFAULT_MODEL == "htdemucs_6s"
    assert "guitar" in SIX_STEMS


def test_missing_audio_is_reported_before_anything_else(tmp_path):
    with pytest.raises(FileNotFoundError):
        separate(tmp_path / "absent.wav", tmp_path / "out")


def test_missing_demucs_explains_how_to_install_it(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    source.write_bytes(b"RIFF")

    monkeypatch.setattr("rigsmith.match.separate.demucs_available", lambda: False)
    with pytest.raises(SeparationUnavailable) as excinfo:
        separate(source, tmp_path / "out")

    message = str(excinfo.value)
    assert "pip install" in message
    assert "slow on CPU" in message


def test_separation_unavailable_is_a_core_error():
    """So every surface maps it to its own idiom without string-matching."""
    assert issubclass(SeparationUnavailable, Unavailable)


def stub_demucs(monkeypatch, *, returncode=0, stderr="", produce=None):
    """Replace the Demucs subprocess with something predictable."""
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if produce:
            produce()
        return subprocess.CompletedProcess(command, returncode, "", stderr)

    monkeypatch.setattr("rigsmith.match.separate.demucs_available", lambda: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


def test_invokes_the_six_stem_model(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    source.write_bytes(b"RIFF")
    calls = stub_demucs(monkeypatch)

    separate(source, tmp_path / "out", stems=None)

    assert calls, "demucs should have been invoked"
    command = calls[0]
    assert "-n" in command
    assert DEFAULT_MODEL in command
    assert str(source) in command


def test_collects_the_stems_it_produced(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    source.write_bytes(b"RIFF")
    out = tmp_path / "out"

    def produce():
        stem_dir = out / DEFAULT_MODEL / "song"
        stem_dir.mkdir(parents=True)
        for stem in ("guitar", "drums", "bass"):
            (stem_dir / f"{stem}.wav").write_bytes(b"RIFF")

    stub_demucs(monkeypatch, produce=produce)
    result = separate(source, out, stems=None)

    assert set(result.stems) == {"guitar", "drums", "bass"}
    assert result.guitar is not None
    assert result.guitar.name == "guitar.wav"


def test_a_missing_guitar_stem_is_called_out(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    source.write_bytes(b"RIFF")
    out = tmp_path / "out"

    def produce():
        stem_dir = out / DEFAULT_MODEL / "song"
        stem_dir.mkdir(parents=True)
        (stem_dir / "other.wav").write_bytes(b"RIFF")

    stub_demucs(monkeypatch, produce=produce)
    result = separate(source, out, stems=None)

    assert result.guitar is None
    assert any("no guitar stem" in w for w in result.warnings)


def test_always_warns_that_a_stem_is_not_ground_truth(tmp_path, monkeypatch):
    """The single most important thing to keep saying about a stem."""
    source = tmp_path / "song.wav"
    source.write_bytes(b"RIFF")

    stub_demucs(monkeypatch)
    result = separate(source, tmp_path / "out", stems=None)

    joined = " ".join(result.warnings)
    assert "timbral reference" in joined
    assert "artefacts" in joined


def test_a_demucs_failure_surfaces_its_output(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    source.write_bytes(b"RIFF")

    stub_demucs(monkeypatch, returncode=1, stderr="CUDA out of memory")
    with pytest.raises(SeparationUnavailable, match="CUDA out of memory"):
        separate(source, tmp_path / "out")


def test_a_timeout_is_reported_as_such(tmp_path, monkeypatch):
    source = tmp_path / "song.wav"
    source.write_bytes(b"RIFF")

    monkeypatch.setattr("rigsmith.match.separate.demucs_available", lambda: True)

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1.0)

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(SeparationUnavailable, match="timed out"):
        separate(source, tmp_path / "out", timeout_s=1.0)


def test_demucs_availability_check_does_not_raise():
    assert isinstance(demucs_available(), bool)


# --------------------------------------------------------------------------
# recorder
# --------------------------------------------------------------------------

pytest.importorskip("fastapi", reason="the recorder needs pip install 'rigsmith[ui]'")

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from rigsmith.protocol.recorder import (  # noqa: E402
    HOP_BY_HOP,
    MAX_LOGGED_BODY,
    Exchange,
    Recording,
    build_app,
)


@pytest.fixture()
def upstream_url():
    """A tiny upstream, served in-process."""
    import socket
    import threading
    import time

    import httpx
    import uvicorn

    app = FastAPI()

    @app.get("/api/v1/rigs")
    def rigs():
        return {"rigs": [{"id": 1, "name": "Cherub Rock"}]}

    @app.post("/api/v1/rigs/1/blocks")
    def add_block(payload: dict):
        return {"ok": True, "slot": payload.get("slot")}

    @app.get("/binary")
    def binary():
        from fastapi.responses import Response

        return Response(content=b"\x00\x01\x02", media_type="application/octet-stream")

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    )
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(80):
        try:
            httpx.get(f"{url}/api/v1/rigs", timeout=0.3)
            break
        except Exception:
            time.sleep(0.05)
    yield url
    server.should_exit = True


def test_records_requests_and_responses(tmp_path, upstream_url):
    recording = Recording(tmp_path / "rec", upstream_url)
    with TestClient(build_app(upstream_url, recording)) as client:
        assert client.get("/api/v1/rigs").json()["rigs"][0]["name"] == "Cherub Rock"
        assert client.post("/api/v1/rigs/1/blocks", json={"slot": 3}).json()["ok"]
    recording.close()

    assert len(recording.exchanges) == 2
    methods = [e.method for e in recording.exchanges]
    assert methods == ["GET", "POST"]


def test_the_log_is_written_as_it_goes(tmp_path, upstream_url):
    """Flushed per exchange, so a Ctrl-C loses nothing."""
    recording = Recording(tmp_path / "rec", upstream_url)
    with TestClient(build_app(upstream_url, recording)) as client:
        client.get("/api/v1/rigs")

    lines = recording.path.read_text(encoding="utf-8").strip().splitlines()
    recording.close()

    assert json.loads(lines[0])["kind"] == "meta"
    entry = json.loads(lines[1])
    assert entry["method"] == "GET"
    assert entry["status"] == 200
    assert "Cherub Rock" in entry["response_body"]


def test_request_bodies_are_captured(tmp_path, upstream_url):
    recording = Recording(tmp_path / "rec", upstream_url)
    with TestClient(build_app(upstream_url, recording)) as client:
        client.post("/api/v1/rigs/1/blocks", json={"slot": 7})
    recording.close()

    exchange = recording.exchanges[0]
    assert "7" in (exchange.request_body or "")


def test_sequence_numbers_preserve_the_order_of_operations(tmp_path, upstream_url):
    recording = Recording(tmp_path / "rec", upstream_url)
    with TestClient(build_app(upstream_url, recording)) as client:
        for _ in range(3):
            client.get("/api/v1/rigs")
    recording.close()

    assert [e.seq for e in recording.exchanges] == [1, 2, 3]


def test_binary_responses_are_noted_not_mangled(tmp_path, upstream_url):
    recording = Recording(tmp_path / "rec", upstream_url)
    with TestClient(build_app(upstream_url, recording)) as client:
        client.get("/binary")
    recording.close()

    assert "bytes binary" in recording.exchanges[0].response_body


def test_summary_groups_by_endpoint(tmp_path, upstream_url):
    recording = Recording(tmp_path / "rec", upstream_url)
    with TestClient(build_app(upstream_url, recording)) as client:
        client.get("/api/v1/rigs")
        client.get("/api/v1/rigs")
        client.post("/api/v1/rigs/1/blocks", json={})
    recording.close()

    summary = recording.summary()
    assert "GET    x2" in summary
    assert "/api/v1/rigs" in summary


def test_hop_by_hop_headers_are_not_forwarded():
    """Passing these through breaks keep-alive and content length."""
    for header in ("connection", "transfer-encoding", "content-length", "host"):
        assert header in HOP_BY_HOP


def test_oversized_bodies_are_truncated_with_a_note():
    from rigsmith.protocol.recorder import _decode

    decoded = _decode(b"x" * (MAX_LOGGED_BODY + 100))
    assert "truncated" in decoded
    assert len(decoded) < MAX_LOGGED_BODY + 200


def test_exchange_serialises_without_empty_fields():
    data = Exchange(seq=1, at=0.0, kind="http", method="GET", path="/x").to_dict()
    assert "request_body" not in data
    assert "direction" not in data
    assert data["method"] == "GET"

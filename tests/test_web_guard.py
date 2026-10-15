"""Only this machine's browser, on rigsmith's own page, can drive the UI.

Loopback is not a boundary: any page in the same browser can reach
127.0.0.1. These are the requests a hostile page could make, refused.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="the UI is optional; pip install 'rigsmith[ui]'")

from fastapi.testclient import TestClient  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from rigsmith import config  # noqa: E402
from rigsmith.core import drafts, rigs  # noqa: E402
from rigsmith.web.guard import check  # noqa: E402
from rigsmith.web.server import build_app  # noqa: E402

HOST = "127.0.0.1:8765"
LOCAL = frozenset({"127.0.0.1", "localhost", "::1"})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    drafts.set_store(drafts.DraftStore(tmp_path / "drafts"))
    yield TestClient(build_app(), base_url=f"http://{HOST}")
    config.set_settings(None)
    drafts.set_store(None)


def test_a_form_on_another_site_cannot_write_a_rig(client):
    """A plain cross-site form POST needs no preflight; this one started a device write."""
    draft = rigs.create("Victim").draft.draft_id
    refused = client.post(f"/api/drafts/{draft}/write?confirm=true", headers={"Origin": "http://evil.example"})
    assert refused.status_code == 403 and "its own page" in refused.json()["error"]
    no_origin = client.post(f"/api/drafts/{draft}/duplicate", headers={"Referer": "http://evil.example/page"})
    assert no_origin.status_code == 403, "an old browser sends a Referer instead"


def test_a_rebound_hostname_is_refused_even_from_its_own_origin(client):
    """DNS rebinding makes the attacker's page same-origin; its Host gives it away."""
    response = client.get("/api/health", headers={"Host": "attacker.example:8765", "Origin": "http://attacker.example:8765"})
    assert response.status_code == 403 and "this machine" in response.json()["error"]


def test_the_page_itself_and_non_browser_clients_still_work(client):
    assert client.get("/api/health").status_code == 200, "no Origin: the CLI, curl, scripts"
    made = client.post("/api/drafts", json={"name": "Mine"}, headers={"Origin": f"http://{HOST}"})
    assert made.status_code == 200
    assert client.get("/api/health", headers={"Host": "[::1]:8765"}).status_code == 200


def test_another_site_cannot_listen_to_the_event_stream(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws", headers={"Origin": "http://evil.example"}) as socket:
            socket.receive_text()
    # The test client's websocket always calls itself "testserver"; a
    # browser sends the page's own host, which is what matches here.
    with client.websocket_connect("/ws", headers={"Origin": "http://testserver"}):
        pass


def test_a_get_cannot_name_a_file_for_the_player(client, tmp_path):
    """An <img> tag used to widen the allowance, and /api/audio then served the file."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside.wav"
    outside.write_bytes(b"RIFF....WAVE")
    client.get("/api/match/suggest-region", params={"path": str(outside)})
    assert client.get("/api/audio", params={"path": str(outside)}).status_code == 403


def test_the_rules_themselves():
    assert check("GET", {"host": "localhost:9000"}, hosts=LOCAL) is None
    assert check("POST", {"host": "localhost:9000", "origin": "http://localhost:3000"}, hosts=LOCAL), "another port is another origin"
    assert check("POST", {"host": "localhost:9000", "origin": "null"}, hosts=LOCAL), "a sandboxed frame says null"
    assert check("GET", {"host": "192.168.1.5:9000"}, hosts=None) is None, "--host 0.0.0.0 is a deliberate choice"

"""Tests for static analysis of the Remote Editor bundle.

The real bundle is unavailable without hardware, so these exercise the
extraction logic against representative JavaScript. The point is that when
the device does come online, we already know the miner works.
"""

from __future__ import annotations

from rigsmith.protocol.capture import Inventory, analyse_bundle

BUNDLE = """
const BASE = "/api/v1";
async function loadRigs() { return fetch("/api/v1/rigs").then(r => r.json()); }
async function getRig(id) { return fetch(`/api/v1/rigs/${id}`); }
axios.post("/api/v1/rigs/create", payload);
axios.put("/api/v1/rigs/7/blocks/3/param", { value: 0.5 });
const xhr = new XMLHttpRequest();
xhr.open("DELETE", "/api/v1/setlists/2");
const sock = new WebSocket("ws://headrushcore.local/ws/state");
sock.send(JSON.stringify({ type: "param_change", slot: 3 }));
sock.send(JSON.stringify({ cmd: "scene_select" }));
dispatch({ event: "rig_loaded" });
fetch("/api/v1/status", { method: "POST" });
const asset = "/static/js/main.9f2a.js";
"""


def analyse(text: str = BUNDLE):
    return analyse_bundle({"http://headrushcore.local/app.js": text})


def test_finds_fetch_endpoints():
    endpoints, *_ = analyse()
    paths = {e.path for e in endpoints}
    assert "/api/v1/rigs" in paths
    assert "/api/v1/status" in paths


def test_call_sites_outrank_bare_literals():
    endpoints, *_ = analyse()
    by_path = {e.path: e for e in endpoints}
    assert by_path["/api/v1/rigs"].evidence == "call"
    # "/api/v1" only ever appears as a plain string.
    assert by_path["/api/v1"].evidence == "literal"
    # Strong evidence sorts first.
    assert endpoints[0].evidence == "call"


def test_extracts_http_verbs_from_client_calls():
    endpoints, *_ = analyse()
    by_path = {e.path: e for e in endpoints}
    assert "POST" in by_path["/api/v1/rigs/create"].methods
    assert "PUT" in by_path["/api/v1/rigs/7/blocks/3/param"].methods
    assert "DELETE" in by_path["/api/v1/setlists/2"].methods


def test_template_literals_are_kept_and_flagged():
    endpoints, *_ = analyse()
    templated = [e for e in endpoints if e.templated]
    assert any("rigs" in e.path for e in templated)


def test_extracts_websocket_url():
    _, _, websockets, ctors, _ = analyse()
    assert "ws://headrushcore.local/ws/state" in websockets
    assert ctors, "the constructor call site should be recorded too"


def test_extracts_message_types_from_bare_and_quoted_keys():
    *_, message_types = analyse()
    assert {"param_change", "scene_select", "rig_loaded"} <= set(message_types)


def test_http_verbs_are_not_mistaken_for_message_types():
    """`method: "POST"` is a request option, not a protocol message."""
    *_, message_types = analyse()
    assert "POST" not in message_types
    assert "GET" not in message_types


def test_non_api_assets_land_in_generic_paths():
    endpoints, generic, *_ = analyse()
    assert "/static/js/main.9f2a.js" in generic
    assert "/static/js/main.9f2a.js" not in {e.path for e in endpoints}


def test_empty_bundle_yields_nothing_rather_than_guessing():
    endpoints, generic, websockets, ctors, message_types = analyse("")
    assert endpoints == []
    assert generic == []
    assert websockets == []
    assert message_types == []


def test_inventory_round_trips_through_json(tmp_path):
    from rigsmith.protocol.capture import load_inventory

    endpoints, generic, websockets, ctors, message_types = analyse()
    inventory = Inventory(
        base_url="http://headrushcore.local",
        captured_at="2026-09-14T00:00:00+00:00",
        endpoints=endpoints,
        generic_paths=generic,
        websockets=websockets,
        websocket_constructors=ctors,
        message_types=message_types,
    )

    import json

    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(inventory.to_dict(), indent=2), encoding="utf-8")

    restored = load_inventory(path)
    assert restored.base_url == inventory.base_url
    assert {e.path for e in restored.endpoints} == {e.path for e in endpoints}
    assert restored.websockets == websockets


def test_summary_counts_strong_evidence():
    endpoints, _, websockets, _, message_types = analyse()
    inventory = Inventory(
        base_url="http://x",
        captured_at="now",
        endpoints=endpoints,
        websockets=websockets,
        message_types=message_types,
    )
    summary = inventory.summary()
    assert "endpoints" in summary and "call sites" in summary


# --------------------------------------------------------------------------
# patterns real bundles actually use
# --------------------------------------------------------------------------

CONSTRUCTED = """
var sock = new WebSocket("ws://" + location.host + "/api/v1/ws");
sock.onmessage = function(e){
  var m = JSON.parse(e.data);
  if (m.type === "cpu") { render(m) }
  else if (m.type === "rig_changed") { reload(m.id) }
};
function send(t, p){ sock.send(JSON.stringify({type: t, payload: p})) }
function selectRig(i){ send("select_rig", {id: i}) }
function pressFootswitch(n){ send("footswitch", {number: n}) }
"""


def test_websocket_path_recovered_from_a_constructed_url():
    """Bundles build their socket URL; they rarely write a literal one.

    Matching only `"ws://..."` string literals found nothing on exactly the
    kind of bundle this exists to read.
    """
    _, _, websockets, ctors, _ = analyse(CONSTRUCTED)
    assert "/api/v1/ws" in websockets
    assert ctors, "the constructor expression should still be recorded"


def test_message_types_found_in_comparisons():
    """The receiving half names types by comparing, not by declaring.

    `if (m.type === "cpu")` is frequently the only place a type name appears
    as a literal, because the sending half passes a variable.
    """
    *_, message_types = analyse(CONSTRUCTED)
    assert "cpu" in message_types
    assert "rig_changed" in message_types


def test_message_types_found_in_send_wrappers():
    *_, message_types = analyse(CONSTRUCTED)
    assert "select_rig" in message_types
    assert "footswitch" in message_types


def test_variable_message_type_is_not_mistaken_for_a_literal():
    """`{type: t}` names no type; it must not invent one."""
    *_, message_types = analyse("send(JSON.stringify({type: t, payload: p}))")
    assert "t" not in message_types

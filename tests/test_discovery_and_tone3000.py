"""Tests for device discovery and the TONE3000 client.

Both had no coverage, and both are places where a quiet wrong answer costs
real time: discovery that reports a router as a HeadRush, or an API client
that misreads a tier refusal as an empty result.

No network is used. Discovery runs against a stub HTTP server in-process, and
the TONE3000 client against a mock transport.
"""

from __future__ import annotations

import ipaddress
import json

import httpx
import pytest

from rigsmith.device import discovery
from rigsmith.device.discovery import DeviceInfo, _extract_title, _fingerprint

# --------------------------------------------------------------------------
# fingerprinting
# --------------------------------------------------------------------------


def test_recognises_a_headrush_page():
    assert _fingerprint("<h1>HeadRush Remote Editor</h1>", {})


def test_recognises_it_in_a_header():
    assert _fingerprint("<html></html>", {"server": "inMusic-httpd/1.0"})


def test_does_not_recognise_an_unrelated_device():
    """A router's admin page must not read as an amp modeler."""
    assert not _fingerprint(
        "<title>Router Admin</title><h1>Wireless Settings</h1>",
        {"server": "lighttpd/1.4"},
    )


def test_fingerprint_is_case_insensitive():
    assert _fingerprint("HEADRUSH CORE", {})


def test_title_extraction():
    assert _extract_title("<html><head><title>HeadRush</title></head>") == "HeadRush"
    assert _extract_title("<TITLE>  Spaced  </TITLE>") == "Spaced"
    assert _extract_title("<html>no title</html>") is None
    assert _extract_title("<title></title>") is None


# --------------------------------------------------------------------------
# DeviceInfo
# --------------------------------------------------------------------------


def test_base_url_omits_the_default_port():
    assert DeviceInfo(host="headrushcore.local", port=80).base_url == (
        "http://headrushcore.local"
    )
    assert DeviceInfo(host="x", port=8080).base_url == "http://x:8080"
    assert DeviceInfo(host="x", port=443, scheme="https").base_url == "https://x"


def test_describe_distinguishes_confirmed_from_guessed():
    confirmed = DeviceInfo(host="a", confirmed=True, source="hostname")
    unconfirmed = DeviceInfo(host="b", confirmed=False, source="sweep")
    assert "confirmed" in confirmed.describe()
    assert "unconfirmed" in unconfirmed.describe()


# --------------------------------------------------------------------------
# probing
# --------------------------------------------------------------------------


def stub_transport(body: str, *, status: int = 200, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, headers=headers or {})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_probe_confirms_a_headrush():
    async with httpx.AsyncClient(
        transport=stub_transport("<title>HeadRush Remote Editor</title>")
    ) as client:
        info = await discovery.probe_host("10.0.0.5", client=client)

    assert info is not None
    assert info.confirmed
    assert info.title == "HeadRush Remote Editor"


@pytest.mark.asyncio
async def test_probe_returns_near_misses_rather_than_nothing():
    """"Something answered but was not a HeadRush" is the useful diagnostic."""
    async with httpx.AsyncClient(
        transport=stub_transport("<title>Router</title>")
    ) as client:
        info = await discovery.probe_host("10.0.0.1", client=client)

    assert info is not None
    assert not info.confirmed
    assert info.title == "Router"


@pytest.mark.asyncio
async def test_probe_returns_none_when_nothing_answers():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        assert await discovery.probe_host("10.0.0.9", client=client) is None


@pytest.mark.asyncio
async def test_probe_survives_a_server_error():
    async with httpx.AsyncClient(
        transport=stub_transport("boom", status=500)
    ) as client:
        info = await discovery.probe_host("10.0.0.7", client=client)
    assert info is not None
    assert not info.confirmed


# --------------------------------------------------------------------------
# subnet selection
# --------------------------------------------------------------------------


def test_local_subnets_excludes_noise():
    """Sweeping loopback, link-local or the WSL range is wasted time."""
    nets = discovery.local_subnets()
    for net in nets:
        assert net.prefixlen == 24
        assert net.network_address.is_private
        assert not net.network_address.is_loopback
        assert not net.network_address.is_link_local
        assert net.network_address not in ipaddress.IPv4Network("172.16.0.0/12")


def test_configured_device_short_circuits_discovery(monkeypatch):
    """An explicit address must not trigger a subnet sweep."""
    from rigsmith import config

    monkeypatch.setenv("RIGSMITH_DEVICE", "10.1.2.3:9000")
    config.set_settings(config.Settings())

    swept = []
    monkeypatch.setattr(
        discovery, "sweep", lambda *a, **k: swept.append(a) or []
    )

    async def fake_probe(host, **kwargs):
        return DeviceInfo(host=host, port=kwargs.get("port", 80), confirmed=True)

    monkeypatch.setattr(discovery, "probe_host", fake_probe)

    import asyncio

    found = asyncio.run(discovery.discover())
    assert len(found) == 1
    assert found[0].host == "10.1.2.3"
    assert found[0].port == 9000
    assert found[0].source == "configured"
    assert not swept, "a configured device should not trigger a sweep"
    config.set_settings(None)


def test_results_sort_confirmed_first():
    devices = [
        DeviceInfo(host="c", confirmed=False, source="sweep"),
        DeviceInfo(host="a", confirmed=True, source="hostname"),
        DeviceInfo(host="b", confirmed=True, source="zeroconf"),
    ]
    devices.sort(
        key=lambda i: (not i.confirmed, discovery.SOURCE_RANK.get(i.source, 9))
    )
    assert [d.host for d in devices] == ["a", "b", "c"]


# --------------------------------------------------------------------------
# TONE3000
# --------------------------------------------------------------------------


from rigsmith.tone.tone3000 import (  # noqa: E402
    Tone,
    Tone3000AuthError,
    Tone3000Client,
    Tone3000Error,
    Tone3000TierError,
    ToneModel,
    TokenSet,
    _items,
    _join,
    generate_pkce,
    load_tokens,
    save_tokens,
)


def test_pkce_challenge_is_derived_from_the_verifier():
    import base64
    import hashlib

    verifier, challenge = generate_pkce()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert challenge == expected
    assert "=" not in challenge


def test_pkce_is_different_every_time():
    assert generate_pkce()[0] != generate_pkce()[0]


def test_authorize_url_is_properly_encoded():
    client = Tone3000Client(client_id="abc")
    url, state = client.authorize_url(
        "http://127.0.0.1:8765/callback", challenge="chal"
    )
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A8765%2Fcallback" in url
    assert "code_challenge_method=S256" in url
    assert state in url


def test_authorize_url_without_a_client_id_is_refused():
    with pytest.raises(Tone3000AuthError, match="client_id"):
        Tone3000Client().authorize_url("http://x", challenge="c")


def test_separator_quirks_are_respected():
    """sizes is hyphen-separated; everything else is not. Easy to get wrong."""
    assert _join(["lite", "standard"], "-") == "lite-standard"
    assert _join(["Marshall", "Fender"], "_") == "Marshall_Fender"
    assert _join("already a string", "_") == "already a string"
    assert _join(None, "_") is None
    assert _join([], "_") is None


def test_envelope_unwrapping_handles_every_shape():
    assert _items({"data": [{"id": 1}]}) == [{"id": 1}]
    assert _items({"tones": [{"id": 2}]}) == [{"id": 2}]
    assert _items([{"id": 3}]) == [{"id": 3}]
    assert _items({"id": 4}) == [{"id": 4}]
    assert _items({"unexpected": "shape"}) == []
    assert _items(None) == []


def test_tone_reports_its_architecture_mix():
    tone = Tone.from_api(
        {"id": 1, "title": "T", "a2_models_count": 3, "a1_models_count": 1}
    )
    assert tone.has_a2
    assert "A2 x3" in tone.describe()
    assert "A1 x1" in tone.describe()


def test_model_generation_and_compatibility():
    a2 = ToneModel.from_api({"id": "m", "architecture_version": 2, "size": "lite"})
    assert a2.generation == "A2"
    assert a2.headrush_compatible is True

    exotic = ToneModel.from_api(
        {"id": "m", "architecture_version": "custom", "size": "custom"}
    )
    assert exotic.generation == "unknown"
    assert exotic.headrush_compatible is None, "unknown must not read as yes"


def test_token_expiry():
    import time

    assert not TokenSet(access_token="x").expired
    assert TokenSet(access_token="x", expires_at=time.time() - 1).expired
    # Refreshed a minute early, so a call does not fail mid-flight.
    assert TokenSet(access_token="x", expires_at=time.time() + 30).expired
    assert not TokenSet(access_token="x", expires_at=time.time() + 600).expired


def test_tokens_round_trip_through_the_credentials_file(tmp_path):
    path = tmp_path / "credentials.json"
    assert load_tokens(path) is None

    save_tokens(path, TokenSet(access_token="a", refresh_token="r", expires_at=123))
    restored = load_tokens(path)
    assert restored.access_token == "a"
    assert restored.refresh_token == "r"


def test_saving_tokens_preserves_other_credentials(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"something_else": {"key": "value"}}), encoding="utf-8")

    save_tokens(path, TokenSet(access_token="a"))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["something_else"] == {"key": "value"}
    assert data["tone3000"]["access_token"] == "a"


def test_corrupt_credentials_read_as_absent(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_tokens(path) is None


def tone3000_stub(status: int, body: str = "{}"):
    """A transport that always answers the same way."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=body, request=request)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_a_tier_refusal_is_distinguished_from_an_empty_result():
    """403 means "your account cannot", not "there is nothing"."""
    client = Tone3000Client(
        tokens=TokenSet(access_token="t"),
        client_id="c",
        transport=tone3000_stub(403, "commercial tier required"),
    )
    with pytest.raises(Tone3000TierError) as excinfo:
        await client.search("marshall")
    assert "commercial" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_missing_credentials_are_reported_as_an_auth_problem():
    client = Tone3000Client(transport=tone3000_stub(401))
    with pytest.raises(Tone3000AuthError, match="login"):
        await client.search("x")


@pytest.mark.asyncio
async def test_rate_limiting_is_named_rather_than_generic():
    client = Tone3000Client(
        tokens=TokenSet(access_token="t"), transport=tone3000_stub(429)
    )
    with pytest.raises(Tone3000Error, match="rate limited"):
        await client.search("x")


@pytest.mark.asyncio
async def test_a_successful_search_parses_results():
    body = json.dumps(
        {"data": [{"id": 7, "title": "JCM800 Hot", "a2_models_count": 2}]}
    )
    client = Tone3000Client(
        tokens=TokenSet(access_token="t"), transport=tone3000_stub(200, body)
    )
    tones = await client.search("jcm800")
    assert len(tones) == 1
    assert tones[0].title == "JCM800 Hot"
    assert tones[0].has_a2


@pytest.mark.asyncio
async def test_non_json_response_is_reported_clearly():
    client = Tone3000Client(
        tokens=TokenSet(access_token="t"),
        transport=tone3000_stub(200, "<html>maintenance</html>"),
    )
    with pytest.raises(Tone3000Error, match="not JSON"):
        await client.search("x")


def test_invalid_sort_is_rejected_before_a_request():
    import asyncio

    client = Tone3000Client(tokens=TokenSet(access_token="t"))
    with pytest.raises(ValueError, match="sort must be"):
        asyncio.run(client.search("x", sort="whatever"))

"""End-to-end tests of the MCP tool surface.

These drive the tools the way an agent would, through the server's own
dispatch, so the registered schemas and the handlers stay in step.
"""

from __future__ import annotations

import pytest

mcp_sdk = pytest.importorskip(
    "mcp", reason="the MCP SDK is optional; install with pip install 'rigsmith[mcp]'"
)


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """A server with an isolated workspace, so tests never touch real drafts."""
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)

    from rigsmith import config
    from rigsmith.mcp import session

    config.set_settings(config.Settings())
    session.set_store(session.DraftStore(tmp_path / "drafts"))

    from rigsmith.mcp import server as module

    yield module

    config.set_settings(None)
    session.set_store(None)


def _text(payload) -> str:
    """Flatten whatever the SDK hands back into plain text."""
    if isinstance(payload, tuple):
        payload = payload[0]
    if hasattr(payload, "content"):
        payload = payload.content
    if isinstance(payload, list):
        return "\n".join(getattr(item, "text", str(item)) for item in payload)
    return getattr(payload, "text", str(payload))


async def call(server, tool: str, args: dict) -> str:
    """Invoke a tool and return its text output."""
    return _text(await server.mcp.call_tool(tool, args))


@pytest.mark.asyncio
async def test_all_tools_register(server):
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert {
        "discover_device",
        "create_rig",
        "add_block",
        "configure_scene",
        "validate_rig",
        "preview_changes",
        "write_rig",
        "search_captures",
        "inspect_capture",
    } <= names


@pytest.mark.asyncio
async def test_every_tool_has_a_description(server):
    """Descriptions are what the agent reads to choose a tool."""
    for tool in await server.mcp.list_tools():
        assert tool.description, f"{tool.name} has no description"
        assert len(tool.description) > 40, f"{tool.name} description is too thin"


@pytest.mark.asyncio
async def test_build_a_rig_end_to_end(server):
    out = await call(server, "create_rig", {"name": "Cherub Rock"})
    assert "cherub-rock" in out

    for category, model_id in [
        ("effect", "Big Muff"),
        ("nam", "Marshall JMP-1959"),
        ("ir", "1960A-SM57"),
        ("effect", "Analog Delay"),
    ]:
        await call(
            server,
            "add_block",
            {"draft_id": "cherub-rock", "category": category, "model_id": model_id},
        )

    await call(
        server,
        "configure_scene",
        {
            "draft_id": "cherub-rock",
            "name": "Clean verse",
            "off_slots": [1, 4],
            "footswitch": 1,
        },
    )

    explained = await call(server, "explain_rig", {"draft_id": "cherub-rock"})
    assert "Big Muff -> NAM(Marshall JMP-1959) -> IR(1960A-SM57) -> Analog Delay" in explained
    assert "Clean verse" in explained

    validated = await call(server, "validate_rig", {"draft_id": "cherub-rock"})
    assert validated.startswith("VALID")

    preview = await call(server, "preview_changes", {"draft_id": "cherub-rock"})
    assert "create rig 'Cherub Rock'" in preview
    assert "5 add" not in preview  # four blocks, not five


@pytest.mark.asyncio
async def test_unknown_models_reported_once_not_per_block(server):
    """An unverified catalog must not bury real findings under noise."""
    await call(server, "create_rig", {"name": "Noise"})
    for i in range(5):
        await call(
            server,
            "add_block",
            {"draft_id": "noise", "category": "effect", "model_id": f"Pedal {i}"},
        )

    report = await call(server, "validate_rig", {"draft_id": "noise"})
    assert report.count("not in the catalog") == 1


@pytest.mark.asyncio
async def test_write_requires_confirmation(server):
    await call(server, "create_rig", {"name": "Gate"})
    await call(
        server,
        "add_block",
        {"draft_id": "gate", "category": "nam", "model_id": "Amp"},
    )

    out = await call(server, "write_rig", {"draft_id": "gate"})
    assert "confirm=True" in out
    assert "Not written" in out


@pytest.mark.asyncio
async def test_write_blocked_by_validation_errors(server):
    await call(server, "create_rig", {"name": "Bad"})
    await call(
        server,
        "add_block",
        {"draft_id": "bad", "category": "nam", "model_id": "Amp"},
    )
    await call(
        server,
        "configure_scene",
        {"draft_id": "bad", "name": "Nope", "on_slots": [13]},
    )

    out = await call(server, "write_rig", {"draft_id": "bad", "confirm": True})
    assert "Refusing to write" in out


@pytest.mark.asyncio
async def test_readonly_mode_blocks_export(server, monkeypatch):
    await call(server, "create_rig", {"name": "Locked"})

    from rigsmith import config

    monkeypatch.setenv("RIGSMITH_READONLY", "1")
    config.set_settings(config.Settings())

    with pytest.raises(Exception) as excinfo:
        await call(server, "export_rig", {"draft_id": "locked"})
    assert "read-only" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_drafts_survive_a_new_store(server, tmp_path):
    """A half-built rig must not evaporate between sessions."""
    await call(server, "create_rig", {"name": "Persist"})
    await call(
        server,
        "add_block",
        {"draft_id": "persist", "category": "amp", "model_id": "Plexi"},
    )

    from rigsmith.mcp import session

    session.set_store(session.DraftStore(tmp_path / "drafts"))

    reloaded = await call(server, "explain_rig", {"draft_id": "persist"})
    assert "Plexi" in reloaded


@pytest.mark.asyncio
async def test_scene_slots_follow_blocks_through_a_move(server):
    await call(server, "create_rig", {"name": "Reorder"})
    for model_id in ("Muff", "Amp", "IR", "Delay"):
        await call(
            server,
            "add_block",
            {"draft_id": "reorder", "category": "effect", "model_id": model_id},
        )
    await call(
        server,
        "configure_scene",
        {"draft_id": "reorder", "name": "V", "off_slots": [1], "on_slots": [4]},
    )

    await call(server, "move_block", {"draft_id": "reorder", "from_slot": 4, "to_slot": 1})

    from rigsmith.mcp.session import get_store

    rig = get_store().get("reorder").rig
    slots = {b.model_id: b.slot for b in rig.blocks}
    scene = rig.scenes[0]
    from rigsmith.rig.model import SceneBlockState

    assert scene.states[slots["Muff"]] is SceneBlockState.OFF
    assert scene.states[slots["Delay"]] is SceneBlockState.ON


@pytest.mark.asyncio
async def test_unknown_draft_reports_known_ones(server):
    await call(server, "create_rig", {"name": "Real"})
    with pytest.raises(Exception) as excinfo:
        await call(server, "explain_rig", {"draft_id": "imaginary"})
    assert "real" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_inspect_capture_reads_a_nam_file(server, tmp_path):
    import json

    path = tmp_path / "jcm800.nam"
    path.write_text(
        json.dumps(
            {
                "version": "0.5.4",
                "architecture": "WaveNet",
                "config": {"activation": "LeakyReLU", "size": "lite"},
                "weights": [0.0] * 10,
                "metadata": {
                    "name": "JCM800",
                    "gear_make": "Marshall",
                    "tone_type": "hi_gain",
                },
            }
        ),
        encoding="utf-8",
    )

    out = await call(server, "inspect_capture", {"path": str(path)})
    assert "A2" in out
    assert "loadable on the Core: yes" in out

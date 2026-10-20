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
    from rigsmith.core import drafts as session

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
async def test_an_agent_can_read_the_notes_a_part_s_di_plays(server, monkeypatch):
    from rigsmith.core import songs

    notes = {"part_id": "intro-1-stem", "label": "intro - all guitars", "duration": 4.0, "source": "transcribed", "heard_as": "arpeggio",
             "events": [{"start": 0.0, "duration": 0.4, "midi": 52.0}, {"start": 0.14, "duration": 0.4, "midi": 56.0}], "midi_path": None}
    monkeypatch.setattr(songs, "part_notes", lambda song_id, part_id: notes)
    out = await call(server, "song_part_notes", {"song_id": "a-song", "part_id": "intro-1-stem"})
    assert "2 notes from transcribed, heard as arpeggio, E3 to G#3" in out
    assert "G#3" in out.splitlines()[-1]


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

    from rigsmith.core import drafts as session

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

    from rigsmith.core.drafts import get_store

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


# --------------------------------------------------------------------------
# configure_scene merge semantics (regression)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_configure_scene_merges_instead_of_replacing(server):
    """It used to rebuild the scene from only the current call's arguments.

    Adding one on-slot silently wiped every previously configured slot and
    the footswitch assignment - which the draft workflow actively invites,
    since it encourages building a scene up over several calls.
    """
    await call(server, "create_rig", {"name": "Merge"})
    for i in range(5):
        await call(
            server,
            "add_block",
            {"draft_id": "merge", "category": "effect", "model_id": f"p{i}"},
        )

    await call(
        server,
        "configure_scene",
        {"draft_id": "merge", "name": "Lead", "on_slots": [1, 2], "footswitch": 3},
    )
    await call(
        server,
        "configure_scene",
        {"draft_id": "merge", "name": "Lead", "on_slots": [3]},
    )

    from rigsmith.core.drafts import get_store
    from rigsmith.rig.model import SceneBlockState

    scene = get_store().get("merge").rig.scenes[0]
    on = sorted(s for s, v in scene.states.items() if v is SceneBlockState.ON)
    assert on == [1, 2, 3], "earlier slots must survive a later call"
    assert scene.footswitch == 3, "footswitch must survive when not re-passed"


@pytest.mark.asyncio
async def test_configure_scene_no_change_slots_removes_a_slot(server):
    await call(server, "create_rig", {"name": "Drop"})
    for i in range(4):
        await call(
            server,
            "add_block",
            {"draft_id": "drop", "category": "effect", "model_id": f"p{i}"},
        )
    await call(
        server,
        "configure_scene",
        {"draft_id": "drop", "name": "S", "on_slots": [1, 2], "presets": {"1": "x"}},
    )
    await call(
        server,
        "configure_scene",
        {"draft_id": "drop", "name": "S", "no_change_slots": [1]},
    )

    from rigsmith.core.drafts import get_store

    scene = get_store().get("drop").rig.scenes[0]
    assert 1 not in scene.states
    assert 1 not in scene.presets
    assert 2 in scene.states


@pytest.mark.asyncio
async def test_configure_scene_reset_clears_first(server):
    await call(server, "create_rig", {"name": "Reset"})
    for i in range(5):
        await call(
            server,
            "add_block",
            {"draft_id": "reset", "category": "effect", "model_id": f"p{i}"},
        )
    await call(
        server,
        "configure_scene",
        {"draft_id": "reset", "name": "S", "on_slots": [1, 2], "footswitch": 2},
    )
    await call(
        server,
        "configure_scene",
        {"draft_id": "reset", "name": "S", "on_slots": [5], "reset": True},
    )

    from rigsmith.core.drafts import get_store
    from rigsmith.rig.model import SceneBlockState

    scene = get_store().get("reset").rig.scenes[0]
    assert sorted(scene.states) == [5]
    assert scene.states[5] is SceneBlockState.ON
    assert scene.footswitch is None


@pytest.mark.asyncio
async def test_configure_scene_reports_the_whole_scene_not_just_the_delta(server):
    """The agent needs to see resulting state, not the arguments it passed."""
    await call(server, "create_rig", {"name": "Report"})
    for i in range(4):
        await call(
            server,
            "add_block",
            {"draft_id": "report", "category": "effect", "model_id": f"p{i}"},
        )
    await call(
        server,
        "configure_scene",
        {"draft_id": "report", "name": "S", "on_slots": [1], "footswitch": 1},
    )
    out = await call(
        server, "configure_scene", {"draft_id": "report", "name": "S", "on_slots": [2]}
    )
    assert "[1, 2]" in out
    assert "footswitch=1" in out


@pytest.mark.asyncio
async def test_what_the_cli_can_do_to_a_song_an_agent_can_too(server, tmp_path):
    """Adding audio, aligning a take and resuming a run were CLI-only."""
    pytest.importorskip("soundfile")
    import numpy as np
    import soundfile as sf

    from rigsmith.core import autopilot, songs

    song_id = songs.create("Agent Song").song_id
    mix = tmp_path / "mix.wav"
    sf.write(str(mix), (0.1 * np.sin(np.arange(48000) / 48000 * 2 * np.pi * 110)).astype("float32"), 48000)
    assert "mix.wav" in await call(server, "add_song_audio", {"song_id": song_id, "mix_path": str(mix)})
    assert "give a mix_path" in await call(server, "add_song_audio", {"song_id": song_id})

    stopped = autopilot.Run(run_id="stopped-run", request=autopilot.Request(title="Agent Song"), state="stopped")
    stopped.save()
    with pytest.raises(Exception) as caught:
        await call(server, "resume_autopilot", {"run_id": "stopped-run", "from_stage": "nonsense"})
    assert "no stage" in str(caught.value)


def test_building_the_server_does_not_turn_on_request_logging(server):
    """The SDK set the root logger to INFO, and every HTTP request was printed."""
    import logging

    assert logging.getLogger().level >= logging.WARNING

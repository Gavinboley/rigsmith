"""The in-app agents, against a scripted model server.

No real model runs here: a mock OpenAI-compatible endpoint replays the
responses a local Qwen would give - proper tool_calls, tool calls leaked
into the text in either of Qwen's formats, think blocks - so the loop around
them is what gets tested.
"""

from __future__ import annotations

import json
import threading
import time

import pytest

pytest.importorskip("mcp", reason="needs pip install 'rigsmith[mcp]'")
httpx = pytest.importorskip("httpx")

from rigsmith import config  # noqa: E402
from rigsmith.agent import providers, roles, runner, tools  # noqa: E402
from rigsmith.core import appsettings, chat, events  # noqa: E402
from rigsmith.core.errors import Invalid  # noqa: E402


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    monkeypatch.delenv("RIGSMITH_MODE", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)
    events.reset()


def scripted(replies):
    """A model server that answers with each reply in turn."""
    seen = []

    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-8b"}, {"id": "text-embedding-qwen3"}]})
        body = json.loads(request.content)
        seen.append(body)
        message = replies[min(len(seen) - 1, len(replies) - 1)]
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})

    return httpx.MockTransport(handler), seen


def local(transport):
    return providers.OpenAICompatible("http://local.test/v1", transport=transport)


def call(name, arguments=None, id_="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": id_, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments or {})}}]}


def test_the_registry_is_the_mcp_servers():
    from rigsmith.mcp import server
    import asyncio

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert set(tools.registry()) == names
    assert tools.registry()["write_rig"].needs_approval
    assert tools.registry()["list_songs"].group == "songs"


def test_every_role_names_only_real_tools():
    known = set(tools.registry())
    for role in roles.ROLES.values():
        assert not [t for t in role.tools if t not in known], role.name


def test_flattening_drops_nullable_anyof_and_titles():
    schema = {"title": "X", "type": "object", "properties": {"a": {"anyOf": [{"type": "string"}, {"type": "null"}], "title": "A"}}}
    assert tools.flatten(schema) == {"type": "object", "properties": {"a": {"type": "string"}}}


def test_a_local_model_picks_a_chat_model_not_an_embedder():
    transport, _ = scripted([{"role": "assistant", "content": "hi"}])
    assert local(transport).pick_model() == "qwen3-8b"


def test_a_tool_call_round_trips(workspace):
    transport, seen = scripted([call("app_mode"), {"role": "assistant", "content": "You are in accuracy mode."}])
    selected = tools.select(["app_mode"])
    conversation = local(transport).start("system", selected)
    conversation.add_user("which mode?")
    emitted = []
    result = runner.step_loop(conversation, selected, emit=emitted.append, approve=lambda c: True)
    assert result.text == "You are in accuracy mode."
    assert result.calls == [{"name": "app_mode", "arguments": {}, "ok": True}]
    tool_message = seen[1]["messages"][-1]
    assert tool_message["role"] == "tool" and "accuracy" in tool_message["content"]
    assert [e["kind"] for e in emitted if e["kind"] != "status"] == ["tool_call", "tool_result", "text"]


@pytest.mark.parametrize(
    "content",
    [
        '<think>the user wants the mode</think><tool_call>{"name": "app_mode", "arguments": {}}</tool_call>',
        "<tool_call><function=app_mode></function></tool_call>",
        '```json\n{"name": "app_mode", "arguments": {}}\n```',
    ],
)
def test_qwen_tool_calls_leaked_into_text_still_run(workspace, content):
    transport, _ = scripted([{"role": "assistant", "content": content}, {"role": "assistant", "content": "done"}])
    selected = tools.select(["app_mode"])
    conversation = local(transport).start("system", selected)
    conversation.add_user("mode?")
    result = runner.step_loop(conversation, selected, emit=lambda e: None, approve=lambda c: True)
    assert result.calls and result.calls[0]["name"] == "app_mode"
    assert result.text == "done"


def test_xml_parameters_are_parsed_as_values():
    calls, rest = providers.parse_text_calls(
        "ok <tool_call><function=show_song><parameter=song_id>going-under</parameter></function></tool_call>", {"show_song"}
    )
    assert calls[0].arguments == {"song_id": "going-under"} and rest == "ok"


def test_a_name_that_is_not_a_tool_is_not_executed():
    calls, _ = providers.parse_text_calls('<tool_call>{"name": "rm_rf", "arguments": {}}</tool_call>', {"app_mode"})
    assert calls == []


def test_think_blocks_never_reach_the_answer():
    reasoning, answer = providers.split_reasoning("<think>hmm</think>The answer.")
    assert reasoning == "hmm" and answer == "The answer."
    reasoning, answer = providers.split_reasoning("still thinking</think>Answer")
    assert reasoning == "still thinking" and answer == "Answer"


def test_a_device_write_waits_for_approval_and_a_no_stops_it(workspace):
    transport, seen = scripted([call("write_rig", {"draft_id": "x"}), {"role": "assistant", "content": "OK, I won't."}])
    selected = tools.select(["write_rig"])
    conversation = local(transport).start("system", selected)
    conversation.add_user("write it")
    asked = []
    result = runner.step_loop(conversation, selected, emit=lambda e: None, approve=lambda c: asked.append(c.name) or False)
    assert asked == ["write_rig"]
    assert "declined" in seen[1]["messages"][-1]["content"]
    assert result.calls == []


def test_the_chat_streams_and_waits_for_a_person(workspace, monkeypatch):
    transport, _ = scripted([call("write_rig", {"draft_id": "x"}), {"role": "assistant", "content": "Not written."}])
    monkeypatch.setattr(providers, "from_settings", lambda *a, **k: local(transport))
    published = []
    events.subscribe(published.append)

    # The rig builder has write_rig; the assistant is never handed it at all.
    made = chat.create(role="rig-builder")
    chat.send(made.chat_id, "write my rig")
    deadline = time.time() + 10
    while not chat.pending_approvals(made.chat_id) and time.time() < deadline:
        time.sleep(0.05)
    pending = chat.pending_approvals(made.chat_id)
    assert pending and pending[0]["tool"] == "write_rig"
    chat.decide(pending[0]["approval_id"], False)
    while chat.get(made.chat_id).busy and time.time() < deadline:
        time.sleep(0.05)
    final = chat.get(made.chat_id)
    assert not final.busy
    assert any(e.get("text") == "Not written." for e in final.transcript)
    assert any(p.get("kind") == "approval_request" for p in published)
    assert final.title == "write my rig"


def test_a_dispatched_agent_reports_back(workspace, monkeypatch):
    transport, _ = scripted([call("list_songs"), {"role": "assistant", "content": "There are no songs yet."}])
    monkeypatch.setattr(providers, "from_settings", lambda *a, **k: local(transport))
    message = chat.dispatch("song-mapper", "list the songs")
    run_id = message.split("run ")[1].split(".")[0]
    deadline = time.time() + 10
    while chat.agent_run(run_id)["state"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert chat.agent_result(run_id) == "done: There are no songs yet."


def test_the_assistant_is_not_handed_device_writes(workspace, monkeypatch):
    transport, _ = scripted([call("write_rig", {"draft_id": "x"}), {"role": "assistant", "content": "I can't do that here."}])
    monkeypatch.setattr(providers, "from_settings", lambda *a, **k: local(transport))
    made = chat.create()
    chat.send(made.chat_id, "write my rig")
    deadline = time.time() + 10
    while chat.get(made.chat_id).busy and time.time() < deadline:
        time.sleep(0.05)
    results = [e for e in chat.get(made.chat_id).transcript if e.get("kind") == "tool_result"]
    assert results and "no tool called 'write_rig'" in results[0]["text"]


def test_local_mode_will_not_hand_a_chat_to_claude(workspace):
    appsettings.update(mode="local", local_llm={"provider": "claude-code"})
    with pytest.raises(Invalid):
        providers.from_settings()


def test_online_tools_refuse_in_local_mode(workspace):
    appsettings.update(mode="local")
    outcome = tools.call("search_song", {"query": "going under"})
    assert outcome.refused and not outcome.ok

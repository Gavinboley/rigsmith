"""The in-app agents, against a scripted model server.

No real model runs here: a mock OpenAI-compatible endpoint replays the
responses a local Qwen would give - proper tool_calls, tool calls leaked
into the text in either of Qwen's formats, think blocks - so the loop around
them is what gets tested.
"""

from __future__ import annotations

import json
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


def test_the_song_mapper_can_read_a_parts_notes_and_ask_for_another_reading(workspace, monkeypatch):
    """The notes view and a part's reading are the mapper's to use: it reads
    a rendered part's notes, and the re-render offers the readings by name."""
    from rigsmith.core import songs, takes
    from rigsmith.core.songs import Part
    from rigsmith.learn.synth import NoteEvent

    # What a local model is handed: the readings by name.
    spec = tools.flatten(tools.registry()["render_song_part_di"].schema)["properties"]["reading"]
    assert set(spec.get("enum", [])) >= {"auto", "mono", "poly", "arpeggio"}

    made = songs.create("Picked")
    made.parts = [Part("intro-1-stem", 0, "intro", "stem", 0.0, 4.0)]
    made.save()
    wav = workspace / "intro-di.wav"
    import numpy as np
    import soundfile

    soundfile.write(str(wav), np.zeros(4800), 48000)
    events = [NoteEvent(start=0.25 * i, duration=0.4, midi=52.0 + (i % 4) * 4) for i in range(8)]
    wav.with_suffix(".notes.json").write_text(json.dumps({"source": "transcribed", "events": [e.to_dict() for e in events]}), encoding="utf-8")
    take = takes.add(wav, take_id="intro-di", kind="di", origin="synth", copy=False)
    song = songs.get(made.song_id)
    song.part("intro-1-stem").di_take = take.take_id
    song.part("intro-1-stem").heard_as = "polyphonic"
    song.save()

    transport, seen = scripted([
        call("song_part_notes", {"song_id": made.song_id, "part_id": "intro-1-stem"}),
        {"role": "assistant", "content": "Read as chords, but it is single picks - render it as an arpeggio."},
    ])
    monkeypatch.setattr(providers, "from_settings", lambda *a, **k: local(transport))
    message = chat.dispatch("song-mapper", "check the intro's notes")
    run_id = message.split("run ")[1].split(".")[0]
    deadline = time.time() + 10
    while chat.agent_run(run_id)["state"] == "running" and time.time() < deadline:
        time.sleep(0.05)
    assert "arpeggio" in chat.agent_result(run_id)
    tool_answers = [m for m in seen[-1]["messages"] if m["role"] == "tool"]
    assert tool_answers and "8 notes from transcribed, heard as polyphonic, E3 to E4" in tool_answers[0]["content"]


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


def streamed(chunks):
    """A model server that streams its reply as server-sent events."""
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-8b"}]})
        assert json.loads(request.content)["stream"] is True
        body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=body, headers={"content-type": "text/event-stream"})

    return httpx.MockTransport(handler)


def test_a_streamed_reply_shows_as_it_is_written_without_its_thinking():
    def delta(**kw):
        return {"choices": [{"delta": kw, "finish_reason": None}]}

    chunks = [delta(content="<think>hmm"), delta(content=" ok</think>The verse "), delta(content="is a Plexi"), delta(content=" <tool_"), delta(content='call>{"name": "show_song", "arguments": {"song_id": "s"}}</tool_call>'),
              {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 9}}]
    seen = []
    conversation = local(streamed(chunks)).start("system", tools.select(["show_song"]))
    conversation.add_user("which amp?")
    turn = conversation.step(on_partial=seen.append)
    assert seen and seen[-1] == "The verse is a Plexi"
    assert not any("<" in text or "hmm" in text for text in seen)
    assert turn.text == "The verse is a Plexi" and turn.calls[0].name == "show_song"
    assert turn.reasoning.startswith("hmm") and turn.usage["completion_tokens"] == 9


def test_streamed_tool_calls_are_put_back_together():
    pieces = [
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c9", "function": {"name": "list_songs", "arguments": ""}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"q":'}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "x"}'}}]}, "finish_reason": "tool_calls"}]},
    ]
    events_seen = []
    conversation = local(streamed(pieces)).start("system", tools.select(["list_songs"]))
    conversation.add_user("songs?")
    turn = runner._step(conversation, events_seen.append)
    assert turn.calls[0].id == "c9" and turn.calls[0].arguments == {"q": "x"}
    assert providers.visible_so_far("Plexi <tool_ca") == "Plexi" and providers.visible_so_far("a <b") == "a <b"


def _fake_anthropic(monkeypatch, final, events):
    """Just enough of the SDK's shape for the conversation to run against."""
    import sys
    import types

    module = types.ModuleType("anthropic")
    for name in ("AuthenticationError", "RateLimitError", "APIStatusError", "APIConnectionError"):
        setattr(module, name, type(name, (Exception,), {}))
    sent = []

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            return iter(events)

        def get_final_message(self):
            return final

    class Messages:
        def stream(self, **params):
            sent.append(params)
            return Stream()

        def create(self, **params):
            sent.append(params)
            return final

    module.Anthropic = lambda: types.SimpleNamespace(beta=types.SimpleNamespace(messages=Messages()))
    monkeypatch.setitem(sys.modules, "anthropic", module)
    return sent


def test_claude_streams_and_a_fallback_is_echoed_back_cleanly(monkeypatch):
    from types import SimpleNamespace as B

    content = [
        B(type="thinking", thinking="first try"),
        B(type="text", text="The verse is "),
        B(type="tool_use", id="t0", name="show_song", input={}),
        B(type="fallback"),
        B(type="text", text="a Plexi."),
        B(type="tool_use", id="t1", name="list_songs", input={"q": "x"}),
    ]
    final = B(content=content, stop_reason="tool_use", usage=B(input_tokens=5, output_tokens=7))
    sent = _fake_anthropic(monkeypatch, final, [B(type="text", text="The verse is "), B(type="message_delta"), B(type="text", text="a Plexi.")])
    conversation = providers.AnthropicAPI(model="claude-opus-5-5").start("system", tools.select(["show_song", "list_songs"]))
    conversation.add_user("which amp?")
    seen = []
    turn = conversation.step(on_partial=seen.append)

    assert seen[-1] == "The verse is a Plexi."
    assert sent[0]["fallbacks"] == "default" and sent[0]["betas"] == ["server-side-fallback-2026-07-01"]
    # Before the boundary only text goes back; the declined attempt's tool call is not run.
    echoed = [b.type for b in conversation.messages[-1]["content"]]
    assert echoed == ["text", "fallback", "text", "tool_use"]
    assert [c.name for c in turn.calls] == ["list_songs"]
    assert providers.echoable(content[1:3]) == content[1:3]  # no fallback: untouched


def test_a_signed_out_claude_code_is_seen_and_passed_over(tmp_path, monkeypatch):
    """Installed but signed out answered nothing, and accuracy mode still chose it."""
    import sys

    from rigsmith.core import appsettings, setup

    fake = tmp_path / "fake_claude.py"
    fake.write_text(
        "import json, sys\n"
        "print(json.dumps({'loggedIn': False, 'authMethod': 'none'}))\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setattr(providers, "claude_code_command", lambda: [sys.executable, str(fake)])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    providers._signed_in_cache.clear()
    assert providers.claude_code_signed_in() is False
    appsettings.update(mode="accuracy")
    assert not isinstance(providers.from_settings(), providers.ClaudeCode), "signed out: the next in line answers"
    step = next(s for s in setup.checklist() if s.step_id == "brains")
    assert "not signed in" in step.detail and "/login" in step.detail


def test_every_tool_that_acts_on_the_hardware_or_deletes_work_waits_for_a_person():
    """Approval is a list, so a new tool is ungated until it is added - this
    makes adding one that acts on the Core, or deletes work, fail here first."""
    acts = ("midi_", "write_", "install_")
    reads = {"midi_ports"}
    deletes = {"delete_draft", "delete_take", "delete_recipe", "workspace_prune"}
    ungated = [
        name for name, spec in tools.registry().items()
        if (name.startswith(acts) and name not in reads or name in deletes) and not spec.needs_approval
    ]
    assert not ungated, f"these act on the device or delete work without asking: {ungated}"


def test_a_second_answer_to_an_approval_does_not_overturn_the_first():
    import threading

    chat._approvals["twice"] = {"approval_id": "twice", "chat_id": "c", "tool": "write_rig", "decision": None, "event": threading.Event()}
    try:
        chat.decide("twice", False)
        assert chat.decide("twice", True)["decision"] is False
    finally:
        chat._approvals.pop("twice", None)


def test_a_local_model_gets_a_set_it_can_choose_from():
    """Small models choose less reliably past twenty or so tools."""
    from rigsmith.agent import modelcheck

    registry = tools.registry()
    for role in roles.ROLES.values():
        local = runner.toolset(role, allow_writes=False, local=True)
        assert len(local) <= 22, (role.name, len(local))
        assert all(name in registry for name in role.local_tools or []), role.name
    assistant = {t.name for t in runner.toolset(roles.get("assistant"), allow_writes=False, local=True)}
    assert {q.expect for q in modelcheck.QUESTIONS} <= assistant, "the model check asks for what a local model is given"
    builder = {t.name: t for t in runner.toolset(roles.get("rig-builder"), allow_writes=False, local=True)}
    assert builder["write_rig"].needs_approval, "a smaller set is not a looser one"


def test_an_unclosed_think_block_is_all_thinking():
    """Cut off by the token limit, a <think> block never closes; its text is
    not the answer, and a tool call rehearsed in it is not run."""
    leaked = '<think>maybe I should call <tool_call>{"name": "write_rig", "arguments": {"draft_id": "x"}}</tool_call>'
    reasoning, answer = providers.split_reasoning("Here is what I found." + leaked)
    assert answer == "Here is what I found." and "write_rig" in reasoning
    calls, _ = providers.parse_text_calls(answer, {"write_rig"})
    assert calls == []


def test_tool_arguments_that_are_not_an_object_do_not_break_the_chat(workspace):
    """Arguments of [1, 2, 3] crashed the tool loop after the call was in the
    history, so no tool reply followed it and every later turn was refused."""
    odd = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "app_mode", "arguments": "[1, 2, 3]"}}]}
    transport, seen = scripted([odd, {"role": "assistant", "content": "Done."}])
    selected = tools.select(["app_mode"])
    conversation = local(transport).start("system", selected)
    conversation.add_user("which mode?")
    result = runner.step_loop(conversation, selected, emit=lambda e: None, approve=lambda c: True)
    assert result.text == "Done."
    history = seen[1]["messages"]
    assert history[-2]["role"] == "assistant" and history[-2]["tool_calls"][0]["id"] == "c1"
    assert history[-1]["role"] == "tool" and history[-1]["tool_call_id"] == "c1", "the call is answered"


def test_a_tool_that_raises_still_answers_its_call(workspace, monkeypatch):
    from rigsmith.agent import tools as registry

    def boom(name, arguments):
        raise RuntimeError("the tool fell over")

    monkeypatch.setattr(registry, "call", boom)
    transport, seen = scripted([call("app_mode"), {"role": "assistant", "content": "Sorry."}])
    selected = tools.select(["app_mode"])
    conversation = local(transport).start("system", selected)
    conversation.add_user("which mode?")
    result = runner.step_loop(conversation, selected, emit=lambda e: None, approve=lambda c: True)
    assert result.calls[0]["ok"] is False and result.text == "Sorry."
    assert seen[1]["messages"][-1]["role"] == "tool" and "fell over" in seen[1]["messages"][-1]["content"]


def test_stop_stops_a_local_model_mid_reply():
    """Stop was only heard between rounds: a local model writing for minutes
    kept going until it finished."""
    def delta(**kw):
        return {"choices": [{"delta": kw, "finish_reason": None}]}

    chunks = [delta(content=f"word{i} ") for i in range(50)]
    asked = {"n": 0}

    def cancelled():
        asked["n"] += 1
        return asked["n"] > 5

    conversation = local(streamed(chunks)).start("system", [])
    conversation.add_user("talk")
    result = runner.step_loop(conversation, [], emit=lambda e: None, approve=lambda c: True, cancelled=cancelled)
    assert result.stopped == "cancelled" and not result.text


def test_qwen_4_is_picked_ahead_of_an_older_qwen_beside_it():
    import httpx

    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "qwen3-8b"}, {"id": "qwen4-27b-instruct"}, {"id": "text-embedding-qwen3"}]})

    assert local(httpx.MockTransport(handler)).pick_model() == "qwen4-27b-instruct"


class FakeClaude:
    """A Claude Code process: given lines of stream-json, then (optionally) silence."""

    def __init__(self, lines, *, hang=False):
        import io
        import threading

        self.args = None
        self.terminated = threading.Event()
        self._lines, self._hang = lines, hang
        self.stdin = io.StringIO()
        self.stderr = iter([])
        self.returncode = None

    @property
    def stdout(self):
        for line in self._lines:
            yield line + "\n"
        if self._hang:
            self.terminated.wait(10)

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated.set()
        self.returncode = -15

    kill = terminate

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


def _claude(monkeypatch, process, tmp_path):
    seen = {}

    def popen(args, **kwargs):
        seen["args"] = args
        return process

    monkeypatch.setattr(providers.subprocess, "Popen", popen)
    claude = providers.ClaudeCode.__new__(providers.ClaudeCode)
    claude.command, claude.model, claude.max_budget_usd = ["claude"], None, None
    return claude, seen


def test_claude_code_refuses_prompts_and_its_tool_server_gets_no_secrets(monkeypatch, tmp_path):
    import json as _json

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "C:/Windows")
    process = FakeClaude(['{"type": "result", "subtype": "success", "result": "hi", "session_id": "s1"}'])
    claude, seen = _claude(monkeypatch, process, tmp_path)
    out = claude.run("hello", system_append="", allowed=["mcp__rigsmith__app_mode"], directory=tmp_path)
    assert out["text"] == "hi"
    args = seen["args"]
    assert args[args.index("--permission-prompts") + 1] == "none", "a prompt nobody answers would wait for ever"
    env = _json.loads((tmp_path / "claude-mcp.json").read_text())["mcpServers"]["rigsmith"]["env"]
    assert env.get("PATH") == "C:/Windows" and "RIGSMITH_WORKSPACE" in env and "ANTHROPIC_API_KEY" not in env


def test_a_silent_claude_code_is_stopped_not_waited_on_for_ever(monkeypatch, tmp_path):
    monkeypatch.setattr(providers, "CLAUDE_QUIET_LIMIT_S", 1)
    process = FakeClaude(['{"type": "system", "subtype": "init", "session_id": "s1"}'], hang=True)
    claude, _ = _claude(monkeypatch, process, tmp_path)
    out = claude.run("hello", system_append="", allowed=[], directory=tmp_path)
    assert out["error"] == "timeout" and process.terminated.is_set()


def test_claude_code_is_stopped_when_handling_its_output_fails(monkeypatch, tmp_path):
    process = FakeClaude(['{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}'], hang=True)
    claude, _ = _claude(monkeypatch, process, tmp_path)

    def boom(event):
        raise RuntimeError("the page went away")

    with pytest.raises(RuntimeError):
        claude.run("hello", system_append="", allowed=[], directory=tmp_path, on_event=boom)
    assert process.terminated.is_set(), "no Claude Code left running behind"


def test_a_chat_whose_claude_session_is_gone_starts_a_fresh_one(monkeypatch, tmp_path):
    from rigsmith.agent import roles
    from rigsmith.core.errors import Unavailable

    asked = []

    class Provider(providers.ClaudeCode):
        def __init__(self):
            pass

        def run(self, prompt, *, resume=None, **kw):
            asked.append(resume)
            if resume:
                raise Unavailable("Claude Code exited with 1", hint="No conversation found with session ID: old")
            return {"text": "fresh", "session_id": "new", "turns": 1}

    result = runner.claude_code_exchange(Provider(), roles.get("assistant"), "hi", emit=lambda e: None, directory=tmp_path, resume="old", allow_writes=False, local=False)
    assert asked == ["old", None] and result.text == "fresh" and result.session_id == "new"

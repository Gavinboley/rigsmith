"""The model check: does a local model reach for the right rigsmith tool?

Against a fake OpenAI-compatible server, so the scoring is tested rather
than any particular model. Nothing the questions name is ever run.
"""

from __future__ import annotations

import json

import httpx
import pytest

from rigsmith import config
from rigsmith.agent import modelcheck
from rigsmith.agent.providers import OpenAICompatible


@pytest.fixture(autouse=True)
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.setenv("RIGSMITH_MODE", "local")
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def server(answer):
    """A chat-completions endpoint whose reply to each question is ``answer(question)``."""

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        question = next(m["content"] for m in reversed(body["messages"]) if m["role"] == "user")
        assert body.get("tools"), "the exam is about tools, so they must be offered"
        name, arguments, text = answer(question)
        message = {"role": "assistant", "content": text}
        if name:
            message["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
        return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}], "usage": {}})

    return OpenAICompatible("http://exam.local/v1", model="qwen3-8b", transport=httpx.MockTransport(handle))


def perfect(question: str):
    lowered = question.lower()
    if "songs have i" in lowered:
        return "list_songs", {}, None
    if "plexi" in lowered:
        return "search_models", {"query": "plexi"}, None
    if "set up next" in lowered:
        return "setup_status", {}, None
    if "going-under" in lowered:
        return "show_song", {"song_id": "going-under-evanescence"}, None
    if "cherub-rock" in lowered:
        return "explain_rig", {"draft_id": "cherub-rock"}, None
    return "workspace_status", {}, None


def test_a_model_that_calls_every_right_tool_passes():
    check = modelcheck.run(server(perfect))
    assert check.passed == len(modelcheck.QUESTIONS) == 6
    assert "reliably" in check.verdict() and check.model == "qwen3-8b"
    assert "6 of 6 right" in check.describe()


def test_words_wrong_tools_and_wrong_arguments_are_told_apart():
    def sloppy(question: str):
        lowered = question.lower()
        if "songs have i" in lowered:
            return None, {}, "You have two songs, I think."
        if "plexi" in lowered:
            return "list_songs", {}, None
        if "cherub-rock" in lowered:
            return "explain_rig", {"draft_id": "cherub"}, None
        return perfect(question)

    check = modelcheck.run(server(sloppy))
    notes = {answer.expected: answer.note for answer in check.answers if not answer.ok}
    assert notes["list_songs"] == "answered in words instead"
    assert notes["search_models"] == "the wrong tool"
    assert notes["explain_rig"].startswith("right tool, wrong arguments")
    assert check.passed == 3 and "not reliable" in check.verdict()


def test_a_call_written_as_text_still_counts():
    """Qwen's XML form, returned as plain text by a server whose parser missed it."""

    def as_text(question: str):
        name, arguments, _ = perfect(question)
        params = "".join(f"<parameter={k}>{v}</parameter>" for k, v in arguments.items())
        return None, {}, f"<tool_call><function={name}>{params}</function></tool_call>"

    assert modelcheck.run(server(as_text)).passed == 6


def test_the_page_can_run_it_as_a_job(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rigsmith.agent import providers
    from rigsmith.core.jobs import get_manager
    from rigsmith.web.server import build_app

    fake = server(perfect)
    monkeypatch.setattr(providers, "OpenAICompatible", lambda *a, **k: fake)
    client = TestClient(build_app())
    job = client.post("/api/settings/model-check", json={}).json()
    running = get_manager().get(job["job_id"])
    assert running.wait(30), "the check never finished"
    assert running.result and running.result["passed"] == 6, running.error

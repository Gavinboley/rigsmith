"""Who answers: Claude (through Claude Code or the API) or a local model.

Three providers, one shape. Each keeps its own conversation in its own
native format - an OpenAI-style message list, Anthropic content blocks - and
the runner only ever sees a :class:`Turn`: some text, maybe some reasoning,
maybe some tool calls. Keeping the native history matters for Claude in
particular: its responses are appended back exactly as they came, thinking
blocks and all, because a reworded history invalidates them.

* :class:`OpenAICompatible` - LM Studio (port 1234), Ollama (11434), llama.cpp
  (8080), vLLM. The local-mode provider. Qwen models leak tool calls into
  plain text in two different formats depending on generation - Hermes-style
  ``<tool_call>{json}</tool_call>`` up to Qwen3, an XML
  ``<function=...><parameter=...>`` form from Qwen3-Coder and Qwen3.5 on - and
  ``<think>`` blocks into content; all three are handled here so the runner
  never sees them.
* :class:`AnthropicAPI` - the Messages API through the official SDK
  (``pip install anthropic``), with the server-side refusal fallback on.
* :class:`ClaudeCode` - Claude Code headless, with rigsmith's MCP server and
  web search. Not a step-at-a-time provider: Claude Code runs its own tool
  loop, and its stream of events is relayed as it happens. This is the
  accuracy-mode default, because it brings Claude's research tools with it.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field

from ..core.errors import Invalid, Unavailable

#: Local models in rough order of how well they drive ~20 tools, best first.
#: Matched against whatever the server says it has loaded. Qwen 4's
#: open-weight model was announced in September 2026 and not out by mid
#: October; when it is loaded, it goes first, ahead of any older Qwen
#: beside it (unmeasured, but each generation so far has called tools
#: better than the last).
LOCAL_PREFERENCE = (
    "qwen4", "qwen-4", "qwen3.8-27b", "qwen3.6-27b", "qwen3.5-27b", "qwen3.6-35b-a3b", "qwen3.5-35b-a3b",
    "qwen3-30b-a3b-instruct-2507", "qwen3-30b-a3b-thinking-2507", "qwen3-30b-a3b",
    "qwen3-coder-30b", "qwen3-32b", "qwen3-14b", "qwen3.5-9b", "qwen3-8b",
    "qwen3-4b-instruct-2507", "qwen3-4b", "qwen",
)

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"

#: How long Claude Code may say nothing - a slow tool runs silently - before
#: the exchange is taken to have hung and is stopped.
CLAUDE_QUIET_LIMIT_S = 900

#: The environment rigsmith's tool server is started with when Claude Code
#: launches it: enough for Python to start and find home and temp on any
#: system, and nothing secret.
_SERVER_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "HOME", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL", "PYTHONPATH", "PYTHONUTF8",
    "VIRTUAL_ENV",
)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Turn:
    text: str = ""
    reasoning: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    stop: str = ""
    usage: dict = field(default_factory=dict)


# --------------------------------------------------------------------------
# local models
# --------------------------------------------------------------------------

_THINK = re.compile(r"<think>(.*?)</think>", re.S)
_HERMES = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_XML_CALL = re.compile(r"<tool_call>\s*<function=([\w.-]+)>(.*?)</function>\s*</tool_call>", re.S)
_XML_PARAM = re.compile(r"<parameter=([\w.-]+)>\s*(.*?)\s*</parameter>", re.S)
_FENCED = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def split_reasoning(text: str) -> tuple[str, str]:
    """Separate ``<think>`` blocks (and an unclosed leading one) from the answer."""
    reasoning = "\n".join(m.strip() for m in _THINK.findall(text or ""))
    answer = _THINK.sub("", text or "")
    if "</think>" in answer:  # an opening tag the template already consumed
        head, _, answer = answer.partition("</think>")
        reasoning = (reasoning + "\n" + head).strip()
    if "<think>" in answer:
        # Opened and never closed - cut off by the token limit, or forgotten.
        # All of it is thinking: shown as the answer it would leak, and a tool
        # call the model only rehearsed there would be run.
        answer, _, unclosed = answer.partition("<think>")
        reasoning = (reasoning + "\n" + unclosed).strip()
    return reasoning.strip(), answer.strip()


#: Where a reply stops being prose and starts being a tool call written as
#: text - the live view hides everything from here on.
_CALL_START = ("<tool_call", "<function=", "<tools>")


def visible_so_far(text: str) -> str:
    """What of a reply still being written can be shown: no ``<think>`` -
    finished or not - and nothing from a text tool call onwards."""
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # the template opened it; the model closed it
        text = text.partition("</think>")[2]
    if "<think>" in text:  # still thinking
        text = text.partition("<think>")[0]
    cut = min((text.find(marker) for marker in _CALL_START if marker in text), default=-1)
    if cut >= 0:
        text = text[:cut]
    # A marker half-written at the very end ("<tool_") is not prose either.
    tail = text.rfind("<")
    if tail >= 0 and any(marker.startswith(text[tail:]) for marker in _CALL_START + ("<think>",)):
        text = text[:tail]
    return text.strip()


def _coerce(value: str):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


def parse_text_calls(text: str, known: set[str]) -> tuple[list[ToolCall], str]:
    """Tool calls a model wrote into its text instead of the tool_calls field.

    Returns the calls and the text with them removed. Only names that are
    real tools count, so a model musing about JSON is not executed.
    """
    calls: list[ToolCall] = []
    remaining = text or ""
    for match in _XML_CALL.finditer(remaining):
        name = match.group(1)
        if name in known:
            arguments = {k: _coerce(v) for k, v in _XML_PARAM.findall(match.group(2))}
            calls.append(ToolCall(f"call_{uuid.uuid4().hex[:8]}", name, arguments))
    remaining = _XML_CALL.sub("", remaining)
    for match in _HERMES.finditer(remaining):
        try:
            data = json.loads(match.group(1))
        except ValueError:
            continue
        name = data.get("name")
        if name in known:
            arguments = data.get("arguments") or data.get("parameters") or {}
            if isinstance(arguments, str):
                arguments = _coerce(arguments) if arguments.strip() else {}
            calls.append(ToolCall(f"call_{uuid.uuid4().hex[:8]}", name, arguments if isinstance(arguments, dict) else {}))
    remaining = _HERMES.sub("", remaining)
    if not calls:
        for match in _FENCED.finditer(remaining):
            try:
                data = json.loads(match.group(1))
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("name") in known and isinstance(data.get("arguments", {}), dict):
                calls.append(ToolCall(f"call_{uuid.uuid4().hex[:8]}", data["name"], data.get("arguments") or {}))
        if calls:
            remaining = _FENCED.sub("", remaining)
    return calls, remaining.strip()


class OpenAICompatible:
    """Any server speaking the OpenAI chat-completions API with tools."""

    name = "openai-compatible"

    def __init__(self, base_url: str, *, model: str = "", api_key: str | None = None, temperature: float = 0.3, thinking: bool = False, timeout: float = 600.0, transport=None) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.thinking = thinking
        self.timeout = timeout
        self.transport = transport

    def _client(self):
        import httpx

        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        return httpx.Client(timeout=self.timeout, headers=headers, transport=self.transport)

    def _listening(self) -> bool:
        """Whether anything is listening, found out quickly.

        Windows retries a refused connection to localhost for about two
        seconds, which made every status check stall when no server was
        running. A bare socket with a short timeout answers in milliseconds
        either way - a running local server accepts at once.
        """
        import socket
        from urllib.parse import urlparse

        parsed = urlparse(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        local = host in ("127.0.0.1", "localhost", "::1")
        try:
            with socket.create_connection((host, port), timeout=0.35 if local else 1.5):
                return True
        except OSError:
            return False

    def models(self, *, quick: bool = False) -> list[str]:
        if quick and self.transport is None and not self._listening():
            raise Unavailable(
                f"no model server answering at {self.base_url}",
                hint="Start LM Studio's server (port 1234), Ollama (11434) or llama-server "
                "(8080), or set the URL in Settings.",
            )
        try:
            with self._client() as client:
                response = client.get(f"{self.base_url}/models", timeout=5.0)
                response.raise_for_status()
                return [m.get("id", "") for m in response.json().get("data", []) if m.get("id")]
        except Exception as exc:
            raise Unavailable(
                f"no model server answering at {self.base_url}",
                hint="Start LM Studio's server (port 1234), Ollama (11434) or llama-server "
                "(8080), or set the URL in Settings.",
            ) from exc

    def pick_model(self) -> str:
        if self.model:
            return self.model
        available = self.models()
        if not available:
            raise Unavailable(f"the server at {self.base_url} has no model loaded")
        chat = [m for m in available if "embed" not in m.lower()]
        lowered = {m.lower(): m for m in chat or available}
        for preferred in LOCAL_PREFERENCE:
            for key, original in lowered.items():
                if preferred in key:
                    self.model = original
                    return original
        self.model = (chat or available)[0]
        return self.model

    def describe(self) -> str:
        return f"local model {self.model or '(auto)'} at {self.base_url}"

    def start(self, system: str, tools) -> "OpenAIConversation":
        return OpenAIConversation(self, system, tools)


class Stopped(Exception):
    """The user stopped the turn while the model was still writing."""


def _collect_stream(lines, on_partial, cancelled=None) -> dict:
    """Server-sent chat-completion chunks back into one response body.

    Content and reasoning are concatenated; tool calls arrive in pieces keyed
    by index (the name once, the arguments a few characters at a time). The
    visible text is reported at most every 80 ms, so a fast model does not
    flood the page.
    """
    import time

    content, reasoning = [], []
    calls: dict[int, dict] = {}
    finish, usage = "", {}
    shown, last = "", 0.0
    for line in lines:
        if cancelled and cancelled():
            raise Stopped()
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:
            continue
        usage = chunk.get("usage") or usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                content.append(delta["content"])
            for key in ("reasoning_content", "reasoning"):
                if delta.get(key):
                    reasoning.append(delta[key])
            for piece in delta.get("tool_calls") or []:
                slot = calls.setdefault(int(piece.get("index", len(calls))), {"id": "", "name": "", "arguments": ""})
                slot["id"] = piece.get("id") or slot["id"]
                function = piece.get("function") or {}
                slot["name"] += function.get("name") or ""
                slot["arguments"] += function.get("arguments") or ""
            finish = choice.get("finish_reason") or finish
        if on_partial and content:
            visible = visible_so_far("".join(content))
            now = time.monotonic()
            if visible != shown and now - last >= 0.08:
                shown, last = visible, now
                on_partial(visible)
    if on_partial:
        visible = visible_so_far("".join(content))
        if visible != shown:
            on_partial(visible)
    message = {"content": "".join(content), "reasoning_content": "".join(reasoning)}
    if calls:
        message["tool_calls"] = [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
            for _, c in sorted(calls.items())
        ]
    return {"choices": [{"message": message, "finish_reason": finish}], "usage": usage}


class OpenAIConversation:
    def __init__(self, provider: OpenAICompatible, system: str, tools) -> None:
        self.provider = provider
        self.tools = list(tools)
        self.known = {t.name for t in self.tools}
        note = "" if provider.thinking else "\n/no_think"
        self.messages: list[dict] = [{"role": "system", "content": system + note}]

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def step(self, on_partial=None, cancelled=None) -> Turn:
        """One model turn. With ``on_partial``, the reply is streamed and the
        callback gets the visible text so far as it grows - a local model
        writing for a minute should not look like a hung one. ``cancelled``
        is asked between chunks, so Stop stops the model mid-reply."""
        model = self.provider.pick_model()
        body = {
            "model": model,
            "messages": self.messages,
            "temperature": self.provider.temperature,
            "stream": bool(on_partial),
        }
        if on_partial:
            body["stream_options"] = {"include_usage": True}
        if self.tools:
            body["tools"] = [t.openai(compact=True) for t in self.tools]
            body["tool_choice"] = "auto"
        # Honoured by vLLM and llama.cpp's Jinja templates; ignored elsewhere.
        body["chat_template_kwargs"] = {"enable_thinking": bool(self.provider.thinking)}
        try:
            with self.provider._client() as client:
                with client.stream("POST", f"{self.provider.base_url}/chat/completions", json=body) as response:
                    if response.status_code >= 400:
                        response.read()
                        raise Unavailable(f"the model server refused the request ({response.status_code}): {response.text[:300]}")
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        data = _collect_stream(response.iter_lines(), on_partial, cancelled)
                    else:  # a server (or a test) that answered in one piece
                        response.read()
                        data = response.json()
        except (Unavailable, Stopped):
            raise
        except Exception as exc:
            raise Unavailable(f"the model server at {self.provider.base_url} failed: {exc}") from exc

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        extra_reasoning, text = split_reasoning(message.get("content") or "")
        reasoning = "\n".join(filter(None, [reasoning, extra_reasoning]))

        calls: list[ToolCall] = []
        for raw in message.get("tool_calls") or []:
            function = raw.get("function") or {}
            arguments = function.get("arguments") or "{}"
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments.strip() else {}
                except ValueError:
                    arguments = {"_unparsed": arguments}
            if not isinstance(arguments, dict):
                # Valid JSON but not an object - a list, a bare string. A tool
                # takes named arguments; the tool's own error says so.
                arguments = {"_unparsed": arguments}
            calls.append(ToolCall(raw.get("id") or f"call_{uuid.uuid4().hex[:8]}", function.get("name", ""), arguments))
        if not calls:
            calls, text = parse_text_calls(text, self.known)

        assistant: dict = {"role": "assistant", "content": text}
        if calls:
            assistant["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in calls
            ]
        # Qwen's guidance for multi-turn tool use: carry the reasoning forward.
        if reasoning and self.provider.thinking:
            assistant["reasoning_content"] = reasoning
        self.messages.append(assistant)
        return Turn(text=text, reasoning=reasoning, calls=calls, stop=choice.get("finish_reason") or "", usage=data.get("usage") or {})

    def add_results(self, results) -> None:
        for call, text, is_error in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": ("ERROR: " if is_error else "") + text})

    def history(self) -> list[dict]:
        return self.messages


# --------------------------------------------------------------------------
# Claude through the Messages API
# --------------------------------------------------------------------------


class AnthropicAPI:
    """Claude through the official SDK. Optional: ``pip install anthropic``."""

    name = "anthropic"

    def __init__(self, *, model: str = "", effort: str = "high") -> None:
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise Unavailable(
                "the Anthropic SDK is not installed",
                hint="pip install 'rigsmith[claude]'  - or use Claude Code, which needs no key.",
            ) from exc
        self.model = model or DEFAULT_CLAUDE_MODEL
        self.effort = effort

    def describe(self) -> str:
        return f"Claude ({self.model}) through the Anthropic API"

    def start(self, system: str, tools) -> "AnthropicConversation":
        return AnthropicConversation(self, system, tools)


def echoable(content) -> list:
    """A reply's blocks as they may be sent back next turn.

    When a stream falls back to another model mid-reply, the declined
    attempt's partial stays in the content, marked off by a ``fallback``
    block. Its text is fine to echo; its thinking and tool calls are not,
    and must be left out - so are server-tool calls without their results,
    and anything unrecognised, before the last boundary. Without a fallback
    block the content goes back untouched.
    """
    blocks = list(content or [])
    marks = [i for i, block in enumerate(blocks) if getattr(block, "type", None) == "fallback"]
    if not marks:
        return blocks
    boundary = marks[-1]
    results = {getattr(b, "tool_use_id", None) for b in blocks[:boundary] if str(getattr(b, "type", "")).endswith("_tool_result")}
    kept = []
    for block in blocks[:boundary]:
        kind = getattr(block, "type", "")
        if kind == "text":
            kept.append(block)
        elif kind == "server_tool_use" and getattr(block, "id", None) in results:
            kept.append(block)
        elif kind.endswith("_tool_result") and kind != "tool_result":
            kept.append(block)
    return kept + blocks[boundary:]


class AnthropicConversation:
    def __init__(self, provider: AnthropicAPI, system: str, tools) -> None:
        import anthropic

        self.provider = provider
        self.client = anthropic.Anthropic()
        self.system = system
        self.tools = [t.anthropic() for t in tools]
        self.messages: list[dict] = []

    def add_user(self, text: str) -> None:
        self.messages.append({"role": "user", "content": text})

    def step(self, on_partial=None, cancelled=None) -> Turn:
        import time

        import anthropic

        params = dict(
            model=self.provider.model,
            max_tokens=16000,
            system=self.system,
            tools=self.tools,
            messages=self.messages,
            output_config={"effort": self.provider.effort},
            # A policy decline re-runs on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        try:
            if on_partial:
                shown, last = "", 0.0
                with self.client.beta.messages.stream(**params) as stream:
                    for event in stream:
                        if cancelled and cancelled():
                            raise Stopped()
                        if getattr(event, "type", "") == "text":
                            shown += event.text
                            now = time.monotonic()
                            if now - last >= 0.08:
                                last = now
                                on_partial(shown.strip())
                    response = stream.get_final_message()
                on_partial(shown.strip())
            else:
                response = self.client.beta.messages.create(**params)
        except anthropic.AuthenticationError as exc:
            raise Unavailable("the Anthropic API rejected the credentials", hint="Set ANTHROPIC_API_KEY, or use Claude Code.") from exc
        except anthropic.RateLimitError as exc:
            raise Unavailable("the Anthropic API is rate limiting; try again shortly") from exc
        except anthropic.APIStatusError as exc:
            raise Unavailable(f"the Anthropic API failed ({exc.status_code}): {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise Unavailable("could not reach the Anthropic API") from exc

        content = echoable(response.content)
        # Appended as returned (thinking blocks must come back unchanged),
        # less what a mid-reply fallback says not to echo.
        self.messages.append({"role": "assistant", "content": content})
        turn = Turn(stop=response.stop_reason or "")
        for block in content:
            if block.type == "text":
                turn.text += block.text
            elif block.type == "thinking":
                turn.reasoning += getattr(block, "thinking", "") or ""
            elif block.type == "tool_use":
                turn.calls.append(ToolCall(block.id, block.name, dict(block.input or {})))
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            turn.text = (turn.text + f"\n\n(Claude declined this{f' - {category}' if category else ''}.)").strip()
        usage = getattr(response, "usage", None)
        if usage is not None:
            turn.usage = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
        return turn

    def add_results(self, results) -> None:
        # Every result for a turn in one user message.
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": call.id, "content": text, "is_error": bool(is_error)}
                    for call, text, is_error in results
                ],
            }
        )

    def history(self) -> list:
        return [
            {"role": m["role"], "content": [b.model_dump(exclude_none=True) if hasattr(b, "model_dump") else b for b in m["content"]] if isinstance(m["content"], list) else m["content"]}
            for m in self.messages
        ]


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------


#: What to do when Claude Code's session on this machine has expired.
LOGIN_HINT = "Claude Code needs signing in again on this machine: run `claude` in a terminal and use /login, then try again."

#: How Claude Code says it has no usable session - its wording varies by
#: version and by whether the token expired or was never there. 2.1.289,
#: signed out, ends its stream with "Failed to authenticate: OAuth session
#: expired and could not be refreshed" (is_error, though subtype "success").
#:
#: Phrases, not fragments: a researcher's failure text can quote a web page
#: that answered 401 or mentions OAuth, and that is not Claude Code being
#: signed out - telling the user to /login would hide the real error.
_LOGIN_SIGNS = (
    "failed to authenticate", "not logged in", "please run /login", "oauth session expired",
    "oauth token has expired", "invalid api key", "authentication_error",
)


def needs_login(text: str | None) -> bool:
    """True when Claude Code's output says it is not signed in."""
    lowered = (text or "").lower()
    return any(sign in lowered for sign in _LOGIN_SIGNS)


_signed_in_cache: dict = {}

#: How long a sign-in check is believed - long enough not to re-run it for
#: every agent dispatched, short enough that a /login shows up promptly.
SIGNED_IN_TTL_S = 60.0


def claude_code_signed_in() -> bool | None:
    """Whether Claude Code on this machine has a session: True, False, or None if it cannot say.

    ``claude auth status`` prints JSON with ``loggedIn`` (and exits 1 when
    signed out). It takes about 0.4 s on Windows, and costs nothing - no
    model is asked anything.
    """
    import time

    command = claude_code_command()
    if not command:
        return None
    cached = _signed_in_cache.get("value")
    if cached is not None and time.monotonic() - _signed_in_cache.get("at", 0.0) < SIGNED_IN_TTL_S:
        return cached[0]
    try:
        done = subprocess.run(command + ["auth", "status"], capture_output=True, text=True, timeout=15)
        found = json.loads(done.stdout).get("loggedIn")
        value = bool(found) if isinstance(found, bool) else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        value = None
    _signed_in_cache.update(value=(value,), at=time.monotonic())
    return value


def claude_code_command() -> list[str] | None:
    """How to start Claude Code here, if it is installed."""
    configured = os.environ.get("RIGSMITH_CLAUDE")
    if configured:
        return configured.split()
    for name in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(name)
        if found:
            return [found]
    return None


class ClaudeCode:
    """Claude Code headless, with rigsmith's tools over MCP and web search.

    Runs its own tool loop; :meth:`run` relays its stream-json events. The
    rigsmith MCP server it starts is this same installation, pointed at the
    same workspace, so whatever it does is visible in the app immediately.
    """

    name = "claude-code"

    def __init__(self, *, model: str = "", max_budget_usd: float | None = None) -> None:
        command = claude_code_command()
        if command is None:
            raise Unavailable(
                "Claude Code is not installed or not on PATH",
                hint="Install it (https://code.claude.com), or set RIGSMITH_CLAUDE to its path.",
            )
        self.command = command
        self.model = model
        self.max_budget_usd = max_budget_usd

    def describe(self) -> str:
        return "Claude through Claude Code" + (f" ({self.model})" if self.model else "")

    def mcp_config(self, directory) -> str:
        import sys
        from pathlib import Path

        from ..config import get_settings

        # What a Python process needs to start and find its files, and the
        # app's own settings. Not the whole environment: this is written to a
        # file in the workspace, and API keys do not belong there.
        env = {name: os.environ[name] for name in _SERVER_ENV if os.environ.get(name)}
        env.update({name: value for name, value in os.environ.items() if name.startswith("RIGSMITH_")})
        env["RIGSMITH_WORKSPACE"] = str(get_settings().workspace)
        config = {"mcpServers": {"rigsmith": {"command": sys.executable, "args": ["-m", "rigsmith.mcp.server"], "env": env}}}
        path = Path(directory) / "claude-mcp.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return str(path)

    def run(
        self,
        prompt: str,
        *,
        system_append: str,
        allowed: list[str],
        directory,
        resume: str | None = None,
        on_event=None,
        cancelled=None,
    ) -> dict:
        """Run one exchange; return ``{"text", "session_id", "cost", "turns"}``."""
        args = [
            *self.command, "-p",
            "--output-format", "stream-json", "--verbose",
            "--mcp-config", self.mcp_config(directory),
            "--strict-mcp-config",
            "--append-system-prompt", system_append,
            "--allowedTools", ",".join(allowed),
            # Nobody is there to answer a permission prompt: one would wait
            # for ever. Anything not allowed above is refused instead.
            "--permission-prompts", "none",
        ]
        if self.model:
            args += ["--model", self.model]
        if resume:
            args += ["--resume", resume]
        if self.max_budget_usd:
            args += ["--max-budget-usd", str(self.max_budget_usd)]
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert process.stdin and process.stdout and process.stderr
        # Both streams read on threads: stderr filling its pipe while only
        # stdout is read would block Claude Code, and a blocked read here
        # would never see Stop.
        lines: queue.Queue = queue.Queue()
        errors: list[str] = []

        def pump() -> None:
            for text in process.stdout:
                lines.put(text)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        threading.Thread(target=lambda: errors.extend(process.stderr), daemon=True).start()
        outcome = {"text": "", "session_id": resume, "cost": None, "turns": 0}
        names: dict[str, str] = {}
        try:
            process.stdin.write(prompt)
            process.stdin.close()
            self._read(lines, outcome, names, on_event, cancelled, process)
            process.wait(timeout=30)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
        if process.returncode not in (0, None) and not outcome["text"]:
            error = "".join(errors)[-800:]
            if needs_login(error):
                raise Unavailable("Claude Code is not signed in", hint=LOGIN_HINT)
            raise Unavailable(f"Claude Code exited with {process.returncode}", hint=error or None)
        return outcome

    def _read(self, lines, outcome, names, on_event, cancelled, process) -> None:
        """Events from Claude Code's stream until it ends, Stop, or it goes quiet."""
        quiet_since = time.monotonic()
        while True:
            if cancelled and cancelled():
                process.terminate()
                outcome["text"] = (outcome["text"] + "\n(stopped)").strip()
                return
            try:
                line = lines.get(timeout=0.5)
            except queue.Empty:
                if time.monotonic() - quiet_since > CLAUDE_QUIET_LIMIT_S:
                    process.terminate()
                    outcome["error"] = "timeout"
                    outcome["text"] = (outcome["text"] + f"\n(Claude Code said nothing for {CLAUDE_QUIET_LIMIT_S // 60:.0f} minutes and was stopped)").strip()
                    return
                continue
            if line is None:
                return
            quiet_since = time.monotonic()
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                outcome["session_id"] = event.get("session_id") or outcome["session_id"]
            elif kind == "assistant":
                for block in (event.get("message") or {}).get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        if on_event:
                            on_event({"kind": "text", "text": block["text"]})
                    elif block.get("type") == "tool_use":
                        names[block.get("id", "")] = block.get("name", "")
                        if on_event:
                            on_event({"kind": "tool_call", "name": _short(block.get("name", "")), "arguments": block.get("input") or {}, "id": block.get("id")})
                    elif block.get("type") == "thinking" and block.get("thinking") and on_event:
                        on_event({"kind": "reasoning", "text": block["thinking"]})
            elif kind == "user":
                for block in (event.get("message") or {}).get("content", []) if isinstance((event.get("message") or {}).get("content"), list) else []:
                    if block.get("type") == "tool_result" and on_event:
                        content = block.get("content")
                        if isinstance(content, list):
                            content = "\n".join(c.get("text", "") for c in content if isinstance(c, dict))
                        on_event({
                            "kind": "tool_result",
                            "name": _short(names.get(block.get("tool_use_id", ""), "")),
                            "ok": not block.get("is_error"),
                            "text": str(content or "")[:4000],
                            "id": block.get("tool_use_id"),
                        })
            elif kind == "result":
                outcome["text"] = event.get("result") or outcome["text"]
                outcome["session_id"] = event.get("session_id") or outcome["session_id"]
                outcome["cost"] = event.get("total_cost_usd")
                outcome["turns"] = event.get("num_turns") or 0
                if event.get("is_error") or event.get("subtype", "success") != "success":
                    outcome["error"] = "error" if event.get("is_error") else (event.get("subtype") or "error")
                    if needs_login(outcome["text"]):
                        outcome["text"] += "\n" + LOGIN_HINT


def _short(name: str) -> str:
    """``mcp__rigsmith__list_takes`` -> ``list_takes``."""
    return name.split("__")[-1] if name.startswith("mcp__") else name


# --------------------------------------------------------------------------
# choosing one
# --------------------------------------------------------------------------


def from_settings(llm=None, *, local: bool | None = None):
    """The provider the settings ask for, falling back sensibly under ``auto``."""
    from ..core import appsettings

    settings = appsettings.load()
    local = settings.local if local is None else local
    llm = llm or settings.active_llm()
    provider = llm.provider
    key = os.environ.get(llm.api_key_env) if llm.api_key_env else None

    if local and provider in ("claude-code", "anthropic"):
        raise Invalid(
            f"'{provider}' reaches the internet, and rigsmith is in local mode",
            hint="Choose an OpenAI-compatible local server for local mode.",
        )
    if provider == "openai-compatible" or local:
        return OpenAICompatible(llm.base_url, model=llm.model, api_key=key, temperature=llm.temperature, thinking=llm.thinking)
    if provider == "claude-code":
        return ClaudeCode(model=llm.claude_model)
    if provider == "anthropic":
        return AnthropicAPI(model=llm.claude_model)
    # auto, accuracy mode: Claude Code, then the API, then whatever is local.
    # Installed but signed out is the same as absent - every request would
    # fail - so the next one in line answers instead.
    if claude_code_command() and claude_code_signed_in() is not False:
        return ClaudeCode(model=llm.claude_model)
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicAPI(model=llm.claude_model)
        except Unavailable:
            pass
    return OpenAICompatible(llm.base_url, model=llm.model, api_key=key, temperature=llm.temperature, thinking=llm.thinking)


def status() -> dict:
    """What is available right now - for the settings page."""
    from ..core import appsettings

    settings = appsettings.load()
    local = OpenAICompatible(settings.local_llm.base_url, model=settings.local_llm.model)
    try:
        models = local.models(quick=True)
        local_state = {"reachable": True, "models": models}
    except Unavailable as exc:
        local_state = {"reachable": False, "error": str(exc).splitlines()[0]}
    try:
        import anthropic  # noqa: F401

        sdk = True
    except ImportError:
        sdk = False
    return {
        "mode": settings.mode,
        "claude_code": bool(claude_code_command()),
        "claude_code_signed_in": claude_code_signed_in(),
        "anthropic_sdk": sdk,
        "anthropic_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "local": {"base_url": settings.local_llm.base_url, **local_state},
    }

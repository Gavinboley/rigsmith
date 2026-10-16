"""The chat in the app, and the agents it sends off.

A chat is a conversation with the assistant role: you type, it looks things
up, runs tools, and hands bigger jobs to specialists that work in the
background. Every step is published as it happens (:mod:`.events`), so the
page shows the tool calls and their results live, and anything that would
change the device or delete work stops and asks you first.

Chats are kept in the workspace. A local model's conversation is restored
exactly after a restart; a Claude Code one is resumed by its session id.

Which model answers follows the app's mode: Claude in accuracy mode, the
local server in local mode. A chat started in one mode keeps its provider
until it is cleared, so a conversation never silently changes who it is
talking to.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings, require_writable
from . import events, storage
from .errors import Invalid, NotFound
from .jobs import get_manager

#: How long an approval request waits for a person before it counts as a no.
APPROVAL_TIMEOUT_S = 600.0


def _dir() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    directory = settings.agents_dir
    (directory / "runs").mkdir(parents=True, exist_ok=True)
    return directory


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Chat:
    chat_id: str
    title: str = "New chat"
    role: str = "assistant"
    provider: str = ""
    """claude-code, anthropic or openai-compatible - fixed for the chat's life."""
    model: str = ""
    mode: str = "accuracy"
    transcript: list[dict] = field(default_factory=list)
    """What the page shows: user, assistant, tool and status entries."""
    native: list = field(default_factory=list)
    """The provider's own history, for picking the conversation back up."""
    claude_session: str | None = None
    allow_writes: bool = False
    busy: bool = False
    job_id: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = ""
    usage: dict = field(default_factory=dict)

    def to_dict(self, *, full: bool = True) -> dict:
        data = asdict(self)
        data.pop("native")
        if not full:
            data.pop("transcript")
            data["messages"] = len(self.transcript)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Chat":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        self.updated_at = _now()
        storage.write_json(_dir() / f"chat-{self.chat_id}.json", asdict(self))


_lock = threading.Lock()
_conversations: dict[str, object] = {}
_approvals: dict[str, dict] = {}


def list_chats() -> list[Chat]:
    chats = []
    for path in _dir().glob("chat-*.json"):
        try:
            chats.append(Chat.from_dict(storage.read_json(path)))
        except (OSError, ValueError, TypeError):
            continue
    chats.sort(key=lambda c: c.updated_at or c.created_at, reverse=True)
    return chats


def get(chat_id: str) -> Chat:
    path = _dir() / f"chat-{chat_id}.json"
    if not path.is_file():
        raise NotFound(f"no chat '{chat_id}'")
    return Chat.from_dict(storage.read_json(path))


def _edit(chat_id: str, change) -> Chat:
    """Read, change and save one chat under its lock."""
    with storage.locked(_dir() / f"chat-{chat_id}.json"):
        chat = get(chat_id)
        change(chat)
        chat.save()
        return chat


def create(*, role: str = "assistant", title: str | None = None, allow_writes: bool = False) -> Chat:
    from ..agent import roles
    from . import appsettings

    require_writable("start a chat")
    roles.get(role)
    settings = appsettings.load()
    chat = Chat(
        chat_id=uuid.uuid4().hex[:10],
        title=title or "New chat",
        role=role,
        mode=settings.mode,
        allow_writes=allow_writes,
    )
    chat.save()
    return chat


def delete(chat_id: str) -> None:
    require_writable("delete a chat")
    path = _dir() / f"chat-{chat_id}.json"
    if path.is_file():
        path.unlink()
    with _lock:
        _conversations.pop(chat_id, None)


def update(chat_id: str, *, title: str | None = None, allow_writes: bool | None = None) -> Chat:
    chat = get(chat_id)
    if title is not None:
        chat.title = title
    if allow_writes is not None:
        chat.allow_writes = allow_writes
    chat.save()
    return chat


# --------------------------------------------------------------------------
# approvals
# --------------------------------------------------------------------------


def pending_approvals(chat_id: str | None = None) -> list[dict]:
    with _lock:
        return [
            {k: v for k, v in entry.items() if k != "event"}
            for entry in _approvals.values()
            if chat_id is None or entry["chat_id"] == chat_id
        ]


def decide(approval_id: str, allow: bool) -> dict:
    with _lock:
        entry = _approvals.get(approval_id)
        if entry is None:
            raise NotFound(f"no pending approval '{approval_id}'", hint="It may already have been answered or timed out.")
        if entry.get("decision") is not None:
            # A double click, or the page and the chat both answering: the
            # first answer stands, and a second cannot overturn it.
            return {k: v for k, v in entry.items() if k != "event"}
        entry["decision"] = bool(allow)
    entry["event"].set()
    events.publish({"type": "chat", "chat_id": entry["chat_id"], "kind": "approval_decided", "approval_id": approval_id, "allow": bool(allow)})
    return {k: v for k, v in entry.items() if k != "event"}


def _wait_for_approval(chat_id: str, call, *, timeout: float = APPROVAL_TIMEOUT_S, cancelled=None) -> bool:
    approval_id = uuid.uuid4().hex[:10]
    entry = {
        "approval_id": approval_id,
        "chat_id": chat_id,
        "tool": call.name,
        "arguments": call.arguments,
        "decision": None,
        "event": threading.Event(),
        "asked_at": _now(),
    }
    with _lock:
        _approvals[approval_id] = entry
    events.publish({"type": "chat", "chat_id": chat_id, "kind": "approval_request", "approval_id": approval_id, "tool": call.name, "arguments": call.arguments})
    waited = 0.0
    while waited < timeout:
        if entry["event"].wait(0.5):
            break
        waited += 0.5
        if cancelled and cancelled():
            break
    with _lock:
        _approvals.pop(approval_id, None)
    return bool(entry["decision"])


# --------------------------------------------------------------------------
# talking
# --------------------------------------------------------------------------


def _provider_for(chat: Chat):
    from ..agent import providers

    return providers.from_settings(local=chat.mode == "local")


def _conversation(chat: Chat, provider, role, tools):
    with _lock:
        existing = _conversations.get(chat.chat_id)
    if existing is not None:
        return existing
    conversation = provider.start(role.system_prompt(_context_note()), tools)
    if chat.native and hasattr(conversation, "messages"):
        # Picking a conversation back up after a restart.
        conversation.messages = list(chat.native)
    with _lock:
        _conversations[chat.chat_id] = conversation
    return conversation


def _context_note() -> str:
    """A line of where things stand, so the agent does not have to ask."""
    from . import appsettings

    settings = appsettings.load()
    bits = [f"Mode: {settings.mode}."]
    if settings.default_profile:
        bits.append(f"The user's default guitar profile is '{settings.default_profile}'.")
    try:
        from . import songs

        recent = songs.list_songs()[:3]
        if recent:
            bits.append("Recent songs: " + ", ".join(s.song_id for s in recent) + ".")
    except Exception:
        pass
    return " ".join(bits)


def send(chat_id: str, text: str) -> dict:
    """Post a message; the reply streams as events from a background job."""
    from ..agent import providers, roles, runner
    from ..agent.providers import ClaudeCode

    require_writable("send a chat message")
    text = (text or "").strip()
    if not text:
        raise Invalid("an empty message")
    chat = get(chat_id)
    if chat.busy:
        raise Invalid("the assistant is still answering the last message", hint="Wait for it, or stop it.")

    role = roles.get(chat.role)
    provider = _provider_for(chat)
    if not chat.provider:
        chat.provider = provider.name
        chat.model = getattr(provider, "model", "") or ""
    if chat.title == "New chat":
        chat.title = text[:60]
    chat.transcript.append({"role": "user", "text": text, "at": _now()})
    chat.busy = True
    chat.save()

    def publish(entry: dict) -> None:
        entry = {"at": _now(), **entry}
        if entry.get("kind") in ("text", "tool_call", "tool_result", "approval", "reasoning", "final", "error"):
            _edit(chat_id, lambda current: current.transcript.append(entry))
        events.publish({"type": "chat", "chat_id": chat_id, **entry})

    def work(progress):
        cancelled = lambda: progress.cancelled  # noqa: E731
        # The turn's last word is said only once the chat is free again:
        # whoever hears "done" may answer at once, and must not be told the
        # assistant is still busy.
        last: list[dict] = []
        try:
            if isinstance(provider, ClaudeCode):
                current = get(chat_id)
                result = runner.claude_code_exchange(
                    provider, role, text,
                    emit=publish, directory=_dir(), resume=current.claude_session,
                    allow_writes=current.allow_writes, local=current.mode == "local",
                    cancelled=cancelled, extra_system=_context_note(),
                )
                _edit(chat_id, lambda current: setattr(current, "claude_session", result.session_id or current.claude_session))
            else:
                extra = runner.dispatch_tools(
                    lambda r, t: dispatch(r, t, parent=chat_id),
                    lambda run_id: agent_result(run_id),
                ) if role.name == "assistant" else []
                tools = runner.toolset(role, allow_writes=chat.allow_writes, extra=extra, local=isinstance(provider, providers.OpenAICompatible))
                conversation = _conversation(chat, provider, role, tools)
                conversation.add_user(text)
                result = runner.step_loop(
                    conversation, tools, emit=publish,
                    approve=lambda call: get(chat_id).allow_writes or _wait_for_approval(chat_id, call, cancelled=cancelled),
                    max_rounds=role.max_rounds, cancelled=cancelled,
                )
                def record(current):
                    current.native = conversation.history() if hasattr(conversation, "history") else []
                    for key, value in (result.usage or {}).items():
                        current.usage[key] = current.usage.get(key, 0) + value

                _edit(chat_id, record)
            if result.stopped == "error":
                last.append({"kind": "error", "text": result.text or "the model failed"})
            last.append({"kind": "done", "text": result.text, "stopped": result.stopped, "cost": result.cost})
            return result.to_dict()
        except Exception as exc:
            last.append({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
            raise
        finally:
            def settle(current):
                current.busy = False
                current.job_id = None

            _edit(chat_id, settle)
            for entry in last:
                publish(entry)

    job = get_manager().submit(f"chat: {text[:40]}", work)
    _edit(chat_id, lambda current: setattr(current, "job_id", job.id) if current.busy else None)
    return {"chat_id": chat_id, "job_id": job.id, "provider": chat.provider}


def stop(chat_id: str) -> dict:
    chat = get(chat_id)
    if chat.job_id:
        try:
            get_manager().cancel(chat.job_id)
        except Exception:
            pass
    for entry in pending_approvals(chat_id):
        decide(entry["approval_id"], False)
    return {"chat_id": chat_id, "stopped": True}


def clear(chat_id: str) -> Chat:
    """Forget the conversation but keep the chat - a fresh start, same window."""
    require_writable("clear a chat")
    chat = get(chat_id)
    chat.transcript, chat.native, chat.claude_session = [], [], None
    chat.provider, chat.model = "", ""
    chat.save()
    with _lock:
        _conversations.pop(chat_id, None)
    return chat


# --------------------------------------------------------------------------
# dispatched agents
# --------------------------------------------------------------------------


def _run_path(run_id: str) -> Path:
    return _dir() / "runs" / f"{run_id}.json"


def dispatch(role_name: str, task: str, *, parent: str | None = None, allow_writes: bool = False) -> str:
    """Start a specialist on a task in the background; return its run id."""
    from ..agent import providers, roles, runner
    from ..agent.providers import ClaudeCode
    from . import appsettings

    role = roles.get(role_name)
    if not task.strip():
        raise Invalid("a dispatched agent needs a task")
    run_id = uuid.uuid4().hex[:10]
    record = {
        "run_id": run_id,
        "role": role.name,
        "task": task,
        "parent": parent,
        "state": "running",
        "events": [],
        "result": None,
        "started_at": _now(),
    }
    storage.write_json(_run_path(run_id), record)
    events.publish({"type": "agent", "kind": "started", "run_id": run_id, "role": role.name, "task": task, "parent": parent})

    def emit(entry: dict) -> None:
        if entry.get("kind") in ("text", "tool_call", "tool_result", "final", "error", "approval"):
            with storage.locked(_run_path(run_id)):
                data = storage.read_json(_run_path(run_id))
                data["events"].append({"at": _now(), **entry})
                storage.write_json(_run_path(run_id), data)
        events.publish({"type": "agent", "run_id": run_id, "role": role.name, "parent": parent, **entry})

    def work(progress):
        local = appsettings.load().local
        provider = providers.from_settings(local=local)
        cancelled = lambda: progress.cancelled  # noqa: E731
        try:
            if isinstance(provider, ClaudeCode):
                result = runner.claude_code_exchange(
                    provider, role, task, emit=emit, directory=_dir(), resume=None,
                    allow_writes=allow_writes, local=local, cancelled=cancelled, extra_system=_context_note(),
                )
            else:
                tools = runner.toolset(role, allow_writes=allow_writes, local=isinstance(provider, providers.OpenAICompatible))
                conversation = provider.start(role.system_prompt(_context_note()), tools)
                conversation.add_user(task)
                approver = (lambda call: _wait_for_approval(parent, call, cancelled=cancelled)) if parent else (lambda call: allow_writes)
                result = runner.step_loop(conversation, tools, emit=emit, approve=approver, max_rounds=role.max_rounds, cancelled=cancelled)
            state = "failed" if result.stopped == "error" else "done"
            payload = result.to_dict()
        except Exception as exc:
            state = "failed"
            payload = {"text": f"{type(exc).__name__}: {exc}", "stopped": "error"}
        with storage.locked(_run_path(run_id)):
            data = storage.read_json(_run_path(run_id))
            data.update(state=state, result=payload, finished_at=_now())
            storage.write_json(_run_path(run_id), data)
        events.publish({"type": "agent", "kind": "finished", "run_id": run_id, "role": role.name, "state": state, "parent": parent, "text": payload.get("text", "")})
        return payload

    job = get_manager().submit(f"{role.name}: {task[:40]}", work)
    with storage.locked(_run_path(run_id)):
        data = storage.read_json(_run_path(run_id))
        data["job_id"] = job.id
        storage.write_json(_run_path(run_id), data)
    return f"Dispatched {role.title} as run {run_id}. Check it with agent_result('{run_id}')."


def agent_run(run_id: str) -> dict:
    path = _run_path(run_id)
    if not path.is_file():
        raise NotFound(f"no agent run '{run_id}'")
    return storage.read_json(path)


def agent_result(run_id: str) -> str:
    try:
        data = agent_run(run_id)
    except NotFound as exc:
        return str(exc)
    if data["state"] == "running":
        tools_used = [e["name"] for e in data["events"] if e.get("kind") == "tool_call"]
        return f"still working ({len(tools_used)} tool call(s) so far: {', '.join(tools_used[-5:])})"
    return f"{data['state']}: " + ((data.get("result") or {}).get("text") or "(no report)")


def list_runs(limit: int = 30) -> list[dict]:
    runs = []
    for path in (_dir() / "runs").glob("*.json"):
        try:
            data = storage.read_json(path)
        except (OSError, ValueError):
            continue
        data.pop("events", None)
        runs.append(data)
    runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
    return runs[:limit]

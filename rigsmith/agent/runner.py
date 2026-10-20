"""The agent loop: ask, act, wait for a person where it matters, repeat.

For step-at-a-time providers (a local model, the Anthropic API) the loop is
here: the model answers, every tool it asked for runs in-process through the
MCP server, results go back, and it continues until it stops asking or runs
out of rounds. Tools that change the device or delete work stop the loop
until a person approves or declines that exact call in the app.

Claude Code runs its own loop; for it this module only relays events and
restricts which tools it may use.

Two tools exist only here, not on the MCP server, because they are about
agents rather than guitars: ``dispatch_agent`` hands a task to a specialist
running in the background, and ``agent_result`` collects what it found.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable

from . import roles as role_registry
from . import tools as tool_registry
from ..core.errors import Unavailable
from .providers import ClaudeCode, ToolCall, needs_login

Emit = Callable[[dict], None]


@dataclass
class VirtualTool:
    name: str
    description: str
    schema: dict
    handler: Callable[[dict], str]
    group: str = "agents"
    needs_approval: bool = False
    online: bool = False

    def openai(self, *, compact: bool = False) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.schema}}

    def anthropic(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def dispatch_tools(dispatch: Callable[[str, str], str], result: Callable[[str], str]) -> list[VirtualTool]:
    roles_text = "; ".join(f"{r.name}: {r.description}" for r in role_registry.ROLES.values() if r.name != "assistant")
    return [
        VirtualTool(
            "dispatch_agent",
            "Hand a multi-step task to a specialist agent that runs in the background. "
            f"Roles - {roles_text}. Give it a complete brief: what, which song/part/profile, "
            "and what to report back. Returns a run id; collect the result with agent_result.",
            {
                "type": "object",
                "properties": {
                    "role": {"type": "string", "enum": [r for r in role_registry.ROLES if r != "assistant"]},
                    "task": {"type": "string", "description": "The complete brief."},
                },
                "required": ["role", "task"],
            },
            lambda args: dispatch(args.get("role", ""), args.get("task", "")),
        ),
        VirtualTool(
            "agent_result",
            "The status and, when finished, the report of a dispatched agent.",
            {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
            lambda args: result(args.get("run_id", "")),
        ),
    ]


@dataclass
class RunResult:
    text: str = ""
    rounds: int = 0
    calls: list[dict] = field(default_factory=list)
    stopped: str = "done"
    usage: dict = field(default_factory=dict)
    session_id: str | None = None
    cost: float | None = None

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "rounds": self.rounds,
            "calls": self.calls,
            "stopped": self.stopped,
            "usage": self.usage,
            "session_id": self.session_id,
            "cost": self.cost,
        }


def toolset(role, *, allow_writes: bool, extra: list[VirtualTool] | None = None, local: bool = False):
    """The tools a role is handed: its own, minus gated ones unless allowed.

    ``local`` hands a local model the role's shorter ``local_tools`` list
    where it has one.
    """
    if local and getattr(role, "local_tools", None):
        selected = tool_registry.select(role.local_tools)
    else:
        selected = tool_registry.select(role.tools, groups_wanted=role.groups)
    if not allow_writes:
        # Gated tools stay visible - the agent can still propose them - and
        # every call waits for approval. Hiding them would make the agent
        # improvise around a missing capability instead of asking.
        pass
    return list(selected) + list(extra or [])


def _step(conversation, emit: Emit, cancelled=None):
    """A turn, streamed when the provider can: ``partial`` events carry the
    visible reply so far, and the final ``text`` replaces them."""
    import inspect

    try:
        parameters = inspect.signature(conversation.step).parameters
    except (TypeError, ValueError):
        parameters = {}
    kwargs = {}
    if "on_partial" in parameters:
        kwargs["on_partial"] = lambda text: emit({"kind": "partial", "text": text})
    if "cancelled" in parameters and cancelled is not None:
        kwargs["cancelled"] = cancelled
    return conversation.step(**kwargs)


def step_loop(
    conversation,
    tools,
    *,
    emit: Emit,
    approve: Callable[[ToolCall], bool],
    max_rounds: int = 24,
    cancelled: Callable[[], bool] | None = None,
) -> RunResult:
    """Drive a step-at-a-time provider to the end of one exchange."""
    by_name = {t.name: t for t in tools}
    result = RunResult()
    for round_number in range(1, max_rounds + 1):
        if cancelled and cancelled():
            result.stopped = "cancelled"
            break
        emit({"kind": "status", "text": "thinking" if round_number == 1 else f"round {round_number}"})
        from .providers import Stopped

        try:
            turn = _step(conversation, emit, cancelled)
        except Stopped:
            result.stopped = "cancelled"
            break
        result.rounds = round_number
        for key, value in (turn.usage or {}).items():
            if isinstance(value, (int, float)):
                result.usage[key] = result.usage.get(key, 0) + value
        if turn.reasoning:
            emit({"kind": "reasoning", "text": turn.reasoning})
        if turn.text:
            emit({"kind": "text", "text": turn.text})
            result.text = turn.text
        if not turn.calls:
            result.stopped = "done" if turn.stop != "length" else "out of room"
            return result

        outcomes = []
        for call in turn.calls:
            emit({"kind": "tool_call", "name": call.name, "arguments": call.arguments, "id": call.id})
            spec = by_name.get(call.name)
            if spec is None:
                text = f"there is no tool called '{call.name}' available to you"
                outcomes.append((call, text, True))
                emit({"kind": "tool_result", "name": call.name, "ok": False, "text": text, "id": call.id})
                continue
            if getattr(spec, "needs_approval", False):
                emit({"kind": "approval", "name": call.name, "arguments": call.arguments, "id": call.id})
                if not approve(call):
                    text = "the user declined this action - do not retry it; ask what they want instead"
                    outcomes.append((call, text, True))
                    emit({"kind": "tool_result", "name": call.name, "ok": False, "text": text, "id": call.id, "declined": True})
                    continue
            if isinstance(spec, VirtualTool):
                try:
                    text, ok = spec.handler(call.arguments), True
                except Exception as exc:
                    text, ok = f"{type(exc).__name__}: {exc}", False
            else:
                # Whatever a tool raises becomes its result: a turn that dies
                # here leaves the model's tool call with no answer in the
                # history, and every later message in the chat is refused.
                try:
                    outcome = tool_registry.call(call.name, call.arguments)
                    text, ok = outcome.text, outcome.ok
                except Exception as exc:
                    text, ok = f"{type(exc).__name__}: {exc}", False
            outcomes.append((call, text, not ok))
            result.calls.append({"name": call.name, "arguments": call.arguments, "ok": ok})
            emit({"kind": "tool_result", "name": call.name, "ok": ok, "text": text[:4000], "id": call.id})
        conversation.add_results(outcomes)
    else:
        result.stopped = "round limit"
        emit({"kind": "status", "text": f"stopped after {max_rounds} rounds"})
    return result


def claude_code_exchange(
    provider: ClaudeCode,
    role,
    prompt: str,
    *,
    emit: Emit,
    directory,
    resume: str | None,
    allow_writes: bool,
    local: bool,
    cancelled=None,
    extra_system: str = "",
) -> RunResult:
    """One exchange with Claude Code, restricted to the role's tools."""
    if local:
        from ..core.errors import Invalid

        raise Invalid("Claude Code reaches the internet, and rigsmith is in local mode")
    names = [t.name for t in tool_registry.select(role.tools, groups_wanted=role.groups)]
    if role.name == "assistant":
        # Claude is capable of driving the whole toolset itself; the router's
        # small set exists for small local models.
        names = list(tool_registry.registry())
    allowed = [f"mcp__rigsmith__{n}" for n in names if allow_writes or not tool_registry.registry()[n].needs_approval]
    allowed += ["WebSearch", "WebFetch"]
    gated = sorted(n for n in names if tool_registry.registry()[n].needs_approval)
    guidance = ""
    if gated and not allow_writes:
        guidance = (
            "\nThese tools are not available to you in this chat because they change the "
            "device or delete work: " + ", ".join(gated) + ". When one is needed, say exactly "
            "what you would run and why, and the user will do it from the app."
        )
    emit({"kind": "status", "text": "Claude Code is working"})

    def run(session):
        return provider.run(
            prompt,
            system_append=role.system_prompt(extra_system + guidance),
            allowed=allowed,
            directory=directory,
            resume=session,
            on_event=emit,
            cancelled=cancelled,
        )

    try:
        outcome = run(resume)
    except Unavailable as exc:
        # A chat remembers its Claude session to carry the conversation on;
        # one Claude Code no longer has would fail every turn after. Start a
        # fresh one instead - the sign-in errors aside, which a new session
        # would only repeat.
        said = f"{exc} {getattr(exc, 'hint', '') or ''}".lower()
        if not resume or needs_login(said) or not any(word in said for word in ("session", "conversation", "resume")):
            raise
        emit({"kind": "status", "text": "the chat's earlier Claude session is gone - starting a fresh one"})
        outcome = run(None)
    if outcome.get("text"):
        emit({"kind": "final", "text": outcome["text"]})
    return RunResult(
        text=outcome.get("text", ""),
        rounds=outcome.get("turns") or 0,
        stopped=outcome.get("error") or "done",
        session_id=outcome.get("session_id"),
        cost=outcome.get("cost"),
    )


def summarise_arguments(arguments: dict, limit: int = 160) -> str:
    text = json.dumps(arguments, default=str)
    return text if len(text) <= limit else text[: limit - 3] + "..."

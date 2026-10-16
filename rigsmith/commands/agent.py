"""``rigsmith chat`` and ``rigsmith agent`` - the agents, from a terminal."""

from __future__ import annotations

import re
import threading
import time

import click

from ..clikit import handled


def _render(event: dict) -> str | None:
    kind = event.get("kind")
    if kind == "text":
        return event.get("text")
    if kind == "tool_call":
        from ..agent.runner import summarise_arguments

        return click.style(f"  -> {event.get('name')}({summarise_arguments(event.get('arguments') or {})})", fg="cyan")
    if kind == "tool_result":
        first = (event.get("text") or "").strip().splitlines()[:1]
        mark = "ok" if event.get("ok") else "failed"
        return click.style(f"  <- {event.get('name')} {mark}: {first[0][:100] if first else ''}", fg="green" if event.get("ok") else "red")
    if kind == "error":
        return click.style(event.get("text", "error"), fg="red")
    if kind == "status":
        return click.style(f"  ({event.get('text')})", dim=True)
    return None


#: "undo that", "go back" - an undo needs no model.
UNDO = re.compile(r"^\s*(undo|undo (that|it|the last (edit|change))|go back|put it back|revert( that)?)\s*[.!]*\s*$", re.I)


def _without_a_model(text: str) -> None:
    """After a turn failed: what the words can still do, asked first.

    The terminal's version of the chat page's buttons - a turn by ear or an
    undo, on the draft edited last, each only on a yes.
    """
    from ..core import rigs
    from ..tune import nudge as words

    drafts = sorted(rigs.list_drafts(), key=lambda d: d.updated_at, reverse=True)
    if not drafts:
        return
    latest = drafts[0]
    try:
        if UNDO.match(text):
            if click.confirm(f"  without a model: undo the last edit to '{latest.rig.name}'?", default=False):
                click.echo("  " + rigs.undo(latest.draft_id).summary)
            return
        heard = words.parse(text)
        if heard.understood and click.confirm(f"  without a model: turn '{latest.rig.name}' that way ({', '.join(heard.understood)})?", default=False):
            click.echo("  " + rigs.nudge(latest.draft_id, ", ".join(heard.understood)).summary)
    except click.Abort:
        return
    except Exception as exc:
        click.echo(click.style(f"  {exc}", fg="red"))


@click.command("chat")
@click.option("--role", default="assistant", show_default=True, help="assistant, researcher, song-mapper, luthier, tone-matcher, rig-builder.")
@click.option("--allow-writes", is_flag=True, help="Let this chat change the device without asking each time.")
@handled
def chat_command(role: str, allow_writes: bool) -> None:
    """Talk to the rigsmith assistant. Ctrl-D or 'exit' to leave."""
    from ..core import chat, events

    session = chat.create(role=role, allow_writes=allow_writes)
    done = threading.Event()
    failed = threading.Event()

    def listen(event: dict) -> None:
        if event.get("type") != "chat" or event.get("chat_id") != session.chat_id:
            return
        if event.get("kind") == "approval_request":
            done_ask.append(event)
            return
        if event.get("kind") in ("done", "error"):
            if event.get("kind") == "error":
                click.echo(_render(event))
                failed.set()
            done.set()
            return
        line = _render(event)
        if line:
            click.echo(line)

    done_ask: list[dict] = []
    unsubscribe = events.subscribe(listen)
    click.echo(click.style(f"rigsmith {role} - type a message; 'exit' to leave.", bold=True))
    try:
        while True:
            try:
                text = click.prompt(click.style("you", fg="yellow"), prompt_suffix="> ", default="", show_default=False)
            except (click.Abort, EOFError):
                break
            if text.strip().lower() in ("exit", "quit"):
                break
            if not text.strip():
                continue
            done.clear()
            failed.clear()
            chat.send(session.chat_id, text)
            while not done.wait(0.2):
                while done_ask:
                    ask = done_ask.pop(0)
                    from ..agent.runner import summarise_arguments

                    allow = click.confirm(
                        click.style(f"  allow {ask['tool']}({summarise_arguments(ask['arguments'])})?", fg="magenta"),
                        default=False,
                    )
                    chat.decide(ask["approval_id"], allow)
            if failed.is_set():
                _without_a_model(text)
    finally:
        unsubscribe()


@click.group("agent")
def group() -> None:
    """Specialist agents: run one on a task, see what is available."""


@group.command("run")
@click.argument("role")
@click.argument("task")
@click.option("--allow-writes", is_flag=True)
@handled
def agent_run(role: str, task: str, allow_writes: bool) -> None:
    """Run a specialist on a task and wait for its report."""
    from ..core import chat, events

    def listen(event: dict) -> None:
        if event.get("type") == "agent" and event.get("run_id") == state.get("run_id"):
            line = _render(event)
            if line:
                click.echo(line)

    state: dict = {}
    unsubscribe = events.subscribe(listen)
    try:
        message = chat.dispatch(role, task, allow_writes=allow_writes)
        state["run_id"] = message.split("run ")[1].split(".")[0]
        while chat.agent_run(state["run_id"])["state"] == "running":
            time.sleep(0.3)
        click.echo("\n" + chat.agent_result(state["run_id"]))
    finally:
        unsubscribe()


@group.command("roles")
@handled
def agent_roles() -> None:
    """The specialists and what each may touch."""
    from ..agent import roles, tools

    for role in roles.ROLES.values():
        count = len(tools.select(role.tools, groups_wanted=role.groups))
        click.echo(f"  {role.name:<13} {count:3} tools  {role.description}")


@group.command("status")
@handled
def agent_status() -> None:
    """Which models are available right now."""
    from ..agent import providers

    status = providers.status()
    click.echo(f"mode: {status['mode']}")
    click.echo(f"Claude Code: {'installed' if status['claude_code'] else 'not found'}")
    click.echo(f"Anthropic SDK: {'installed' if status['anthropic_sdk'] else 'not installed'}, key {'set' if status['anthropic_key'] else 'not set'}")
    local = status["local"]
    if local.get("reachable"):
        click.echo(f"local server {local['base_url']}: " + ", ".join(local["models"][:8]))
    else:
        click.echo(f"local server {local['base_url']}: not answering")


@group.command("runs")
@handled
def agent_runs() -> None:
    """Recent dispatched agents."""
    from ..core import chat

    for run in chat.list_runs():
        click.echo(f"  {run['run_id']}  {run['role']:<13} {run['state']:<8} {run['task'][:60]}")

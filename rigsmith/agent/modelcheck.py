"""Can this model drive rigsmith? A short exam, against the model you have loaded.

Agents work by calling tools, so what decides whether a local model is any
use here is not how well it chats but whether, asked for something, it
reaches for the right tool with the right arguments. Benchmarks say how a
model does on someone else's tools; this asks yours, with the assistant's
real tool list and system prompt.

Each question is a fresh conversation, and only the model's first reply is
judged: which tool it called (if any) and whether the arguments carry what
the question named. Nothing is executed - the exam reads nothing and writes
nothing - so it can run against any workspace.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Callable


@dataclass
class Question:
    ask: str
    expect: str
    check: Callable[[dict], bool] | None = None
    """Whether the arguments carry what the question named."""


def _names(field_name: str, value: str) -> Callable[[dict], bool]:
    return lambda arguments: str(arguments.get(field_name, "")).strip().lower() == value


def _mentions(text: str) -> Callable[[dict], bool]:
    return lambda arguments: text in json.dumps(arguments).lower()


#: Everyday requests, each with one right tool. All read-only.
QUESTIONS = (
    Question("What songs have I added so far?", "list_songs"),
    Question("Find me amp models with 'plexi' in the name.", "search_models", _mentions("plexi")),
    Question("What should I set up next before my first session with the Core?", "setup_status"),
    Question("Show me everything about the song 'going-under-evanescence'.", "show_song", _names("song_id", "going-under-evanescence")),
    Question("Explain the rig draft 'cherub-rock' block by block.", "explain_rig", _names("draft_id", "cherub-rock")),
    Question("How much disk space is the rigsmith workspace using?", "workspace_status"),
)


@dataclass
class Answer:
    ask: str
    expected: str
    called: list[str] = field(default_factory=list)
    ok: bool = False
    seconds: float = 0.0
    note: str = ""


@dataclass
class ModelCheck:
    model: str
    server: str
    answers: list[Answer] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(a.ok for a in self.answers)

    @property
    def median_s(self) -> float:
        return statistics.median(a.seconds for a in self.answers) if self.answers else 0.0

    def verdict(self) -> str:
        share = self.passed / max(1, len(self.answers))
        if share >= 0.99:
            return "drives rigsmith's tools reliably"
        if share >= 0.66:
            return "usable - expect the occasional retry"
        return "not reliable enough for agents - try a larger or tool-tuned model"

    def describe(self) -> str:
        lines = [
            f"{self.model} at {self.server}: {self.passed} of {len(self.answers)} right, "
            f"median {self.median_s:.1f}s a question - {self.verdict()}"
        ]
        for answer in self.answers:
            mark = "ok " if answer.ok else "-- "
            got = ", ".join(answer.called) or "no tool"
            lines.append(f"  {mark}{answer.expected:<18} got {got}" + (f" ({answer.note})" if answer.note else ""))
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(passed=self.passed, total=len(self.answers), median_s=round(self.median_s, 2), verdict=self.verdict(), describe=self.describe())
        return data


def run(provider=None, *, questions=QUESTIONS, progress=None) -> ModelCheck:
    """Ask each question of ``provider`` (default: the local model in Settings)."""
    from ..core import appsettings
    from . import roles, runner
    from .providers import OpenAICompatible

    if provider is None:
        llm = appsettings.load().local_llm
        provider = OpenAICompatible(llm.base_url, model=llm.model, temperature=llm.temperature, thinking=llm.thinking)
    model = provider.pick_model()
    role = roles.get("assistant")
    tools = runner.toolset(role, allow_writes=False)
    check = ModelCheck(model=model, server=getattr(provider, "base_url", provider.describe()))
    for index, question in enumerate(questions):
        if progress:
            progress.check()
            progress.update(index / len(questions), f"asking: {question.ask}")
        answer = Answer(ask=question.ask, expected=question.expect)
        conversation = provider.start(role.system_prompt(), tools)
        conversation.add_user(question.ask)
        started = time.monotonic()
        try:
            turn = conversation.step()
        except Exception as exc:
            answer.note = f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            answer.seconds = round(time.monotonic() - started, 2)
            check.answers.append(answer)
            continue
        answer.seconds = round(time.monotonic() - started, 2)
        answer.called = [call.name for call in turn.calls]
        hit = next((call for call in turn.calls if call.name == question.expect), None)
        if hit is None:
            answer.note = "answered in words instead" if not turn.calls else "the wrong tool"
        elif question.check and not question.check(hit.arguments or {}):
            answer.note = f"right tool, wrong arguments {json.dumps(hit.arguments)[:80]}"
        else:
            answer.ok = True
        check.answers.append(answer)
    return check

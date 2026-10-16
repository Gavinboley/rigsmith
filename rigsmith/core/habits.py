"""What your ears do after autopilot - and an offer to start there.

Autopilot hands over the amp settings that measured closest to the record.
Then you play along and turn knobs. When the same knob goes the same way
on rig after rig, that is no longer about one song: it is your room, your
monitors, your taste. This finds those habits by comparing what autopilot
built with what each draft holds now - so an undone nudge does not count,
and a knob turned by hand counts as much as one turned by words.

Habits are kept per bench: a simulated amp's knobs are not the Core's, and
the turns that correct one say nothing about the other. Autopilot starts
from them only when asked.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

#: The amp controls habits are looked for on.
KNOBS = ("GainA", "Bass", "Mid", "Treble", "Presence", "Master")

#: A habit needs this many rigs turned the same way...
MIN_RIGS = 3
#: ...at least this share of the rigs where the knob was turned at all...
AGREEMENT = 0.75
#: ...and of all the rigs considered, so a few among many is not a habit.
COVERAGE = 0.5
#: Smaller turns than this (share of travel) are not counted as turns.
MIN_MOVE = 0.02

NAMES = {"GainA": "Gain"}


@dataclass
class Habit:
    control: str
    bench: str
    rigs: int
    """Rigs where you turned it this way."""
    of: int
    """Rigs looked at on this bench."""
    move: float
    """The typical turn, as a share of the knob's travel."""

    def describe(self) -> str:
        name = NAMES.get(self.control, self.control)
        return f"{name} {'up' if self.move > 0 else 'down'} on {self.rigs} of {self.of} rigs, typically {self.move:+.0%}"

    def to_dict(self) -> dict:
        return {"control": self.control, "bench": self.bench, "rigs": self.rigs, "of": self.of, "move": round(self.move, 4), "describe": self.describe()}


def start_value(model_id: str, control: str) -> float:
    """Where a control the draft has not set sits: the catalog's default, else noon."""
    from ..rig.catalog import get_catalog

    spec = get_catalog().get(model_id)
    param = spec.param(control) if spec else None
    default = param.default if param is not None else None
    if isinstance(default, (int, float)) and not isinstance(default, bool) and 0.0 <= float(default) <= 1.0:
        return float(default)
    return 0.5


def built(model_id: str, knobs: dict) -> dict:
    """What autopilot built, as a run records it: every habit knob's value."""
    return {"model": model_id, "params": {c: round(float(knobs[c]) if c in knobs else start_value(model_id, c), 4) for c in KNOBS}}


def turns() -> list[tuple[str, dict[str, float]]]:
    """``(bench, {control: change})`` for every finished run whose draft is still here."""
    from . import autopilot, rigs

    found = []
    for run in autopilot.list_runs():
        made = run.built_amp
        if not made or not run.draft_id or run.state != "done":
            continue
        try:
            draft = rigs.get(run.draft_id)
        except Exception:
            continue
        amp = next((b for b in sorted(draft.rig.blocks, key=lambda b: b.slot) if b.model_id == made["model"]), None)
        if amp is None:
            continue
        changes = {}
        for control, was in made["params"].items():
            now = amp.params.get(control, start_value(amp.model_id, control))
            if isinstance(now, (int, float)) and not isinstance(now, bool):
                changes[control] = float(now) - float(was)
        found.append((run.request.bench, changes))
    return found


def habits(bench: str) -> list[Habit]:
    """The knobs you keep turning the same way after autopilot, on ``bench``."""
    looked = [changes for where, changes in turns() if where == bench]
    found = []
    for control in KNOBS:
        moved = [c[control] for c in looked if abs(c.get(control, 0.0)) >= MIN_MOVE]
        if not moved:
            continue
        up = [m for m in moved if m > 0]
        down = [m for m in moved if m < 0]
        same = up if len(up) > len(down) else down
        if len(same) >= MIN_RIGS and len(same) >= AGREEMENT * len(moved) and len(same) >= COVERAGE * len(looked):
            found.append(Habit(control, bench, len(same), len(looked), median(same)))
    return found


def apply(knobs: dict, model_id: str, found: list[Habit]) -> tuple[dict, list[str]]:
    """``knobs`` moved by each habit, kept inside the knob's travel, and what moved."""
    moved = dict(knobs)
    said = []
    for habit in found:
        before = float(moved.get(habit.control, start_value(model_id, habit.control)))
        after = round(min(max(before + habit.move, 0.0), 1.0), 4)
        if after != before:
            moved[habit.control] = after
            said.append(f"{NAMES.get(habit.control, habit.control)} {before:.0%} -> {after:.0%}")
    return moved, said

"""Tweaks said in words - "a bit brighter, tighter low end" - as knob moves.

After listening, what a person knows is a direction, not a number. These
are the directions guitarists use, each mapped to the amp controls that
move a tone that way; "a bit", "a touch" and "a lot" scale the step. It is
deliberately a small vocabulary: anything it does not know is said back
rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The usual step, as a share of the knob's travel, and the softer and
#: harder ones the words ask for.
STEP = 0.08
GENTLE = ("a bit", "slightly", "a touch", "a little", "a hair", "tad")
STRONG = ("a lot", "much", "way", "far", "lots", "really")

#: Each direction, and how far each control moves for one step of it.
DIRECTIONS: dict[str, dict[str, float]] = {
    "brighter": {"Treble": 1.0, "Presence": 1.0},
    "darker": {"Treble": -1.0, "Presence": -1.0},
    "warmer": {"Treble": -0.5, "Bass": 0.5},
    "more gain": {"GainA": 1.0},
    "more drive": {"GainA": 1.0},
    "dirtier": {"GainA": 1.0},
    "heavier": {"GainA": 1.0, "Bass": 0.5},
    "less gain": {"GainA": -1.0},
    "less drive": {"GainA": -1.0},
    "cleaner": {"GainA": -1.0},
    "more bass": {"Bass": 1.0},
    "fuller": {"Bass": 1.0, "Mid": 0.5},
    "thicker": {"Bass": 0.5, "Mid": 1.0},
    "less bass": {"Bass": -1.0},
    "tighter": {"Bass": -1.0},
    "less boomy": {"Bass": -1.0},
    "more mids": {"Mid": 1.0},
    "less mids": {"Mid": -1.0},
    "scooped": {"Mid": -1.0},
    "honky": {"Mid": 1.0},
    "less honky": {"Mid": -1.0},
    "more bite": {"Presence": 1.0, "Mid": 0.5},
    "more cut": {"Presence": 1.0, "Mid": 0.5},
    "smoother": {"Presence": -1.0, "Treble": -0.5},
    "less fizz": {"Presence": -1.0, "Treble": -0.5},
    "less harsh": {"Presence": -1.0, "Treble": -0.5},
    "louder": {"Master": 1.0},
    "quieter": {"Master": -1.0},
    "less bright": {"Treble": -1.0, "Presence": -1.0},
    "more treble": {"Treble": 1.0},
    "less treble": {"Treble": -1.0},
    "more presence": {"Presence": 1.0},
    "less presence": {"Presence": -1.0},
    "less dark": {"Treble": 1.0, "Presence": 1.0},
}


@dataclass
class Nudge:
    moves: dict[str, float] = field(default_factory=dict)
    """Control -> change, as a share of its travel."""
    understood: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def parse(text: str) -> Nudge:
    """``"a bit brighter, a lot tighter"`` -> moves, and what was not understood."""
    result = Nudge()
    for phrase in re.split(r"[,;]|\band\b|\bbut\b", text.lower()):
        phrase = " ".join(phrase.split())
        if not phrase:
            continue
        scale = STEP / 2 if any(word in phrase for word in GENTLE) else STEP * 2 if any(word in phrase for word in STRONG) else STEP
        # The longest direction first, so "less gain" is not read as "gain".
        found = next((d for d in sorted(DIRECTIONS, key=len, reverse=True) if d in phrase), None)
        if found is None:
            result.unknown.append(phrase)
            continue
        result.understood.append(phrase)
        for control, weight in DIRECTIONS[found].items():
            result.moves[control] = result.moves.get(control, 0.0) + weight * scale
    return result


def apply(params: dict, moves: dict[str, float], *, default: float = 0.5) -> dict[str, tuple[float, float]]:
    """New wire values for ``params``, kept inside 0-1: ``{control: (before, after)}``."""
    changed = {}
    for control, change in moves.items():
        before = float(params.get(control, default))
        after = round(min(max(before + change, 0.0), 1.0), 4)
        if after != before:
            changed[control] = (before, after)
    return changed

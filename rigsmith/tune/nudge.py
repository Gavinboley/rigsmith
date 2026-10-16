"""Tweaks said in words - "a bit brighter, tighter low end" - as knob moves.

After listening, what a person knows is a direction, not a number. These
are the directions guitarists use, each mapped to the amp controls that
move a tone that way - or, for "more reverb" or "fewer repeats", to the
reverb or delay; "a bit", "a touch" and "a lot" scale the step. Just
as often what comes out is the complaint rather than the cure - "too
boomy", "too much gain", "not enough bite" - so those are read as the
direction that fixes them. It is deliberately a small vocabulary: anything
it does not know is said back rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: The usual step, as a share of the knob's travel, and the softer and
#: harder ones the words ask for.
STEP = 0.08
GENTLE = ("a bit", "slightly", "a touch", "a little", "a hair", "tad")
STRONG = ("a lot", "much", "way", "far", "lots", "really")

#: Each direction, and how far each control moves for one step of it. A
#: control is the amp's, unless it names the effect it is on - "reverb:wet"
#: is the first reverb's wet level, whatever that model calls it.
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
    "more mid": {"Mid": 1.0},
    "less mid": {"Mid": -1.0},
    "more low end": {"Bass": 1.0},
    "less low end": {"Bass": -1.0},
    "less mud": {"Bass": -1.0, "Mid": -0.5},
    "less boxy": {"Mid": -1.0},
    "more sparkle": {"Treble": 1.0},
    "crunchier": {"GainA": 1.0},
    "more reverb": {"reverb:wet": 1.0},
    "less reverb": {"reverb:wet": -1.0},
    "more delay": {"delay:wet": 1.0},
    "less delay": {"delay:wet": -1.0},
    "wetter": {"reverb:wet": 1.0, "delay:wet": 1.0},
    "drier": {"reverb:wet": -1.0, "delay:wet": -1.0},
    "more repeats": {"delay:repeats": 1.0},
    "fewer repeats": {"delay:repeats": -1.0},
    "less repeats": {"delay:repeats": -1.0},
}

#: An effect's controls by what they do, and the names models give them, in
#: the order to look for them.
EFFECT_CONTROLS: dict[str, tuple[str, ...]] = {
    "wet": ("Mix", "DelayWet", "Level"),
    "repeats": ("Feedback", "DelayFdbk", "Fdbk"),
}

#: "Too <this>" -> the direction that fixes it.
COMPLAINTS: dict[str, str] = {
    "bright": "darker",
    "shrill": "darker",
    "dark": "brighter",
    "dull": "brighter",
    "harsh": "less harsh",
    "fizzy": "less fizz",
    "boomy": "tighter",
    "flabby": "tighter",
    "loose": "tighter",
    "muddy": "less mud",
    "thin": "fuller",
    "clean": "more gain",
    "dirty": "less gain",
    "distorted": "less gain",
    "saturated": "less gain",
    "scooped": "more mids",
    "hollow": "more mids",
    "honky": "less mids",
    "nasal": "less mids",
    "boxy": "less mids",
    "quiet": "louder",
    "loud": "quieter",
    "wet": "drier",
    "washy": "less reverb",
    "dry": "wetter",
}


@dataclass
class Nudge:
    moves: dict[str, float] = field(default_factory=dict)
    """Control -> change, as a share of its travel."""
    understood: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)


def parse(text: str) -> Nudge:
    """``"a bit brighter, too boomy"`` -> moves, and what was not understood."""
    result = Nudge()
    for phrase in re.split(r"[,;]|\band\b|\bbut\b", text.lower()):
        phrase = " ".join(phrase.split())
        if not phrase:
            continue
        said = phrase
        # "too much bass" is "less bass", "not enough bite" is "more bite".
        phrase = re.sub(r"\btoo much\b", "less", re.sub(r"\bnot enough\b", "more", phrase))
        scale = STEP / 2 if any(word in phrase for word in GENTLE) else STEP * 2 if any(word in phrase for word in STRONG) else STEP
        complaint = re.search(r"\btoo (\w+)", phrase)
        found = COMPLAINTS.get(complaint.group(1)) if complaint else None
        # The longest direction first, so "less gain" is not read as "gain".
        found = found or next((d for d in sorted(DIRECTIONS, key=len, reverse=True) if d in phrase), None)
        if found is None:
            result.unknown.append(said)
            continue
        result.understood.append(said)
        for control, weight in DIRECTIONS[found].items():
            result.moves[control] = result.moves.get(control, 0.0) + weight * scale
    return result


def target(control: str) -> tuple[str, str]:
    """``"reverb:wet"`` -> ``("reverb", "wet")``; an amp control -> ``("amp", control)``."""
    kind, _, name = control.rpartition(":")
    return kind or "amp", name


def effect_kind(model_id: str, subcategory: str | None) -> str | None:
    """"reverb" or "delay" for an effect model, or None.

    By the catalog's subcategory, and by name as well: the Core files its
    AIR Reverb and AIR Delay under vocal effects.
    """
    name = model_id.lower()
    if subcategory == "reverb" or "verb" in name or "shimmer" in name:
        return "reverb"
    if subcategory == "delay" or "delay" in name or "echo" in name:
        return "delay"
    return None


def apply(params: dict, moves: dict[str, float], *, default: float = 0.5) -> dict[str, tuple[float, float]]:
    """New wire values for ``params``, kept inside 0-1: ``{control: (before, after)}``."""
    changed = {}
    for control, change in moves.items():
        before = float(params.get(control, default))
        after = round(min(max(before + change, 0.0), 1.0), 4)
        if after != before:
            changed[control] = (before, after)
    return changed

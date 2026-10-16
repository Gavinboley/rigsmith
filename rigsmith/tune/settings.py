"""Amp settings as people write them, as the Core's controls.

Research records what a player said or a rig rundown showed - "gain 7",
"mids at noon", "treble 2 o'clock", "presence dimed" - and that is a far
better place for the tuner to start than every knob at noon. This turns
those words into 0-1 positions on the Core's amp controls; anything it
cannot read is left out rather than guessed.
"""

from __future__ import annotations

import re

#: How settings get named, and the Core control each one is.
NAMES = {
    "gain": "GainA", "drive": "GainA", "preamp": "GainA", "input": "GainA",
    "bass": "Bass", "low": "Bass", "lows": "Bass",
    "mid": "Mid", "mids": "Mid", "middle": "Mid",
    "treble": "Treble", "high": "Treble", "highs": "Treble",
    "presence": "Presence",
    "master": "Master", "volume": "Master",
}

WORDS = {
    "off": 0.0, "min": 0.0, "minimum": 0.0, "zero": 0.0,
    "noon": 0.5, "half": 0.5, "middle": 0.5,
    "max": 1.0, "maximum": 1.0, "full": 1.0, "dimed": 1.0, "cranked": 1.0,
}

_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*o'?\s*clock")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


def position(value) -> float | None:
    """One setting as a 0-1 knob position, or None when it cannot be read.

    0-10 is a dial, past 10 up to 100 a percentage; a clock position runs
    from 7 o'clock (off) through noon to 5 o'clock (full).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number < 0:
            return None
        return min(number / 10.0, 1.0) if number <= 10 else min(number / 100.0, 1.0) if number <= 100 else None
    text = str(value).strip().lower()
    if not text:
        return None
    clock = _CLOCK.search(text)
    if clock:
        hour = int(clock.group(1)) % 12 + (int(clock.group(2) or 0) / 60.0)
        return round(min(max(((hour - 7) % 12) / 10.0, 0.0), 1.0), 3)
    for word, known in WORDS.items():
        if re.search(rf"\b{word}\b", text):
            return known
    number = _NUMBER.search(text)
    if number is None:
        return None
    figure = float(number.group())
    if "%" in text:
        return min(max(figure / 100.0, 0.0), 1.0)
    out_of = re.search(r"/\s*(\d+)", text)
    if out_of and float(out_of.group(1)) > 0:
        return min(max(figure / float(out_of.group(1)), 0.0), 1.0)
    return position(figure)


def amp_controls(settings: dict | None) -> dict[str, float]:
    """``{"gain": "7", "mids": "noon"}`` -> ``{"GainA": 0.7, "Mid": 0.5}``."""
    found: dict[str, float] = {}
    for name, value in (settings or {}).items():
        control = NAMES.get(str(name).strip().lower())
        if control is None:
            continue
        at = position(value)
        if at is not None:
            found[control] = at
    return found

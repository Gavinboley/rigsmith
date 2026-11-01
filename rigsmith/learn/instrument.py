"""Strings, frets and pitches: the instrument as arithmetic.

A pitch on a guitar is ambiguous in a way it is not on a piano. The E at
329.6 Hz is the open high string, the fifth fret of the B string, the ninth of
the G, the fourteenth of the D - and each of those *sounds different*: a wound
string at the fourteenth fret is darker and dies sooner than an open plain
one. So a profile keyed only by pitch would hand back the wrong timbre for half
the notes it was asked for, and everything here keeps the string alongside
the note.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from ..core.errors import Invalid

A4_HZ = 440.0
A4_MIDI = 69

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
_FLATS = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#"}

#: Tunings by name, lowest string first, written as note names. Six strings
#: unless the name says otherwise. Kept as names rather than MIDI numbers so
#: the table can be read by a guitarist and checked against a tuner.
TUNINGS: dict[str, tuple[str, ...]] = {
    "E standard": ("E2", "A2", "D3", "G3", "B3", "E4"),
    "Eb standard": ("D#2", "G#2", "C#3", "F#3", "A#3", "D#4"),
    "D standard": ("D2", "G2", "C3", "F3", "A3", "D4"),
    "C# standard": ("C#2", "F#2", "B2", "E3", "G#3", "C#4"),
    "C standard": ("C2", "F2", "A#2", "D#3", "G3", "C4"),
    "B standard": ("B1", "E2", "A2", "D3", "F#3", "B3"),
    "drop D": ("D2", "A2", "D3", "G3", "B3", "E4"),
    "drop C#": ("C#2", "G#2", "C#3", "F#3", "A#3", "D#4"),
    "drop C": ("C2", "G2", "C3", "F3", "A3", "D4"),
    "drop B": ("B1", "F#2", "B2", "E3", "G#3", "C#4"),
    "drop A": ("A1", "E2", "A2", "D3", "F#3", "B3"),
    "open G": ("D2", "G2", "D3", "G3", "B3", "D4"),
    "open D": ("D2", "A2", "D3", "F#3", "A3", "D4"),
    "open E": ("E2", "B2", "E3", "G#3", "B3", "E4"),
    "DADGAD": ("D2", "A2", "D3", "G3", "A3", "D4"),
    "7-string standard": ("B1", "E2", "A2", "D3", "G3", "B3", "E4"),
    "7-string drop A": ("A1", "E2", "A2", "D3", "G3", "B3", "E4"),
    "Bass E standard": ("E1", "A1", "D2", "G2"),
    "Bass drop D": ("D1", "A1", "D2", "G2"),
    "Bass Eb standard": ("D#1", "G#1", "C#2", "F#2"),
    "Bass D standard": ("D1", "G1", "C2", "F2"),
    "Bass C standard": ("C1", "F1", "A#1", "D#2"),
    "Bass 5-string standard": ("B0", "E1", "A1", "D2", "G2"),
    "Bass 5-string drop A": ("A0", "E1", "A1", "D2", "G2"),
    "Bass 6-string standard": ("B0", "E1", "A1", "D2", "G2", "C3"),
}

DEFAULT_TUNING = "E standard"

#: How far up the neck the guided protocols go by default. Twelve covers every
#: pitch class on every string, which is what a sampler needs; past that the
#: voice model extrapolates better than a user's patience does.
DEFAULT_FRETS = 12

#: The highest fret any guitar here is assumed to have.
MAX_FRET = 24


def midi_to_hz(midi: float) -> float:
    return A4_HZ * 2.0 ** ((midi - A4_MIDI) / 12.0)


def hz_to_midi(hz: float) -> float:
    if hz <= 0:
        raise Invalid(f"{hz} Hz is not a pitch")
    return A4_MIDI + 12.0 * math.log2(hz / A4_HZ)


def note_to_midi(name: str) -> int:
    """``E2`` -> 40, ``Bb3`` -> 58. Flats and sharps both accepted."""
    match = re.fullmatch(r"\s*([A-Ga-g])([#b]?)(-?\d)\s*", str(name))
    if not match:
        raise Invalid(f"'{name}' is not a note name", hint="Write it like E2, F#3 or Bb1.")
    letter, accidental, octave = match.groups()
    pitch = letter.upper() + accidental
    pitch = _FLATS.get(pitch, pitch)
    if pitch not in NOTE_NAMES:
        # E#, B#, Cb, Fb - legal in theory, absent from every tuning chart.
        raise Invalid(f"'{name}' is not a note this table spells")
    return NOTE_NAMES.index(pitch) + 12 * (int(octave) + 1)


def midi_to_note(midi: float) -> str:
    nearest = int(round(midi))
    return f"{NOTE_NAMES[nearest % 12]}{nearest // 12 - 1}"


def cents_between(hz: float, midi: float) -> float:
    """How far ``hz`` is from the exact pitch of ``midi``, in cents."""
    return 100.0 * (hz_to_midi(hz) - midi)


@dataclass(frozen=True)
class Position:
    """Where a note is played: string 1 is the highest, as guitarists number them."""

    string: int
    fret: int

    def describe(self) -> str:
        return f"string {self.string} fret {self.fret}"


@dataclass(frozen=True)
class Tuning:
    """A named set of open strings."""

    name: str
    open_strings: tuple[int, ...]
    """MIDI numbers, lowest string first."""

    @property
    def string_count(self) -> int:
        return len(self.open_strings)

    def open_midi(self, string: int) -> int:
        """Open pitch of a string numbered the guitarist's way (1 = highest)."""
        if not 1 <= string <= self.string_count:
            raise Invalid(
                f"string {string} does not exist on a {self.string_count}-string "
                f"in {self.name}"
            )
        return self.open_strings[self.string_count - string]

    def midi_at(self, position: Position) -> int:
        return self.open_midi(position.string) + position.fret

    def positions(self, midi: int, *, max_fret: int = MAX_FRET) -> list[Position]:
        """Every place this pitch can be played, lowest fret first."""
        found = []
        for string in range(1, self.string_count + 1):
            fret = midi - self.open_midi(string)
            if 0 <= fret <= max_fret:
                found.append(Position(string, fret))
        found.sort(key=lambda p: (p.fret, p.string))
        return found

    @property
    def lowest(self) -> int:
        return min(self.open_strings)

    @property
    def is_bass(self) -> bool:
        """Whether this tuning represents a bass guitar (4, 5, or 6-string bass)."""
        return (
            "bass" in self.name.lower()
            or self.lowest <= 32
            or (self.string_count <= 5 and self.lowest <= 38)
        )

    def range(self, *, max_fret: int = MAX_FRET) -> tuple[int, int]:
        return self.lowest, max(self.open_strings) + max_fret

    def describe(self) -> str:
        notes = " ".join(midi_to_note(m) for m in self.open_strings)
        return f"{self.name} ({notes})"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "strings": [midi_to_note(m) for m in self.open_strings],
            "open_midi": list(self.open_strings),
            "open_hz": [round(midi_to_hz(m), 2) for m in self.open_strings],
            "is_bass": self.is_bass,
        }


def tuning(name: str | None = None) -> Tuning:
    """Look a tuning up by name, or parse one written as notes.

    ``"drop D"``, ``"Drop d"`` and ``"D2 A2 D3 G3 B3 E4"`` all work; the last
    form is how anything not in the table gets in.
    """
    text = (name or DEFAULT_TUNING).strip()
    for known, notes in TUNINGS.items():
        if known.lower() == text.lower():
            return Tuning(known, tuple(note_to_midi(n) for n in notes))

    parts = re.split(r"[\s,]+", text)
    if len(parts) >= 4:
        try:
            midis = tuple(note_to_midi(p) for p in parts)
        except Invalid:
            midis = ()
        if midis:
            if list(midis) != sorted(midis) and midis[0] > midis[-1]:
                # Written highest first, as some tab sites do.
                midis = tuple(reversed(midis))
            return Tuning(text, midis)

    raise Invalid(
        f"unknown tuning '{name}'",
        hint="Known: " + ", ".join(TUNINGS) + ". Or write the open strings, "
        "lowest first, e.g. 'D2 A2 D3 G3 B3 E4'.",
    )


#: Where a note is placed by :func:`assign_strings`: no higher than this
#: fret, and a string still ringing costs this much more than a reach.
PLACING_MAX_FRET = 15
BUSY_STRING_COST = 8.0


def assign_strings(notes, tune: Tuning, *, max_fret: int = PLACING_MAX_FRET) -> list[int | None]:
    """A string for each note, as a hand would play them.

    ``notes`` are ``(start, end, midi)`` in time order. Each goes near where
    the hand already is - an open string is free - and not onto a string
    still ringing another note unless there is nowhere else: an arpeggio's
    notes land on different strings and ring together, while a run up one
    string mutes each note with the next. ``None`` where the pitch is off
    the neck.
    """
    ringing: dict[int, float] = {}
    frets: list[int] = []
    placed: list[int | None] = []
    for start, end, midi in notes:
        places = tune.positions(int(round(midi)), max_fret=max_fret)
        if not places:
            placed.append(None)
            continue
        recent = sorted(frets[-4:])
        hand = recent[len(recent) // 2] if recent else None

        def cost(place: Position) -> float:
            reach = 0.0 if place.fret == 0 else (abs(place.fret - hand) if hand is not None else 0.3 * place.fret)
            busy = BUSY_STRING_COST if ringing.get(place.string, -1.0) > start + 0.02 else 0.0
            return reach + busy + 0.05 * place.fret

        best = min(places, key=cost)
        placed.append(best.string)
        ringing[best.string] = end
        if best.fret > 0:
            frets.append(best.fret)
    return placed


def tunings() -> list[Tuning]:
    return [tuning(name) for name in TUNINGS]

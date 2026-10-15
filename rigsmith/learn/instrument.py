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


def tunings() -> list[Tuning]:
    return [tuning(name) for name in TUNINGS]

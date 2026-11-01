"""What to play so the instrument can be learned.

A recording of you noodling contains a lot of notes and almost no labels: the
E at 329 Hz could have been any of four places on the neck, and which one is
the difference between a bright open string and a dull wound one. A protocol
fixes that by deciding the order in advance. Play what it says, in the order
it says, and every note arrives already knowing its string and fret.

The aligner is what makes that forgiving. People miss a note, play one twice,
hit the wrong fret, or stop halfway. :func:`align` matches what was heard
against what was asked for the way a diff matches two files - so a skipped
step shows up as *missing*, not as every later note shifted one place along
and mislabelled.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..core.errors import Invalid, NotFound
from .instrument import DEFAULT_FRETS, Tuning, midi_to_note, tuning as get_tuning
from .segment import Note

#: How far a heard note may sit from the asked-for pitch and still be that
#: step. More than a semitone is the wrong fret, not a sharp guitar.
MATCH_SEMITONES = 0.6


@dataclass
class Step:
    """One thing to play."""

    string: int | None
    fret: int | None
    midi: int | None
    """The lowest pitch expected. ``None`` for a step with no fixed pitch."""
    technique: str = "pick"
    """pick, palm-mute, chord, strum, harmonic."""
    dynamic: str = "mf"
    """pp, mf, ff - how hard."""
    prompt: str = ""

    @property
    def label(self) -> str:
        if self.string is None:
            return self.prompt
        name = midi_to_note(self.midi) if self.midi is not None else "?"
        return f"s{self.string}f{self.fret} {name}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["label"] = self.label
        return data


@dataclass
class Protocol:
    """A sequence of steps, and how long to leave between them."""

    protocol_id: str
    name: str
    description: str
    tuning: str
    steps: list[Step] = field(default_factory=list)
    spacing_s: float = 1.5
    """Seconds between steps when following the on-screen cursor."""
    purpose: str = "sampler"

    @property
    def seconds(self) -> float:
        return len(self.steps) * self.spacing_s + 3.0

    def describe(self) -> str:
        minutes = self.seconds / 60
        return (
            f"{self.protocol_id}: {self.name} - {len(self.steps)} steps, about "
            f"{minutes:.1f} min. {self.description}"
        )

    def to_dict(self) -> dict:
        return {
            "protocol_id": self.protocol_id,
            "name": self.name,
            "description": self.description,
            "tuning": self.tuning,
            "spacing_s": self.spacing_s,
            "seconds": round(self.seconds, 1),
            "purpose": self.purpose,
            "steps": [s.to_dict() for s in self.steps],
        }


def _note_step(tune: Tuning, string: int, fret: int, **extra) -> Step:
    midi = tune.open_midi(string) + fret
    return Step(string=string, fret=fret, midi=midi, prompt=f"string {string}, fret {fret}", **extra)


def _strings_low_to_high(tune: Tuning) -> list[int]:
    return list(range(tune.string_count, 0, -1))


def quick(tune: Tuning) -> Protocol:
    frets = (0, 5, 7, 12)
    steps = [_note_step(tune, s, f) for s in _strings_low_to_high(tune) for f in frets]
    return Protocol(
        "quick", "Quick profile",
        "Four notes per string: open, 5th, 7th and 12th fret. Enough for the "
        "voice model to cover the neck; the sampler fills the gaps by shifting.",
        tune.name, steps, spacing_s=1.6,
    )


def chromatic(tune: Tuning, frets: int = DEFAULT_FRETS) -> Protocol:
    if not 1 <= frets <= 24:
        raise Invalid(f"{frets} frets is not a guitar", hint="Use 1-24.")
    steps = [
        _note_step(tune, s, f)
        for s in _strings_low_to_high(tune)
        for f in range(0, frets + 1)
    ]
    return Protocol(
        "chromatic", f"Every note, frets 0-{frets}",
        "Every fret of every string, low string first, one firm note each. "
        "The most accurate profile there is: every note is a real sample of "
        "your guitar at the place it is actually played.",
        tune.name, steps, spacing_s=1.3,
    )


def dynamics(tune: Tuning) -> Protocol:
    steps = []
    for s in _strings_low_to_high(tune):
        for f in (0, 7):
            for level, words in (("pp", "softly"), ("mf", "normally"), ("ff", "hard")):
                step = _note_step(tune, s, f, dynamic=level)
                step.prompt = f"string {s}, fret {f}, {words}"
                steps.append(step)
    return Protocol(
        "dynamics", "Dynamics ladder",
        "Two notes per string, each soft, medium and hard. How your pickups "
        "respond to picking strength is what an amp's gain reacts to, so this "
        "is what makes a synthesised part push the amp like you would.",
        tune.name, steps, spacing_s=1.4, purpose="velocity",
    )


def techniques(tune: Tuning) -> Protocol:
    low = tune.string_count
    steps: list[Step] = []
    for _ in range(4):
        step = _note_step(tune, low, 0, technique="palm-mute")
        step.prompt = f"palm-muted open string {low}"
        steps.append(step)
    for root_fret in (0, 5, 7):
        midi = tune.open_midi(low) + root_fret
        if tune.is_bass:
            steps.append(Step(low, root_fret, midi, "chord", "mf", f"octave / fifth double-stop, root on string {low} fret {root_fret}"))
        else:
            steps.append(Step(low, root_fret, midi, "chord", "mf", f"power chord, root on string {low} fret {root_fret}"))
    if tune.is_bass:
        for s in _strings_low_to_high(tune):
            step = _note_step(tune, s, 0, dynamic="ff")
            step.prompt = f"hard thumb/slap or dig-in on open string {s}, let it ring"
            steps.append(step)
        steps.append(_note_step(tune, 1, 12, technique="harmonic"))
        steps[-1].prompt = "natural harmonic, string 1 fret 12"
    else:
        for name in ("open E", "open A", "open D", "open G", "open C"):
            steps.append(Step(None, None, None, "strum", "mf", f"{name} chord, one strum, let it ring"))
        steps.append(_note_step(tune, max(1, low - 3), 12, technique="harmonic"))
        steps[-1].prompt = f"natural harmonic, string {max(1, low - 3)} fret 12"
    return Protocol(
        "techniques", "Techniques",
        "Mutes, double-stops/chords and hard attacks. Not used for the sampler - "
        "used to check that gain staging and note definition behave the way "
        "your real playing will push them.",
        tune.name, steps, spacing_s=2.2, purpose="reference",
    )


def free(tune: Tuning) -> Protocol:
    return Protocol(
        "free", "Anything",
        "Play whatever you like. Notes are labelled by pitch alone, using the "
        "lowest position on the neck - less accurate than a guided protocol, "
        "and fine for a first profile.",
        tune.name, [], spacing_s=0, purpose="sampler",
    )


BUILDERS = {
    "quick": quick,
    "chromatic": chromatic,
    "dynamics": dynamics,
    "techniques": techniques,
    "free": free,
}


def get(protocol_id: str, *, tuning: str | None = None, frets: int | None = None) -> Protocol:
    builder = BUILDERS.get(protocol_id)
    if builder is None:
        raise NotFound(
            f"no protocol '{protocol_id}'", hint="Protocols: " + ", ".join(BUILDERS)
        )
    tune = get_tuning(tuning)
    if protocol_id == "chromatic" and frets is not None:
        return chromatic(tune, frets)
    return builder(tune)


def available(tuning: str | None = None) -> list[Protocol]:
    tune = get_tuning(tuning)
    return [builder(tune) for builder in BUILDERS.values()]


# --------------------------------------------------------------------------
# alignment
# --------------------------------------------------------------------------


@dataclass
class Alignment:
    """How a recording lined up against a protocol."""

    protocol_id: str
    matched: list[tuple[int, int]] = field(default_factory=list)
    """``(step index, note index)`` pairs."""
    missing: list[int] = field(default_factory=list)
    """Steps nobody played."""
    extra: list[int] = field(default_factory=list)
    """Notes nobody asked for."""
    wrong: list[tuple[int, int]] = field(default_factory=list)
    """Steps answered with the wrong pitch - usually the wrong fret."""

    @property
    def coverage(self) -> float:
        total = len(self.matched) + len(self.missing) + len(self.wrong)
        return len(self.matched) / total if total else 0.0

    def describe(self, protocol: Protocol | None = None) -> str:
        lines = [
            f"{len(self.matched)} step(s) matched, {len(self.missing)} missing, "
            f"{len(self.wrong)} wrong pitch, {len(self.extra)} extra note(s) "
            f"({self.coverage:.0%} coverage)"
        ]
        if protocol and self.missing:
            names = ", ".join(protocol.steps[i].label for i in self.missing[:12])
            more = "" if len(self.missing) <= 12 else f" and {len(self.missing) - 12} more"
            lines.append(f"  missing: {names}{more}")
        if protocol and self.wrong:
            lines.append(
                "  wrong pitch: "
                + ", ".join(protocol.steps[i].label for i, _ in self.wrong[:8])
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "protocol_id": self.protocol_id,
            "matched": self.matched,
            "missing": self.missing,
            "extra": self.extra,
            "wrong": self.wrong,
            "coverage": round(self.coverage, 3),
        }


def _pitch_cost(step: Step, note: Note) -> float:
    if step.midi is None or step.technique in ("strum",):
        return 0.0
    if not note.pitched:
        # Chords and mutes come out unpitched; for those steps that is right.
        return 0.2 if step.technique in ("chord", "palm-mute") else 1.5
    distance = abs(note.midi - step.midi)
    if step.technique == "harmonic":
        # A 12th-fret harmonic sounds an octave above the open string, which
        # is the fretted pitch - but the autocorrelator may land an octave out.
        distance = min(distance, abs(note.midi - 12 - step.midi), abs(note.midi + 12 - step.midi))
    return min(distance, 4.0) / MATCH_SEMITONES


def align(protocol: Protocol, notes: list[Note]) -> Alignment:
    """Match heard notes to protocol steps, diff-style.

    Dynamic programming over (steps, notes) with three moves: pair a step with
    a note, skip a step (missed), skip a note (extra). Pairing costs the pitch
    distance; skipping costs a flat penalty set so that a wrong-fret note is
    still paired (and flagged) when it sits where the step should be, rather
    than producing a miss and an extra.
    """
    result = Alignment(protocol.protocol_id)
    steps = protocol.steps
    if not steps:
        # Nothing was asked for, so everything played is unasked-for.
        result.extra = list(range(len(notes)))
        return result

    skip_step, skip_note = 2.2, 1.6
    n, m = len(steps), len(notes)
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    move = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            here = cost[i][j]
            if here == inf:
                continue
            if i < n and j < m:
                c = here + min(_pitch_cost(steps[i], notes[j]), 3.5)
                if c < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1] = c
                    move[i + 1][j + 1] = "pair"
            if i < n and here + skip_step < cost[i + 1][j]:
                cost[i + 1][j] = here + skip_step
                move[i + 1][j] = "miss"
            if j < m and here + skip_note < cost[i][j + 1]:
                cost[i][j + 1] = here + skip_note
                move[i][j + 1] = "extra"

    # Trailing steps nobody reached are missing; walk back from the corner.
    i, j = n, m
    while i > 0 or j > 0:
        step_move = move[i][j]
        if step_move == "pair":
            i, j = i - 1, j - 1
            if _pitch_cost(steps[i], notes[j]) <= 1.0:
                result.matched.append((i, j))
            else:
                result.wrong.append((i, j))
        elif step_move == "miss":
            i -= 1
            result.missing.append(i)
        else:
            j -= 1
            result.extra.append(j)
    result.matched.reverse()
    result.missing.reverse()
    result.extra.reverse()
    result.wrong.reverse()
    return result


def label(protocol: Protocol, notes: list[Note], alignment: Alignment | None = None) -> list[Note]:
    """Write string, fret and step labels onto the notes that matched.

    With the ``free`` protocol, or for notes nobody asked for, the lowest
    position on the neck is assumed and flagged as an assumption.
    """
    alignment = alignment or align(protocol, notes)
    tune = get_tuning(protocol.tuning)
    for step_index, note_index in alignment.matched:
        step = protocol.steps[step_index]
        note = notes[note_index]
        note.string, note.fret = step.string, step.fret
        note.label = step.label
        if step.technique != "pick":
            note.flags.append(step.technique)
        if step.dynamic != "mf":
            note.flags.append(step.dynamic)
    for step_index, note_index in alignment.wrong:
        notes[note_index].flags.append(f"expected {protocol.steps[step_index].label}")

    assigned = {j for _, j in alignment.matched}
    for index, note in enumerate(notes):
        if index in assigned or not note.pitched:
            continue
        positions = tune.positions(note.nearest)
        if positions:
            note.string, note.fret = positions[0].string, positions[0].fret
            note.flags.append("position assumed")
    return notes

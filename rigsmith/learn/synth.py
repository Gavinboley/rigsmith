"""Playing a part on a learned guitar.

Given a sequence of notes - from a transcription of a record, from a MIDI
file, typed in by hand - render the DI your guitar would have produced playing
it. That DI is what candidate rigs are then heard through.

It is a sampler, and deliberately a plain one. For each note the closest
recorded sample is chosen (pitch first, then string, then how hard it was
played), shifted the small remaining distance by resampling, levelled to the
note's dynamic, and laid into the output. Resampling changes the length along
with the pitch, which is what a fretted string does anyway: higher up the
neck, shorter string, faster decay. For a semitone or two it is inaudible as
processing, which is why a guided protocol records every fret.

Where a sample would have to be shifted further than :data:`MAX_SHIFT` - a
fret the profile never heard - the voice model (:mod:`.voice`) takes over if
one has been fitted, and the plucked-string model (:mod:`.pluck`) if not. Every
render records which of the three played each note, because "your guitar"
and "a guess at your guitar" are different claims.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .instrument import midi_to_note

#: Furthest a sample is shifted, in semitones, before something else plays the
#: note. Three is where resampling starts to sound like resampling.
MAX_SHIFT = 3.0

#: Fade at the end of a note, when the next note on the same string stops it.
DAMP_S = 0.03


@dataclass
class NoteEvent:
    """One note to play."""

    start: float
    duration: float
    midi: float
    velocity: float = 0.75
    """0-1. Mapped onto the profile's own dynamic range."""
    string: int | None = None
    technique: str = "pick"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "NoteEvent":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Rendered:
    """A synthesised DI and an honest account of how it was made."""

    audio: np.ndarray
    sample_rate: int
    voices: dict[str, int] = field(default_factory=dict)
    """How many notes each source played: sample, voice, pluck."""
    max_shift: float = 0.0
    notes: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return len(self.audio) / self.sample_rate

    @property
    def from_samples(self) -> float:
        return self.voices.get("sample", 0) / self.notes if self.notes else 0.0

    def describe(self) -> str:
        parts = ", ".join(f"{count} from {name}" for name, count in self.voices.items() if count)
        lines = [f"{self.notes} note(s), {self.duration:.1f}s: {parts or 'nothing played'}"]
        if self.max_shift:
            lines.append(f"  largest shift {self.max_shift:.1f} semitones")
        lines.extend(f"  note: {w}" for w in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "sample_rate": self.sample_rate,
            "notes": self.notes,
            "voices": self.voices,
            "from_samples": round(self.from_samples, 3),
            "max_shift": round(self.max_shift, 2),
            "warnings": self.warnings,
            "describe": self.describe(),
        }


def shift(audio: np.ndarray, semitones: float) -> np.ndarray:
    """Pitch-shift by resampling, which also scales the length.

    Up a semitone plays the sample 6% faster - exactly what fretting one fret
    higher does to a string's period, and close to what it does to its decay.
    """
    if abs(semitones) < 1e-3:
        return audio
    ratio = 2.0 ** (semitones / 12.0)
    length = max(2, int(round(len(audio) / ratio)))
    positions = np.arange(length) * ratio
    try:
        from scipy.signal import resample_poly
        from fractions import Fraction

        fraction = Fraction(1 / ratio).limit_denominator(240)
        if fraction.numerator and fraction.denominator:
            out = resample_poly(audio, fraction.numerator, fraction.denominator)
            return out[:length] if len(out) >= length else np.pad(out, (0, length - len(out)))
    except ImportError:  # pragma: no cover - scipy is a hard dependency of learning
        pass
    return np.interp(positions, np.arange(len(audio)), audio, right=0.0)


def _velocity_to_db(velocity: float, median_db: float, p95_db: float) -> float:
    """Map 0-1 onto the profile's own range: 0.75 lands on its median peak."""
    spread = max(3.0, p95_db - median_db)
    return median_db + (float(np.clip(velocity, 0.05, 1.0)) - 0.75) / 0.25 * spread


def render(
    events: list[NoteEvent],
    profile=None,
    *,
    sample_dir: Path | None = None,
    sample_rate: int = 48000,
    tail: float = 1.0,
    voice=None,
    pickup: str = "bridge",
    noise_floor_db: float | None = None,
    seed: int = 0,
) -> Rendered:
    """Render note events into a DI.

    Args:
        profile: A :class:`~rigsmith.learn.profile.GuitarProfile`, or ``None``
            to play everything on the synthetic string.
        sample_dir: Where the profile's sample files live.
        voice: A fitted :class:`~rigsmith.learn.voice.VoiceModel`, used for
            notes too far from any sample.
        noise_floor_db: Add the guitar's own noise floor (and hum) back under
            the render. Off by default; on when the render is going through
            a high-gain amp, where the floor is part of the sound.
    """
    from .pluck import pluck
    from ..match.render_offline import load_audio, resample

    events = sorted(events, key=lambda e: e.start)
    rendered = Rendered(audio=np.zeros(1), sample_rate=sample_rate)
    rendered.voices = {"sample": 0, "voice": 0, "pluck": 0}
    if not events:
        rendered.audio = np.zeros(int(tail * sample_rate))
        return rendered

    end = max(e.start + e.duration for e in events) + tail
    out = np.zeros(int(end * sample_rate) + 1)
    cache: dict[str, np.ndarray] = {}
    rng = np.random.default_rng(seed)

    median_db = profile.fingerprint.median_peak_db if profile else -12.0
    p95_db = profile.fingerprint.p95_peak_db if profile else -6.0
    if profile and median_db <= -100:
        median_db, p95_db = -12.0, -6.0

    # Which notes are cut short by the next one on the same string. Without a
    # string, the next note within a semitone-and-a-half is assumed to be on
    # it - a run down one string mutes each note as the next is picked.
    stops: list[float | None] = [None] * len(events)
    for i, event in enumerate(events):
        for later in events[i + 1 :]:
            if later.start <= event.start + 0.02:
                continue
            same = (
                event.string is not None and later.string == event.string
            ) or (event.string is None and abs(later.midi - event.midi) <= 4.5)
            if same:
                stops[i] = later.start
                break

    for index, event in enumerate(events):
        target_db = _velocity_to_db(event.velocity, median_db, p95_db)
        length_s = event.duration + 0.35
        if stops[index] is not None:
            length_s = min(length_s, stops[index] - event.start + DAMP_S)
        note: np.ndarray | None = None
        source = "pluck"

        sample, needed = (None, 0.0)
        if profile is not None and profile.samples:
            sample, needed = profile.choose(
                event.midi, peak_db=target_db, string=event.string, technique=event.technique
            )
        if sample is not None and abs(needed) <= MAX_SHIFT and sample_dir is not None:
            path = sample_dir / sample.file
            if str(path) not in cache:
                try:
                    audio, rate = load_audio(path)
                    if rate != sample_rate:
                        audio = resample(audio, rate, sample_rate)
                    cache[str(path)] = audio
                except Exception as exc:  # a missing sample degrades, it does not abort
                    rendered.warnings.append(f"sample {sample.file} unreadable: {exc}")
                    cache[str(path)] = np.zeros(0)
            raw = cache[str(path)]
            if len(raw):
                note = shift(raw, needed)
                rendered.max_shift = max(rendered.max_shift, abs(needed))
                # Level relative to the sample's own peak, so a soft sample
                # asked to play loud is raised rather than replaced.
                note = note * 10 ** ((target_db - sample.peak_db) / 20)
                source = "sample"

        if note is None and voice is not None:
            try:
                note = voice.synthesize(event.midi, length_s, string=event.string, peak_db=target_db, sample_rate=sample_rate)
                source = "voice"
            except Exception as exc:
                rendered.warnings.append(f"voice model failed on {midi_to_note(event.midi)}: {exc}")
                note = None

        if note is None:
            note = pluck(
                event.midi, length_s, sample_rate=sample_rate,
                velocity=event.velocity, pickup=pickup,
                seed=int(rng.integers(0, 2**31)),
            )
            peak = float(np.max(np.abs(note))) or 1.0
            note = note * (10 ** (target_db / 20) / peak)

        cut = int(length_s * sample_rate)
        note = np.array(note[:cut], dtype=np.float64)
        damp = min(len(note), int(DAMP_S * sample_rate))
        if damp > 1:
            note[-damp:] *= np.linspace(1.0, 0.0, damp)

        first = int(round(event.start * sample_rate))
        if first < len(out):
            span = min(len(note), len(out) - first)
            out[first : first + span] += note[:span]
        rendered.voices[source] += 1

    rendered.notes = len(events)
    if noise_floor_db is not None and noise_floor_db > -110:
        out += rng.normal(0.0, 10 ** (noise_floor_db / 20), len(out))
        hum = getattr(getattr(profile, "fingerprint", None), "hum_hz", None)
        hum_db = getattr(getattr(profile, "fingerprint", None), "hum_db", None)
        if hum and hum_db:
            t = np.arange(len(out)) / sample_rate
            out += 10 ** (hum_db / 20) * np.sin(2 * np.pi * hum * t)

    peak = float(np.max(np.abs(out))) if len(out) else 0.0
    if peak > 0.98:
        out *= 0.98 / peak
        rendered.warnings.append(
            f"overlapping notes peaked at {20 * np.log10(peak):+.1f} dBFS and were "
            "scaled down - the part drives an amp a little softer than you would"
        )
    if profile is None:
        rendered.warnings.append(
            "no profile - every note was played on a synthetic string, not your guitar"
        )
    elif rendered.voices["pluck"]:
        rendered.warnings.append(
            f"{rendered.voices['pluck']} note(s) were outside what the profile has "
            "heard and were synthesised generically. Record a fuller protocol "
            "to cover them."
        )
    rendered.audio = out
    return rendered


def events_from_notes(notes, *, velocity_from_peak: bool = True) -> list[NoteEvent]:
    """Turn segmented :class:`~rigsmith.learn.segment.Note` objects into events.

    Used to re-render a performance on a different guitar: segment the
    original, then play the same notes, at the same times, through another
    profile.
    """
    pitched = [n for n in notes if n.pitched]
    if not pitched:
        return []
    peaks = np.array([n.peak_db for n in pitched])
    median = float(np.median(peaks))
    events = []
    for note in pitched:
        velocity = 0.75
        if velocity_from_peak:
            velocity = float(np.clip(0.75 + (note.peak_db - median) / 24.0, 0.1, 1.0))
        events.append(
            NoteEvent(
                start=note.start,
                duration=max(0.05, note.end - note.start),
                midi=note.midi,
                velocity=velocity,
                string=note.string,
            )
        )
    return events


#: Note lengths for the sequence form, in beats.
LENGTHS = {"1": 4.0, "2": 2.0, "4": 1.0, "8": 0.5, "16": 0.25, "32": 0.125, "w": 4.0, "h": 2.0, "q": 1.0, "e": 0.5, "s": 0.25}


def _strip_comments(text: str) -> list[str]:
    import re

    return [re.sub(r"(^|\s)#.*$", "", line).strip() for line in text.splitlines()]


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _suffixed(item, suffix: str):
    if isinstance(item, list):
        return [_suffixed(inner, suffix) for inner in item]
    if item.startswith("@"):
        return item
    length = suffix if suffix.startswith(":") and ":" not in item else ""
    accent = suffix.lstrip(":0123456789whqes.") if not item.endswith(("!", "'")) else ""
    head = item.rstrip("!'")
    tail = item[len(head):]
    if length:
        length = length.rstrip("!'")
    return f"{head}{length}{tail or accent}"


def parse_sequence(text: str, *, bpm: float = 120.0) -> list[NoteEvent]:
    """The quick form: notes in order, the way a riff is hummed.

    ``@140  E2 E2 G2 E2 A2:4 G2 (E2+B2+E3):2 -`` - ``@140`` sets the tempo,
    each note is an eighth unless ``:4`` (quarter), ``:16``, ``:2`` or a
    dotted ``:8.`` says otherwise, ``+`` stacks a chord, ``-`` rests, a
    trailing ``!`` accents and ``'`` ghosts a note, and ``x3`` repeats the
    note or the bracketed group before it.
    """
    import re

    from ..core.errors import Invalid
    from .instrument import note_to_midi

    body = " ".join(_strip_comments(text))
    tokens = re.findall(r"\(|\)[^\s()]*|[^\s()]+", body)
    beat = 60.0 / bpm

    def expand(items: list[str], position: int, depth: int) -> tuple[list[str], int]:
        out: list[str] = []
        while position < len(items):
            token = items[position]
            position += 1
            if token == "(":
                group, position = expand(items, position, depth + 1)
                out.append(group)  # type: ignore[arg-type]
                continue
            if token.startswith(")"):
                if depth == 0:
                    raise Invalid("a ')' with no '(' before it")
                # ")x2" repeats the group; ")" followed by a length or an
                # accent gives it to every note inside that has none.
                suffix = token[1:]
                repeat = re.search(r"x(\d+)$", suffix)
                times = int(repeat.group(1)) if repeat else 1
                suffix = suffix[: repeat.start()] if repeat else suffix
                if suffix:
                    out = [_suffixed(item, suffix) for item in out]
                return out * times, position
            match = re.fullmatch(r"x(\d+)", token)
            if match and out:
                last = out.pop()
                out.extend([last] * int(match.group(1)))
                continue
            out.append(token)
        if depth:
            raise Invalid("a '(' that is never closed")
        return out, position

    def flatten(items) -> list[str]:
        flat: list[str] = []
        for item in items:
            flat.extend(flatten(item) if isinstance(item, list) else [item])
        return flat

    raw, _ = expand(tokens, 0, 0)
    events: list[NoteEvent] = []
    clock = 0.0
    for token in flatten(raw):
        if token.startswith("@"):
            try:
                beat = 60.0 / float(token[1:])
            except (ValueError, ZeroDivisionError):
                raise Invalid(f"'{token}' is not a tempo", hint="For example:  @140") from None
            continue
        velocity = 0.75
        if token.endswith("!"):
            velocity, token = 0.95, token[:-1]
        elif token.endswith("'"):
            velocity, token = 0.4, token[:-1]
        length = 0.5
        if ":" in token:
            token, _, spec = token.partition(":")
            dotted = spec.endswith(".")
            spec = spec.rstrip(".")
            if spec not in LENGTHS:
                raise Invalid(f"'{spec}' is not a note length", hint="Use 1, 2, 4, 8, 16 or 32 (w h q e s), with a '.' for dotted")
            length = LENGTHS[spec] * (1.5 if dotted else 1.0)
        seconds = length * beat
        if token not in ("-", "r", "_"):
            for name in token.split("+"):
                try:
                    midi = float(name) if _is_number(name) else note_to_midi(name)
                except Exception:
                    raise Invalid(f"'{name}' is not a note", hint="Note names like E2, A#2, Bb3, or MIDI numbers") from None
                events.append(NoteEvent(clock, seconds * 0.92, midi, velocity))
        clock += seconds
    return events


def parse_events(text: str) -> list[NoteEvent]:
    """Read a tiny note language: ``start dur note [velocity]`` per line.

    ``0.0 0.5 E2 0.8`` - typed by hand for a riff, or pasted from somewhere.
    A ``#`` that starts a word begins a comment; one inside a word is a sharp,
    so ``A#2`` is a note and ``# the riff`` is not. Notes may be names or MIDI
    numbers.

    Text whose first word is not a time is the quick form instead - see
    :func:`parse_sequence`.
    """
    from ..core.errors import Invalid
    from .instrument import note_to_midi

    lines = _strip_comments(text)
    first = next((line.split()[0] for line in lines if line), "")
    if first and not _is_number(first.replace(",", "")):
        return parse_sequence(text)

    events = []
    for number, line in enumerate(lines, start=1):
        if not line:
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 3:
            raise Invalid(
                f"line {number}: '{line}' needs start, duration and note",
                hint="For example:  0.0 0.5 E2 0.8",
            )
        try:
            start, duration = float(parts[0]), float(parts[1])
        except ValueError:
            raise Invalid(f"line {number}: start and duration must be seconds") from None
        token = parts[2]
        midi = float(token) if token.replace(".", "", 1).isdigit() else note_to_midi(token)
        velocity = float(parts[3]) if len(parts) > 3 else 0.75
        events.append(NoteEvent(start, duration, midi, velocity))
    return events

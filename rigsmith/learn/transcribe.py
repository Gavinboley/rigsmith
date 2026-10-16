"""From a guitar stem to the notes it plays.

Not a tab transcriber, and nothing here pretends it is. What it has to get
right is narrower than that: *which pitches sound when*, closely enough that
playing them back on your guitar gives a DI whose notes, register and rhythm
match the record. The record's tone is then the only thing left differing -
which is the whole point.

Two estimators, chosen per passage:

* **Monophonic** - leads, single-note riffs, arpeggios. The onset segmenter
  from :mod:`.segment` with its autocorrelation pitch, which is accurate to a
  few cents on one note at a time.
* **Polyphonic** - chords and power-chord rhythm. Iterative harmonic-sum
  salience after Klapuri (2006): score every candidate fundamental by the
  weighted energy at its harmonics, take the best, remove its harmonics from
  the spectrum, and look again. Chords of up to six notes, with the octave
  errors that method is known for kept in check by refusing anything below
  the tuning's lowest string.

Distortion is the hard case and is said so in the result: a high-gain stem
has intermodulation products that look like extra notes, and a power chord's
fifth sits on its root's third harmonic. The report's ``confidence`` is lower
for both, and the rendering downstream says which notes it was unsure of.

When Spotify's basic-pitch is importable (or ``RIGSMITH_BASIC_PITCH`` names a
command that runs it) that is used instead, since it was trained for exactly
this. On Python 3.14 it currently is not, so the built-in path is the one
that usually runs.
"""

from __future__ import annotations

import math
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .instrument import midi_to_hz, midi_to_note
from .synth import NoteEvent

#: Frame size for the polyphonic estimator. Long, because a semitone at the
#: low E is under 5 Hz wide and resolution is what separates it.
N_FFT = 8192
HOP = 1024

#: Highest frequency used by the salience. Above this a distorted stem is fizz.
MAX_HZ = 6000.0

MAX_POLYPHONY = 6

#: A pitch needs this many consecutive frames (~85 ms) to be a note. Three
#: let the broadband smear of an attack through as a phantom bass note.
MIN_FRAMES = 4

#: How far a partial may sit from where the candidate predicts it, as a
#: fraction of its frequency. 1.5% is a quarter-tone: wide enough for a
#: string's inharmonicity, narrow enough that an E2's seventh harmonic cannot
#: vouch for a D3.
PARTIAL_TOLERANCE = 0.015

#: Klapuri's harmonic weighting constants.
ALPHA, BETA = 27.0, 320.0


@dataclass
class Transcription:
    """Notes, how they were found, and how far to trust them."""

    events: list[NoteEvent] = field(default_factory=list)
    method: str = "builtin"
    mode: str = "polyphonic"
    polyphony: float = 0.0
    """Average simultaneous pitches across the frames that held any."""
    confidence: float = 0.0
    """0-1: how much of the stem's pitched energy the notes explain."""
    tuning_cents: float = 0.0
    notes_text: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"{len(self.events)} note(s) by {self.method} ({self.mode}), "
            f"avg polyphony {self.polyphony:.1f}, confidence {self.confidence:.0%}"
        ]
        if abs(self.tuning_cents) > 5:
            lines.append(f"  the record sits {self.tuning_cents:+.0f} cents from concert pitch")
        lines.extend(f"  note: {w}" for w in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "events": [e.to_dict() for e in self.events],
            "method": self.method,
            "mode": self.mode,
            "polyphony": round(self.polyphony, 2),
            "confidence": round(self.confidence, 3),
            "tuning_cents": round(self.tuning_cents, 1),
            "warnings": self.warnings,
            "describe": self.describe(),
        }


# --------------------------------------------------------------------------
# salience
# --------------------------------------------------------------------------


def _spectrogram(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    from .segment import _frames

    return _frames(audio, N_FFT, HOP)


def _peaks(frame: np.ndarray, floor: float) -> np.ndarray:
    """Keep only local spectral maxima above ``floor``; everything else is 0.

    Salience summed over raw bins rewards smeared energy - a decaying note's
    skirts, a distorted stem's noise - and that is what made a single-note
    riff read as five simultaneous pitches. Peaks are where partials are.
    """
    peaks = np.zeros_like(frame)
    if len(frame) < 3:
        return peaks
    middle = frame[1:-1]
    is_peak = (middle > frame[:-2]) & (middle >= frame[2:]) & (middle > floor)
    peaks[1:-1][is_peak] = middle[is_peak]
    return peaks


def _true_fundamental(best: int, f0s: np.ndarray, spectrum: np.ndarray, bin_hz: float, limit: int) -> int:
    """Move a choice down to its fundamental when the evidence says so.

    Harmonic-sum salience likes an upper partial: the fifth harmonic of an A2
    is a C#5 with a strong "fundamental" and plenty of its own harmonics, all
    of which are really the A2's. The tell is partials the higher candidate
    cannot explain - the A2's 2nd, 3rd, 4th, 6th... sit between the C#5's.
    When at least three of those are present, the lower note is the answer.
    """
    chosen = f0s[best]
    for divisor in range(8, 1, -1):
        candidate = chosen / divisor
        if candidate < f0s[0] * 0.97:
            continue
        unexplained = 0
        for h in range(1, 13):
            if h % divisor == 0:
                continue
            centre = h * candidate
            idx = int(round(centre / bin_hz))
            half = max(1, int(round(PARTIAL_TOLERANCE * centre / bin_hz)))
            if idx + half >= limit:
                break
            if spectrum[max(1, idx - half) : idx + half + 1].max() > 0:
                unexplained += 1
        if unexplained >= 3:
            nearest = int(np.argmin(np.abs(f0s - candidate)))
            return nearest
    return best


def salience_frames(
    magnitude: np.ndarray,
    sample_rate: int,
    *,
    min_midi: int = 28,
    max_midi: int = 88,
    tuning_cents: float = 0.0,
    max_polyphony: int = MAX_POLYPHONY,
) -> list[list[tuple[int, float]]]:
    """Per frame, the pitches found by iterative estimation and cancellation.

    Each round scores every candidate by the weighted, compressed peak energy
    at its harmonics, takes the best, then removes what that note explains -
    but only down to a *smooth* harmonic envelope (Klapuri's spectral
    smoothness), so a partial two notes share is left partly standing for the
    second one. A candidate also has to show its own evidence: at least three
    of its first six harmonics present as peaks, and not be a plain octave or
    twelfth of a note already found unless it is substantially louder than
    that note's partials would explain.
    """
    freqs = np.fft.rfftfreq(N_FFT, 1 / sample_rate)
    bin_hz = freqs[1]
    limit = int(MAX_HZ / bin_hz)
    candidates = np.arange(min_midi, max_midi + 1)
    f0s = np.array([midi_to_hz(m + tuning_cents / 100.0) for m in candidates])

    plan = []
    for f0 in f0s:
        entries = []
        h = 1
        while h * f0 < MAX_HZ and h <= 20:
            centre = h * f0
            half = max(1, int(round(PARTIAL_TOLERANCE * centre / bin_hz)))
            idx = int(round(centre / bin_hz))
            entries.append((max(1, idx - half), min(limit, idx + half + 1), (f0 + ALPHA) / (h * f0 + BETA)))
            h += 1
        plan.append(entries)

    # Every candidate scored at once, exactly. All the (candidate, harmonic)
    # windows are laid end to end as one index array; one maximum.reduceat
    # gives every window's largest peak, one add.reduceat sums them into each
    # candidate's score. The Python loop this replaces - candidate by
    # harmonic by frame - took a minute on a four-minute stem. (Treating a
    # window's sum as its maximum was faster still and cost a third of the
    # chord accuracy: another note's partial in the window is double counted.)
    bins, starts, window_weight, window_low, cand_starts = [], [], [], [], []
    for entries in plan:
        cand_starts.append(len(starts))
        for h, (a, b, weight) in enumerate(entries):
            if b <= a:
                b = a + 1
            starts.append(len(bins))
            bins.extend(range(a, b))
            window_weight.append(weight)
            window_low.append(h < 6)
    bins = np.minimum(np.array(bins, dtype=np.int64), limit - 1)
    starts = np.array(starts, dtype=np.int64)
    window_weight = np.array(window_weight)
    window_low = np.array(window_low, dtype=float)
    cand_starts = np.array(cand_starts, dtype=np.int64)

    def score_all(spectrum: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        window_max = np.maximum.reduceat(spectrum[bins], starts)
        scores = np.add.reduceat(window_max * window_weight, cand_starts)
        hits = np.add.reduceat((window_max > 0) * window_low, cand_starts)
        return scores, hits

    compressed_all = np.sqrt(magnitude[:, :limit]) if magnitude.size else magnitude
    loudest = float(compressed_all.max()) if compressed_all.size else 0.0
    results: list[list[tuple[int, float]]] = []
    for frame in compressed_all:
        top = float(frame.max()) if len(frame) else 0.0
        if loudest <= 0 or top < loudest * 10 ** (-40 / 40):
            results.append([])
            continue
        spectrum = _peaks(frame, top * 10 ** (-50 / 40))
        found: list[tuple[int, float]] = []
        taken: set[int] = set()
        first = None
        for _ in range(max_polyphony):
            scores, hits = score_all(spectrum)
            scores[hits < 3] = 0.0
            for index in taken:
                scores[index] = 0.0
            best = int(np.argmax(scores))
            lower = _true_fundamental(best, f0s, spectrum, bin_hz, limit)
            if lower not in taken:
                best = lower
            score = float(scores[best]) if scores[best] > 0 else float(scores.max())
            if first is None:
                first = score
            if score <= 0 or score < 0.45 * first:
                break
            midi = int(candidates[best])
            related = any((midi - m) in (12, 19, 24, -12, -19, -24) for m, _ in found)
            if related and score < 0.7 * first:
                break
            found.append((midi, score))
            taken.add(best)
            # Smooth cancellation: each harmonic is reduced by the smaller of
            # itself and the mean of its neighbours, not wiped.
            amplitudes = [float(spectrum[a:b].max()) if b > a else 0.0 for a, b, _ in plan[best]]
            for h, (a, b, _) in enumerate(plan[best]):
                neighbours = [amplitudes[k] for k in (h - 1, h + 1) if 0 <= k < len(amplitudes)]
                smooth = min(amplitudes[h], (sum(neighbours) / len(neighbours)) if neighbours else amplitudes[h])
                if h == 0:
                    smooth = amplitudes[h]
                if b > a:
                    region = spectrum[a:b]
                    original = region.copy()
                    region[:] = np.maximum(region - smooth, 0.0)
                    # A partial left at under half its height is this note's
                    # skirt, not somebody else's evidence.
                    region[region < 0.5 * original] = 0.0
        results.append(found)
    return results


#: The bottom strings of the tunings people play, highest first. The lowest
#: string a stem implies is rounded down to one of these.
TUNING_BOTTOMS = (40, 39, 38, 37, 36, 35, 33)


def infer_lowest(frames) -> int:
    """The lowest string a stem implies, from where its lowest notes sit.

    The salience estimator's one systematic error is reaching an octave below
    a note - a B1 under a B2 - and the only thing that stops it is a floor.
    Too low a floor lets the error through; too high cuts off a real drop
    tuning. So the floor is read from the music: the 10th percentile of each
    frame's lowest pitch, rounded down to a tuning's bottom string. Sub-octave
    errors are occasional, so they do not move a percentile; a band in drop C
    plays its low C constantly, so it does.
    """
    lows = [min(p for p, _ in found) for found in frames if found]
    if len(lows) < 10:
        return TUNING_BOTTOMS[-1]
    typical = float(np.percentile(lows, 10))
    for bottom in TUNING_BOTTOMS:
        if bottom <= typical + 0.5:
            return bottom
    return TUNING_BOTTOMS[-1]


def _notes_from_frames(frames, onsets_s, sample_rate, energies) -> list[NoteEvent]:
    """Turn per-frame pitch sets into note events, split at onsets."""
    frame_s = HOP / sample_rate
    onset_frames = sorted({int(round(t / frame_s)) for t in onsets_s})
    onset_set = set(onset_frames)
    by_pitch: dict[int, list[int]] = {}
    for index, found in enumerate(frames):
        for pitch, _ in found:
            by_pitch.setdefault(pitch, []).append(index)

    events = []
    peak_energy = max(energies) if len(energies) else 1.0
    for pitch, indices in by_pitch.items():
        run = [indices[0]]
        runs = []
        for index in indices[1:]:
            gap = index - run[-1]
            # A re-pick of the same pitch is a new note even with no gap -
            # palm-muted chugs on one string are exactly that.
            repicked = any(f in onset_set for f in range(run[-1] + 1, index + 1)) and index - run[0] >= MIN_FRAMES
            if gap > 2 or repicked:
                runs.append(run)
                run = [index]
            else:
                run.append(index)
        runs.append(run)
        for run in runs:
            if len(run) < MIN_FRAMES:
                continue
            start = run[0] * frame_s
            # Snap to a nearby onset - frames are 21 ms; onsets are 5.
            near = [t for t in onsets_s if abs(t - start) <= 2.5 * frame_s]
            if near:
                start = min(near, key=lambda t: abs(t - start))
            end = (run[-1] + 1) * frame_s
            # A short note with no attack anywhere near it is a chord's
            # partials beating as they decay, not something anyone played.
            if not near and end - start < 0.3:
                continue
            level = energies[run[0]] / peak_energy if peak_energy else 0.5
            velocity = float(np.clip(0.45 + 0.55 * math.sqrt(max(level, 0.0)), 0.1, 1.0))
            events.append(NoteEvent(start=round(start, 4), duration=round(max(0.05, end - start), 4), midi=float(pitch), velocity=velocity))
    return sorted(events, key=lambda e: (e.start, e.midi))


#: Against the lowest strong pitch, how loud a fifth's own fundamental at
#: 1.5x must be for an attack to be a chord - and how far above the
#: spectrum either side of it it must stand.
FIFTH_FLOOR = 0.05
FIFTH_CONTRAST = 3.0


def _collapse_single_notes(audio, sample_rate, events: list[NoteEvent]) -> tuple[list[NoteEvent], int]:
    """Attacks that are one note with harmonics on it, as that one note.

    Chroma cannot tell a palm-muted low E from an E5: a driven note's third
    harmonic is the fifth's pitch class. The spectrum can - a power chord's
    fifth sounds its own fundamental at 1.5 times the root's frequency,
    which no harmonic of the root ever does - so an attack whose lowest
    pitch has nothing at 1.5x above it, nor at 2/3 below it (a quiet root
    under its fifth), is one note, and the chord's other notes were that
    note's harmonics.
    """
    starts = sorted({round(e.start, 3) for e in events})
    if not starts:
        return events, 0
    ends = starts[1:] + [len(audio) / sample_rate]
    kept: list[NoteEvent] = []
    collapsed = 0
    for start, end in zip(starts, ends):
        group = [e for e in events if round(e.start, 3) == start]
        if len(group) < 2:
            kept.extend(group)
            continue
        # From the bass that actually sounds, not the lowest note reported:
        # the salience also credits a pitch 2/3 below a single note, whose
        # harmonics fit it too, though nothing plays there.
        basses = _bass_notes(audio, sample_rate, start, end)
        if len(basses) != 1:
            kept.extend(group)  # no bass to go on, or a quiet root under a fifth
            continue
        settle = 0.02 if end - start >= 0.2 else 0.008
        a, b = int((start + settle) * sample_rate), int(min(end, start + 0.3) * sample_rate)
        piece = audio[a:b]
        size = 1 << int(np.ceil(np.log2(max(len(piece), 16384))))
        spectrum = np.abs(np.fft.rfft(piece * np.hanning(len(piece)), size))
        freqs = np.fft.rfftfreq(size, 1 / sample_rate)
        # The pitch to test from: the transcription's own estimate nearest
        # that bass - a short note is all attack, and a plucked string starts
        # sharp, so the bass alone reads it up to a semitone high, and 1.5x
        # of that lands among the note's own smeared harmonics.
        near = min(group, key=lambda e: abs(e.midi - basses[0]))
        pitch = near.midi if abs(near.midi - basses[0]) <= 1.5 else float(round(basses[0]))
        f0 = 440.0 * 2 ** ((pitch - 69) / 12)

        def peak(hz: float) -> float:
            near = (freqs >= hz * 0.975) & (freqs <= hz * 1.025)
            return float(spectrum[near].max()) if near.any() else 0.0

        root = peak(f0)

        def stands_out(ratio: float, left: float, right: float) -> bool:
            # A chord tone that is there stands well above the spectrum beside
            # it; a short, driven note smears energy between its harmonics,
            # level across the gap. (Measured at the fifth: single notes
            # 0.3-1.0, power chords 10-300.)
            tone = peak(f0 * ratio)
            return tone >= FIFTH_FLOOR * root and tone >= FIFTH_CONTRAST * 0.5 * (peak(f0 * left) + peak(f0 * right))

        # The fifth (3:2) of a power chord, a triad's third (5:4, 6:5) - and
        # that third an octave up (5:2, 12:5), for a driven triad whose
        # difference tones put a bass an octave under its root. None of
        # those is a harmonic of one note.
        if root <= 0 or any(stands_out(*tone) for tone in (
            (1.5, 1.36, 1.64), (1.26, 1.12, 1.40), (1.19, 1.12, 1.33), (2.52, 2.24, 2.80), (2.38, 2.24, 2.67),
        )):
            kept.extend(group)
            continue
        kept.append(NoteEvent(
            start=float(start), duration=max(e.duration for e in group),
            midi=float(pitch), velocity=max(e.velocity for e in group),
        ))
        collapsed += 1
    return kept, collapsed


def _single_notes(notes, min_midi: int) -> list[NoteEvent]:
    """Measured notes as events, one at a time - the pitched ones."""
    median_peak = float(np.median([n.peak_db for n in notes])) if notes else -12.0
    events = []
    for n in notes:
        if not n.pitched:
            continue
        pitch = n.nearest
        # A power chord's root and fifth share a period an octave below
        # the root; an autocorrelator reports that. Below the lowest
        # string it cannot be the note, so it is taken up an octave.
        while pitch < min_midi and pitch + 12 <= 96:
            pitch += 12
        events.append(
            NoteEvent(
                start=n.start,
                duration=max(0.05, n.end - n.start),
                midi=float(pitch),
                velocity=float(np.clip(0.75 + (n.peak_db - median_peak) / 24, 0.1, 1.0)),
            )
        )
    return events


def _chroma_similarity(audio, sample_rate, events) -> float:
    """How well the notes explain the stem's pitched energy, 0-1.

    Average per-frame cosine between the stem's chroma and the notes' own
    chroma (from the event list, no rendering needed), weighted by loudness.
    """
    from .align import audio_chroma, events_chroma

    hop_s = 0.05
    a = audio_chroma(audio, sample_rate, hop_s=hop_s)
    b = events_chroma(events, len(a), hop_s=hop_s)
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a, b = a[:n], b[:n]
    weights = a.sum(axis=1)
    norms = np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1)
    valid = (norms > 0) & (weights > 0)
    if not valid.any():
        return 0.0
    cosine = (a[valid] * b[valid]).sum(axis=1) / norms[valid]
    return float(np.average(cosine, weights=weights[valid]))


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------


def basic_pitch_command() -> list[str] | None:
    """How to run basic-pitch here, if it can be run at all."""
    configured = os.environ.get("RIGSMITH_BASIC_PITCH")
    if configured:
        return configured.split()
    try:
        import basic_pitch  # noqa: F401

        return None if not shutil.which("basic-pitch") else ["basic-pitch"]
    except ImportError:
        return None


def _basic_pitch(path: Path) -> list[NoteEvent] | None:
    command = basic_pitch_command()
    if not command:
        return None
    from .midifile import read, to_events

    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                [*command, tmp, str(path)], check=True, capture_output=True, timeout=900
            )
        except (OSError, subprocess.SubprocessError):
            return None
        produced = sorted(Path(tmp).glob("*.mid"))
        if not produced:
            return None
        return to_events(read(produced[0]))


def transcribe(
    audio,
    sample_rate: int,
    *,
    path: Path | None = None,
    mode: str = "auto",
    lowest: str | int = "auto",
    method: str = "auto",
    match_record_pitch: bool = True,
) -> Transcription:
    """Notes from a guitar stem.

    Args:
        mode: ``mono``, ``poly`` or ``auto`` (decide from how many pitches
            sound at once).
        lowest: Nothing below this is a note: the tuning's lowest string, or
            ``auto`` to infer it from the stem (see :func:`infer_lowest`).
        method: ``builtin``, ``basic-pitch``, or ``auto`` (basic-pitch when
            it can be run, else built in).
        match_record_pitch: Offset every note by the record's own deviation
            from concert pitch, so the rendered part sits where it does.
    """
    from ..match.pitch import analyse as pitch_analyse
    from .instrument import note_to_midi
    from .segment import detect_onsets, segment

    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    result = Transcription()
    auto_lowest = isinstance(lowest, str) and lowest.strip().lower() == "auto"
    min_midi = 33 if auto_lowest else (note_to_midi(lowest) if isinstance(lowest, str) else int(lowest))

    report = pitch_analyse(audio, sample_rate)
    if report.measured:
        result.tuning_cents = float(report.cents_offset)

    if method in ("auto", "basic-pitch") and path is not None:
        events = _basic_pitch(Path(path))
        if events is not None:
            result.events = [e for e in events if e.midi >= min_midi]
            result.method = "basic-pitch"
            result.mode = "polyphonic"
            result.confidence = _chroma_similarity(audio, sample_rate, result.events)
            return result
        if method == "basic-pitch":
            result.warnings.append("basic-pitch could not be run here; used the built-in estimator")

    magnitude = _spectrogram(audio, sample_rate)
    frames = salience_frames(magnitude, sample_rate, min_midi=min_midi, tuning_cents=result.tuning_cents)
    if auto_lowest:
        bottom = infer_lowest(frames)
        if bottom > min_midi:
            min_midi = bottom
            frames = salience_frames(magnitude, sample_rate, min_midi=min_midi, tuning_cents=result.tuning_cents)
        result.notes_text.append(f"lowest string taken as {midi_to_note(min_midi)}")
    onsets = detect_onsets(audio, sample_rate)
    # Polyphony from the settled frames only: the two frames after an attack
    # hold the old note's tail and the new note's smear at once, and counting
    # them made a single-note riff look like a stream of dyads.
    frame_s = HOP / sample_rate
    unsettled = {int(t / frame_s) + k for t in onsets for k in range(-1, 4)}
    active = [len(f) for index, f in enumerate(frames) if f and index not in unsettled]
    result.polyphony = float(np.median(active)) if active else 0.0
    energies = np.sqrt((magnitude ** 2).sum(axis=1)) if len(magnitude) else np.zeros(0)

    chosen = mode
    notes = None
    if mode == "auto":
        chosen = "mono" if result.polyphony <= 1.35 else "poly"
        if chosen == "poly":
            # Distortion adds intermodulation the salience counts as extra
            # pitches, but it does not make a single note less periodic. When
            # nearly every note the autocorrelator hears is cleanly periodic,
            # it is one note at a time with distortion on it - however many
            # pitches the salience counts: a high-gain eighth-note riff read
            # as polyphony 4 and was transcribed as chords (F1 0.39, against
            # 0.99 as single notes).
            notes = segment(audio, sample_rate)
            if notes:
                # Measured: distorted single notes sit at 0.73-0.87 clarity,
                # chords - clean or distorted - at 0.61 and below.
                periodic = sum(1 for n in notes if n.clarity >= 0.7) / len(notes)
                if periodic >= 0.8:
                    chosen = "mono"

    if chosen == "mono":
        notes = notes if notes is not None else segment(audio, sample_rate)
        result.events = _single_notes(notes, min_midi)
        result.mode = "monophonic"
    else:
        result.events = _notes_from_frames(frames, onsets, sample_rate, energies)
        result.mode = "polyphonic"
        # The note-by-note estimate hears a chord's common fundamental, an
        # octave or two below the chord, more often than the chord. Read the
        # chords from their pitch classes as well, and keep whichever set of
        # notes follows the stem better.
        chords = _chords_from_chroma(audio, sample_rate, onsets, min_midi)
        if chords and _chroma_similarity(audio, sample_rate, chords) > _chroma_similarity(audio, sample_rate, result.events):
            result.events = chords
            result.notes_text.append("chords read from their pitch classes and voiced as a guitarist would")
        result.events, collapsed = _collapse_single_notes(audio, sample_rate, result.events)
        if collapsed:
            result.notes_text.append(f"{collapsed} attack(s) were one note with distortion's harmonics on it, not a chord")

    result.method = "builtin"
    result.confidence = _chroma_similarity(audio, sample_rate, result.events)

    # Play it back at the record's pitch, not at concert pitch. A record that
    # sits twenty cents flat - tape speed, a detuned band - puts every
    # harmonic twenty cents low, and a DI rendered in tune would be measured
    # as a tone difference that no rig can fix.
    if match_record_pitch and abs(result.tuning_cents) >= 3:
        for event in result.events:
            event.midi += result.tuning_cents / 100.0

    # Say what makes this one hard, rather than leaving a low number to speak
    # for itself.
    flatness = _spectral_flatness(magnitude)
    if flatness > 0.2:
        result.warnings.append(
            "the stem is heavily distorted - intermodulation can read as extra "
            "notes, and a power chord's fifth hides under its root's harmonics"
        )
    if result.confidence < 0.6:
        result.warnings.append(
            "the notes explain under 60% of the stem's pitched energy. Check "
            "them, or supply a tab's MIDI export - the right notes from a tab "
            "beat a guess at a distorted stem"
        )
    if not result.events:
        result.warnings.append("no notes found - is this a guitar stem?")
    return result


#: Chord shapes by name, as the pitch classes they hold above the root.
CHORD_SHAPES = {
    "5": (0, 7), "": (0, 4, 7), "m": (0, 3, 7), "sus4": (0, 5, 7), "sus2": (0, 2, 7),
    "7": (0, 4, 7, 10), "m7": (0, 3, 7, 10), "maj7": (0, 4, 7, 11),
}
#: How each is voiced to play back: a barre shape from the root up.
CHORD_VOICINGS = {
    "5": (0, 7, 12), "": (0, 7, 12, 16, 19), "m": (0, 7, 12, 15, 19), "sus4": (0, 7, 12, 17, 19),
    "sus2": (0, 7, 12, 14, 19), "7": (0, 7, 10, 16, 19), "m7": (0, 7, 10, 15, 19), "maj7": (0, 7, 11, 16, 19),
}
#: What each tone beyond root and fifth costs: a distorted power chord's
#: harmonics light up the third and seventh too, and a plain chord should
#: win unless the extra tones are really there.
CHORD_TONE_COST = 0.08


def _chord_template(voicing, *, partials: int = 8, decay: float = 0.75) -> np.ndarray:
    """The pitch classes a voicing sounds, harmonics included, as a unit vector."""
    template = np.zeros(12)
    for tone in voicing:
        for k in range(1, partials + 1):
            template[(tone + int(round(12 * np.log2(k)))) % 12] += decay ** (k - 1)
    return template / np.linalg.norm(template)


#: How strong, against the loudest bass peak, a pitch a fifth below the
#: lowest strong one must be to be offered as a power chord's root.
POWER_ROOT_FLOOR = 0.08

#: How much better the higher candidate root must fit the notes to beat the
#: lower one. Tuned on synthetic parts (two render seeds): 0.02 gives back
#: most of the power chords' gain, 0.05 starts costing sevenths, 0.1 triads.
LOWER_ROOT_MARGIN = 0.04


def _bass_notes(audio: np.ndarray, sample_rate: int, start: float, end: float) -> list[float]:
    """Candidates for a chord's root as played: the lowest strong pitch in
    70-350 Hz just after an attack, and the pitch a fifth below it when one
    rings there.

    Driven, a power chord's root can ring quieter than its fifth - the amp's
    low end rolls off - and the fifth is then the lowest strong pitch. A
    distorted triad can show a peak a fifth below its root too, from the
    amp's intermodulation, so this only offers it: the chord shapes decide
    which root explains the notes (:func:`_chords_from_chroma`).
    """
    # Past the pick's click - but a palm-muted note is over in 70 ms, and
    # waiting 40 ms for it to settle measured mostly its tail.
    settle = 0.04 if end - start >= 0.2 else 0.008
    a, b = int((start + settle) * sample_rate), int(min(end, start + 0.5) * sample_rate)
    piece = audio[a:b]
    if len(piece) < 2048:
        return []
    size = 1 << int(np.ceil(np.log2(max(len(piece), 8192))))
    spectrum = np.abs(np.fft.rfft(piece * np.hanning(len(piece)), size))
    freqs = np.fft.rfftfreq(size, 1 / sample_rate)
    band = (freqs >= 70.0) & (freqs <= 350.0)
    level, where = spectrum[band], freqs[band]
    if not len(level) or level.max() <= 0:
        return []
    midi = lambda hz: float(69 + 12 * np.log2(hz / 440.0))  # noqa: E731
    for i in range(1, len(level) - 1):
        if level[i] >= level[i - 1] and level[i] >= level[i + 1] and level[i] >= 0.3 * level.max():
            found = [midi(where[i])]
            below = (freqs >= where[i] * 2 / 3 * 0.975) & (freqs <= where[i] * 2 / 3 * 1.025)
            if below.any() and spectrum[below].max() >= POWER_ROOT_FLOOR * level.max():
                found.append(midi(freqs[below][int(np.argmax(spectrum[below]))]))
            return found
    return []


def _chords_from_chroma(audio: np.ndarray, sample_rate: int, onsets, min_midi: int, *, hop_s: float = 0.02) -> list[NoteEvent]:
    """A chord at each attack, from the pitch classes that ring after it.

    The root is the lowest strong pitch in the bass - what a guitarist's
    root is, or the pitch a fifth below it when a driven power chord's root
    rings quieter than its fifth (:func:`_bass_notes`) - and the shape the
    one whose pitch classes, harmonics included, best match the chroma once
    each tone beyond a power chord has paid :data:`CHORD_TONE_COST`. Voiced
    as a barre shape from that root.

    On synthetic parts this reads triads' pitch classes right at 94% of
    attacks clean, crunchy or driven (the note-by-note estimate: 19-56%),
    sevenths at 81-94%, and power chords' roots at 84-97% (59-94% before
    the fifth below was offered; driven, the root was the fifth half the
    time).
    """
    from .align import audio_chroma

    if not onsets:
        return []
    chroma = audio_chroma(audio, sample_rate, hop_s=hop_s)
    templates = {name: _chord_template(CHORD_VOICINGS[name]) for name in CHORD_SHAPES}
    events: list[NoteEvent] = []
    ends = list(onsets[1:]) + [len(audio) / sample_rate]
    for start, end in zip(onsets, ends):
        a, b = int((start + 0.06) / hop_s), int(min(end - 0.02, start + 1.2) / hop_s)
        if b - a < 2 or b > len(chroma):
            continue
        profile = chroma[a:b].mean(axis=0)
        if profile.sum() <= 0:
            continue
        profile = profile / np.linalg.norm(profile)
        basses = _bass_notes(audio, sample_rate, start, end)
        roots = sorted({int(round(bass)) % 12 for bass in basses}) or range(12)
        fits = [
            (float(np.dot(profile, np.roll(template, r))) - CHORD_TONE_COST * (len(CHORD_SHAPES[shape]) - 2), r, shape)
            for shape, template in templates.items()
            for r in roots
        ]
        score, root, name = max(fits)
        if len(basses) > 1:
            # The lower candidate is the root a guitarist plays unless the
            # other explains the notes clearly better: root and fifth alone
            # fit a sus4 on the fifth almost as well as a power chord.
            lower = int(round(basses[1])) % 12
            best_lower = max(f for f in fits if f[1] == lower)
            if best_lower[0] >= score - LOWER_ROOT_MARGIN:
                score, root, name = best_lower
        played = [bass for bass in basses if int(round(bass)) % 12 == root]
        base = int(round(played[0])) if played else min_midi + ((root - min_midi) % 12)
        while base < min_midi:
            base += 12
        for tone in CHORD_VOICINGS[name]:
            events.append(NoteEvent(start=float(start), duration=max(0.05, end - start - 0.02), midi=float(base + tone), velocity=0.75))
    return events


def _spectral_flatness(magnitude: np.ndarray) -> float:
    if not magnitude.size:
        return 0.0
    loud = magnitude[magnitude.sum(axis=1) > np.percentile(magnitude.sum(axis=1), 50)]
    if not len(loud):
        return 0.0
    loud = loud[:, 5 : N_FFT // 4] + 1e-12
    geometric = np.exp(np.mean(np.log(loud), axis=1))
    arithmetic = np.mean(loud, axis=1)
    return float(np.median(geometric / arithmetic))


def power_chords(events: list[NoteEvent], *, add_octave: bool = True) -> list[NoteEvent]:
    """Expand single low notes into root-fifth(-octave) power chords.

    For a high-gain rhythm part the estimator often finds only the root - the
    fifth's partials sit under the root's. When the record is known to be
    power chords, this puts the fifth back, so the amp sees the same
    intermodulation the record's amp did.
    """
    expanded = []
    for event in events:
        expanded.append(event)
        if event.midi > 57:  # above A3 it is not a power-chord root
            continue
        for interval in ((7, 12) if add_octave else (7,)):
            expanded.append(NoteEvent(event.start + 0.004 * (interval // 7), event.duration, event.midi + interval, event.velocity * 0.92, None, event.technique))
    return sorted(expanded, key=lambda e: (e.start, e.midi))


def describe_events(events: list[NoteEvent], *, limit: int = 40) -> str:
    lines = []
    for event in events[:limit]:
        lines.append(f"{event.start:7.2f}s  {midi_to_note(event.midi):<4} {event.duration:5.2f}s  v{event.velocity:.2f}")
    if len(events) > limit:
        lines.append(f"... and {len(events) - limit} more")
    return "\n".join(lines)

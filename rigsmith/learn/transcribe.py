"""From a guitar stem to the notes it plays.

Not a tab transcriber, and nothing here pretends it is. What it has to get
right is narrower than that: *which pitches sound when*, closely enough that
playing them back on your guitar gives a DI whose notes, register and rhythm
match the record. The record's tone is then the only thing left differing -
which is the whole point.

Three estimators, chosen per passage:

* **Monophonic** - leads and single-note riffs. The onset segmenter from
  :mod:`.segment` with its autocorrelation pitch, which is accurate to a few
  cents on one note at a time.
* **Arpeggio** - notes picked one at a time and left to ring over each
  other. At every attack, the spectrum after it less the one before: what
  is left is the note that just arrived, and the salience below reads it.
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
from collections import Counter
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


#: How loud, against the loudest pitch from E2 to E3, the stem must be at a
#: string below E2 for a lower tuning to be believed.
LOW_STRING_FLOOR = 0.2


def _sounds_below(magnitude: np.ndarray, sample_rate: int, bottom: int) -> bool:
    """Whether the stem really sounds between ``bottom`` and E2 - a drop
    tuning played - rather than only the salience's echo of the notes above.
    From the loud moments (90th percentile per bin), so a low riff that comes
    and goes still counts."""
    if not len(magnitude):
        return False
    spectrum = np.percentile(magnitude, 90, axis=0)
    freqs = np.fft.rfftfreq(N_FFT, 1 / sample_rate)
    hz = lambda m: 440.0 * 2 ** ((m - 69) / 12)  # noqa: E731
    ref = (freqs >= hz(40) * 0.97) & (freqs <= hz(52) * 1.03)
    if not ref.any():
        return False
    floor = LOW_STRING_FLOOR * spectrum[ref].max()
    # A peak of its own: E2's lobe reaches down past D#2, rising all the way,
    # so energy there is not enough - a played low note is a local maximum.
    for i in np.nonzero((freqs >= hz(bottom) * 0.97) & (freqs <= hz(39) * 1.03))[0]:
        if 0 < i < len(spectrum) - 1 and spectrum[i] >= floor and spectrum[i] > spectrum[i - 1] and spectrum[i] >= spectrum[i + 1]:
            return True
    return False


#: The 5th and 7th harmonics of the low pitch the salience hears below E2,
#: against its 4th, 6th and 8th, for that pitch to be a string. A B1 through
#: a cab keeps too little of its fundamental for :func:`_sounds_below` (B
#: standard read an octave up, F1 0.25-0.38), but sounds every harmonic -
#: 0.50-0.62 from B standard to drop A. The salience's echo an octave under a
#: note above has only that note's harmonics, the even ones: 0.36 at most on
#: every E-standard part.
LOW_STRING_ODD = 0.45


def _string_below(magnitude: np.ndarray, frames, sample_rate: int, bottom: int) -> bool:
    """Whether the pitch the salience hears most often below E2 is a string,
    by its odd harmonics, when its fundamental is too faint to show."""
    below = [(index, round(min(p for p, _ in found))) for index, found in enumerate(frames) if found]
    below = [(index, pitch) for index, pitch in below if bottom <= pitch < 40 and index < len(magnitude)]
    if len(below) < 10:
        return False
    pitch = Counter(p for _, p in below).most_common(1)[0][0]
    spectrum = np.median(magnitude[[index for index, p in below if p == pitch]], axis=0)
    freqs = np.fft.rfftfreq(N_FFT, 1 / sample_rate)
    f0 = 440.0 * 2 ** ((pitch - 69) / 12)

    def peak(hz: float) -> float:
        band = (freqs >= hz * 0.985) & (freqs <= hz * 1.015)
        return float(spectrum[band].max()) if band.any() else 0.0

    return (peak(5 * f0) + peak(7 * f0)) / max(peak(4 * f0) + peak(6 * f0) + peak(8 * f0), 1e-12) >= LOW_STRING_ODD


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
#: ...and how loud against the louder shoulder: a little under it still
#: counts, for a sus chord whose fourth and fifth crowd each other's
#: shoulders. 0.6 let the slopes of a chug's harmonics pass for chord tones.
PEAK_SHARE = 0.8

#: Chord tones above a bass that no harmonic of the bass note is near, as
#: (ratio, left shoulder, right shoulder): the fifth, the major and minor
#: third, the fourth, the major sixth, and the thirds an octave up.
CHORD_TONES = (
    (1.5, 1.36, 1.64), (1.26, 1.12, 1.40), (1.19, 1.12, 1.33), (1.335, 1.22, 1.45), (1.68, 1.56, 1.80),
    (2.52, 2.24, 2.80), (2.38, 2.24, 2.67),
)


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
            band = (freqs >= hz * 0.975) & (freqs <= hz * 1.025)
            return float(spectrum[band].max()) if band.any() else 0.0

        root = peak(f0)

        def stands_out(ratio: float, left: float, right: float) -> bool:
            # A chord tone that is there is a peak of its own: louder than the
            # spectrum a step and a half either side, and well above the
            # quieter of the two. A short, driven note smears energy level
            # across the gap between its harmonics (measured at the fifth:
            # 0.3-1.0x, against 10-300x for a power chord) and up the slope of
            # each harmonic's peak, where the side nearer the harmonic is
            # louder. Against the quieter side, so a chord tone next door - a
            # sus4's fourth beside its fifth - does not hide it.
            tone = peak(f0 * ratio)
            low, high = peak(f0 * left), peak(f0 * right)
            return tone >= FIFTH_FLOOR * root and tone >= PEAK_SHARE * max(low, high) and tone >= FIFTH_CONTRAST * min(low, high)

        # A chord tone above the bass where no harmonic of one note is: the
        # fifth, the thirds, a sus4's or an inversion's fourth and sixth - and
        # the thirds an octave up, for a driven triad whose difference tones
        # put a bass an octave under its root.
        if root <= 0 or any(stands_out(*tone) for tone in CHORD_TONES):
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


# --------------------------------------------------------------------------
# arpeggios
# --------------------------------------------------------------------------

#: What makes a part an arpeggio, each tested on its own share of the
#: attacks. Most attacks bring one new pitch alone (``SINGLE``) - or, driven,
#: show only as a new pitch (``PITCH_ONLY``) - and some of what sounds after
#: an attack is new but not all of it (``NEW_SHARE``: all is a chord change,
#: none is a chord struck again), and the pick changes note from one attack
#: to the next (``CHANGE``: a re-struck power chord keeps its root).
#: Measured over arpeggios up and down, open-chord picking, a minor pattern
#: and a Travis pattern at 80, 110 and 150 BPM through a clean, a crunch and
#: a high-gain amp, against triads, sevenths, open six-string chords, power
#: chords and eighth-note re-strums: arpeggios at single 0.53-0.96 clean or
#: pitch-only 0.33-1.00 driven, new share 0.14-0.70, change 0.91-1.00; no
#: chord part met all three (the near misses: single 0.55 with new share
#: 1.00, pitch-only 0.53 with new share 0.05, change 0.55).
ARPEGGIO_SINGLE = 0.5
ARPEGGIO_PITCH_ONLY = 0.3
ARPEGGIO_NEW_SHARE = (0.12, 0.85)
ARPEGGIO_CHANGE = 0.85

#: An attack is read from 85 ms just after it, against the 85 ms before.
ATTACK_WINDOW = 4096

#: A pick whose partials between the multiples of k carry under this share
#: of what the multiples carry is the note k times higher. Distortion's
#: difference tones put energy under a new note, at a pitch whose
#: "harmonics" are the real note's: half of a driven arpeggio came back an
#: octave or a twelfth low.
OCTAVE_EVIDENCE = 0.5

#: A ringing note has stopped once its partials fall this far below its
#: attack; none rings longer than ``RING_CAP_S``.
RING_DROP_DB = 12.0
RING_CAP_S = 4.0


def _attack_spectra(audio: np.ndarray, sample_rate: int, onsets) -> np.ndarray:
    """At each onset, what arrived: the spectrum after it less the one before."""
    hann = np.hanning(ATTACK_WINDOW)
    lead = int(0.01 * sample_rate)
    gap = int(0.005 * sample_rate)

    def spectrum(start: int) -> np.ndarray | None:
        if start < 0:
            return None
        piece = audio[start : start + ATTACK_WINDOW]
        if len(piece) < ATTACK_WINDOW:
            piece = np.pad(piece, (0, ATTACK_WINDOW - len(piece)))
        return np.abs(np.fft.rfft(piece * hann, n=N_FFT))

    rows = []
    for onset in onsets:
        at = int(onset * sample_rate)
        after = spectrum(at + lead)
        before = spectrum(at - gap - ATTACK_WINDOW)
        rows.append(after if before is None else np.maximum(after - before, 0.0))
    return np.array(rows) if rows else np.zeros((0, N_FFT // 2 + 1))


def _partials(spectrum: np.ndarray, f0: float, bin_hz: float, count: int = 12) -> np.ndarray:
    """The largest bin near each of a pitch's first ``count`` harmonics."""
    amplitudes = []
    for h in range(1, count + 1):
        centre = h * f0
        if centre > MAX_HZ:
            break
        idx = int(round(centre / bin_hz))
        half = max(1, int(round(PARTIAL_TOLERANCE * centre / bin_hz)))
        amplitudes.append(float(spectrum[max(1, idx - half) : idx + half + 1].max()))
    return np.array(amplitudes)


def _higher_note(midi: int, spectrum: np.ndarray, sample_rate: int, tuning_cents: float = 0.0, max_midi: int = 88) -> int:
    """Move a pick up an octave, a twelfth or two when its partials say so.

    A real note has energy at all of its harmonics; a difference tone under
    a B3 that the salience took for an E2 has it at the E2's 3rd, 6th, 9th -
    the B3's own - and little between.
    """
    bin_hz = sample_rate / N_FFT
    compressed = np.sqrt(spectrum)
    for _ in range(3):
        amplitudes = _partials(compressed, midi_to_hz(midi + tuning_cents / 100.0), bin_hz)
        numbers = np.arange(1, len(amplitudes) + 1)
        for k, step in ((2, 12), (3, 19), (4, 24)):
            on = numbers % k == 0
            if midi + step > max_midi or not on.any() or on.all():
                continue
            if float(amplitudes[~on].mean()) < OCTAVE_EVIDENCE * float(amplitudes[on].mean()):
                midi += step
                break
        else:
            break
    return midi


def _ring_out(events: list[NoteEvent], magnitude: np.ndarray, sample_rate: int, tuning_cents: float = 0.0) -> None:
    """Let each note ring until its partials fall away, it is picked again,
    or the cap - an arpeggio's notes sound over each other, and a DI that
    cut each one at the next attack would play a different part."""
    frame_s = HOP / sample_rate
    smear = N_FFT / 2 / sample_rate
    bin_hz = sample_rate / N_FFT
    floor = 10 ** (-RING_DROP_DB / 20)
    for i, event in enumerate(events):
        following = events[i + 1].start if i + 1 < len(events) else event.start + RING_CAP_S
        repick = next((e.start for e in events[i + 1 :] if round(e.midi) == round(event.midi)), math.inf)
        stop = min(repick, event.start + RING_CAP_S)
        first = int(event.start / frame_s) + 2
        if first + 3 > len(magnitude):
            # Picked at the very end of the clip: nothing left to measure it
            # by, so it rings to the end - and no shorter than a pick does.
            end = max(len(magnitude) * frame_s, event.start + 0.1)
            event.duration = round(max(0.005, min(end, stop) - event.start), 4)
            continue
        f0 = midi_to_hz(event.midi + tuning_cents / 100.0)

        def level(frame: int) -> float:
            return float(_partials(magnitude[frame], f0, bin_hz, count=4).sum())

        peak = max(level(f) for f in range(first, min(len(magnitude), first + 3)))
        last = first
        frame = first
        while frame < len(magnitude) and frame * frame_s < stop and level(frame) >= peak * floor:
            last = frame
            frame += 1
        # The analysis window still holds a note half a window after it stops.
        end = max((last + 1) * frame_s - smear, min(following, event.start + 0.1), event.start + 0.05)
        # Picked again sooner than that: it stops there, however short.
        event.duration = round(max(0.005, min(end, stop) - event.start), 4)


def _arpeggio(audio, sample_rate: int, magnitude: np.ndarray, min_midi: int, tuning_cents: float) -> tuple[list[NoteEvent], bool]:
    """One note per attack - the pitch that arrived with it - left to ring.

    Returns the notes, and whether the part sounds like an arpeggio (see
    ``ARPEGGIO_SINGLE``).
    """
    from .segment import pitch_onsets

    found_onsets = pitch_onsets(audio, sample_rate)
    onsets = found_onsets.times
    if not onsets:
        return [], False
    spectra = _attack_spectra(audio, sample_rate, onsets)
    found = salience_frames(spectra, sample_rate, min_midi=min_midi, tuning_cents=tuning_cents)
    # How hard each note was picked, against the part's typical pick - which
    # plays at 0.75, as single notes do (see _single_notes). Scaled from the
    # loudest instead, every pick sat near 0.9, and a DI picked that much
    # harder renders brighter: driven, it scored 0.126 against the part where
    # the right notes score 0.095; centred, 0.098.
    levels = 20 * np.log10(np.sqrt((spectra ** 2).sum(axis=1)) + 1e-12)
    typical = float(np.median(levels))
    events, single = [], 0
    for onset, pitches, spectrum, level in zip(onsets, found, spectra, levels):
        if not pitches:
            continue
        # A phantom an octave or a twelfth under the note it came with is
        # that note, the same check that names the pick. Counted as a second
        # pitch, it made most attacks of a clean arpeggio in a reverb look
        # like chords: 32 of 45 such parts read as arpeggios, 41 counted
        # this way, and still none of 108 chord parts, dry or wet.
        single += len({round(_higher_note(pitch, spectrum, sample_rate, tuning_cents)) for pitch, _ in pitches}) == 1
        midi = _higher_note(pitches[0][0], spectrum, sample_rate, tuning_cents)
        velocity = float(np.clip(0.75 + (level - typical) / 24, 0.1, 1.0))
        events.append(NoteEvent(start=round(onset, 4), duration=0.05, midi=float(midi), velocity=velocity))
    _ring_out(events, magnitude, sample_rate, tuning_cents)
    picks = [round(e.midi) for e in events]
    change = sum(a != b for a, b in zip(picks, picks[1:])) / (len(picks) - 1) if len(picks) > 1 else 0.0
    low, high = ARPEGGIO_NEW_SHARE
    arpeggio = (
        (single / len(onsets) >= ARPEGGIO_SINGLE or found_onsets.pitch_only >= ARPEGGIO_PITCH_ONLY)
        and low <= found_onsets.new_share <= high
        and change >= ARPEGGIO_CHANGE
    )
    return events, arpeggio


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
    """How to run basic-pitch here, if it can be run at all.

    It runs as its own command, so it need not live in rigsmith's Python:
    ``RIGSMITH_BASIC_PITCH`` names one, or a ``basic-pitch`` on the PATH is
    used - installed apart with pipx on a Python it supports, when it will
    not install on this one. Requiring it to import here as well left such
    an install unused.
    """
    configured = os.environ.get("RIGSMITH_BASIC_PITCH")
    if configured:
        return configured.split()
    found = shutil.which("basic-pitch")
    return [found] if found else None


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


#: A chord reading whose chords last longer than this, middling, with the
#: note-by-note estimate starting notes at twice as many moments, has lost
#: its attacks - under heavy drive two in eight were found and four chords
#: came back as two - and the pitch-class similarity judges between them.
SPARSE_CHORD_S = 1.2


def _keep_chords(audio, sample_rate: int, chords: list[NoteEvent], frames: list[NoteEvent]) -> bool:
    """Whether the chord reading stands over the note-by-note estimate.

    It does, nearly always: voiced from the spectrum, it read every
    synthetic chord part at least as well, and the similarity that used to
    choose - blind to octaves - picked the estimate on clean power chords
    (F1 0.64 against 0.95). Only when its attacks went missing (see
    ``SPARSE_CHORD_S``) does the similarity choose again.
    """
    starts = sorted({round(e.start, 3) for e in chords})
    spans = np.diff(starts + [len(audio) / sample_rate])
    if not len(spans) or float(np.median(spans)) <= SPARSE_CHORD_S:
        return True
    if len({round(e.start, 2) for e in frames}) < 2 * len(starts):
        return True
    return _chroma_similarity(audio, sample_rate, chords) >= _chroma_similarity(audio, sample_rate, frames)


#: An echo is taken out before the notes are read when its first repeat
#: comes back at least this loud against the note. A steady part's note
#: spacing can stand in the cepstrum like an echo; every dry synthetic part
#: that showed one came out at 0.105 or less, real delays at 0.155 and up.
ECHO_MIN_MIX = 0.15

#: The most of each repeat that is assumed to feed the next.
ECHO_MAX_FEEDBACK = 0.7

#: A steady low note puts the cepstrum's peaks at whole multiples of its
#: period, and one of them can stand as tall as a faint delay: a dry drop-A
#: riff showed an "echo" every 90.9 ms - five A1 cycles - at 15%. The note's
#: own period then stands far taller than the lag (3.8-4.0x); under a real
#: delay no fraction of the lag reached 1.5x.
ECHO_PITCH_RATIO = 2.0


def _without_echo(audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, str | None]:
    """The stem with its delay taken out, and a line saying so - or the stem.

    A delay's repeats are read as notes: on synthetic parts with a dotted
    eighth at 35% and 40% feedback, a lead came back with twice its notes
    and clean triads at F1 0.48. The echo's time is where the song map finds
    it (:func:`..song.describe._delay`). Its mix and feedback are read off
    the same power cepstrum - a feedback delay puts its mix at the lag and
    feedback x mix less half the mix squared at twice it; the windowed,
    band-limited cepstrum shows half of each - and the delay is undone with
    its inverse, (1 - fb z^-L) / (1 + (mix - fb) z^-L). Across mixes of 0.3-0.5
    and feedback 0.3-0.6 the parts' mean F1 went from 0.59-0.71 to 0.92-0.93.
    """
    from scipy.signal import lfilter

    from ..song.describe import _delay, power_cepstrum

    lag_ms, _, _ = _delay(audio, sample_rate, None)
    if not lag_ms:
        return audio, None
    near = int(round(lag_ms / 1000.0 * sample_rate))
    # Twice the lag has to fit in half the window: a half-note delay at a
    # slow tempo (from 683 ms) needs the longer one, or it was left in.
    size = 1 << 17
    while 2 * near + 4 >= size // 2 and size < 1 << 20:
        size <<= 1
    cepstrum = power_cepstrum(audio, sample_rate, size=size)
    if cepstrum is None or 2 * near + 4 >= len(cepstrum) // 2:
        return audio, None
    lag = near - 3 + int(np.argmax(cepstrum[near - 3 : near + 4]))
    mix = 2.0 * float(cepstrum[lag])
    if mix < ECHO_MIN_MIX:
        return audio, None
    fractions = [float(cepstrum[max(1, round(lag / k) - 2) : round(lag / k) + 3].max()) for k in range(2, 9)]
    if max(fractions) >= ECHO_PITCH_RATIO * float(cepstrum[lag]):
        return audio, None  # a note's period, many times over - not an echo
    around = cepstrum[2 * lag - 3 : 2 * lag + 4]
    second = 2.0 * float(around[int(np.argmax(np.abs(around)))])
    feedback = float(np.clip((2.0 * second + mix * mix) / (2.0 * mix), 0.0, ECHO_MAX_FEEDBACK))
    mix = min(mix, 0.95)
    b = np.zeros(lag + 1)
    a = np.zeros(lag + 1)
    b[0] = a[0] = 1.0
    b[lag] = -feedback
    a[lag] = mix - feedback
    dry = lfilter(b, a, audio)
    return dry, (
        f"an echo every {lag / sample_rate * 1000:.0f} ms ({mix:.0%} back, {feedback:.0%} of each repeat feeding the next) "
        "was taken out before the notes were read"
    )


def transcribe(
    audio,
    sample_rate: int,
    *,
    path: Path | None = None,
    mode: str = "auto",
    lowest: str | int = "auto",
    method: str = "auto",
    match_record_pitch: bool = True,
    echo: bool = True,
) -> Transcription:
    """Notes from a guitar stem.

    Args:
        mode: ``mono``, ``poly``, ``arpeggio`` (one note per attack, left to
            ring) or ``auto`` (decide from how many pitches sound at once,
            and how many each attack brings).
        lowest: Nothing below this is a note: the tuning's lowest string, or
            ``auto`` to infer it from the stem (see :func:`infer_lowest`).
        method: ``builtin``, ``basic-pitch``, or ``auto`` (basic-pitch when
            it can be run, else built in).
        match_record_pitch: Offset every note by the record's own deviation
            from concert pitch, so the rendered part sits where it does.
        echo: Take a delay's repeats out before reading the notes (see
            :func:`_without_echo`).
    """
    from ..match.pitch import analyse as pitch_analyse
    from .instrument import note_to_midi
    from .segment import detect_onsets, segment

    # A NaN sample from a damaged file made a near-silent stem a note as long
    # as the file; read it as silence.
    audio = np.nan_to_num(np.asarray(audio, dtype=np.float64), nan=0.0, posinf=0.0, neginf=0.0)
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

    if echo:
        audio, echo_text = _without_echo(audio, sample_rate)
        if echo_text:
            result.notes_text.append(echo_text)

    magnitude = _spectrogram(audio, sample_rate)
    frames = salience_frames(magnitude, sample_rate, min_midi=min_midi, tuning_cents=result.tuning_cents)
    if auto_lowest:
        bottom = infer_lowest(frames)
        if bottom < 40 and not _sounds_below(magnitude, sample_rate, bottom) and not _string_below(magnitude, frames, sample_rate, bottom):
            # The salience credits a pitch 2/3 under a note - an A1 under a
            # low E - and a part that keeps returning to its low notes moves
            # the percentile there. Below E2 it has to be heard, too.
            bottom = 40
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

    # Several pitches ringing, but each attack bringing one: an arpeggio.
    # Read as chords it came back a chord on every pick (F1 0.02-0.38).
    arpeggio = None
    if chosen == "arpeggio" or (mode == "auto" and chosen == "poly"):
        picked, sounds_like_one = _arpeggio(audio, sample_rate, magnitude, min_midi, result.tuning_cents)
        if chosen == "arpeggio" or (picked and sounds_like_one):
            arpeggio = picked

    if chosen == "mono":
        notes = notes if notes is not None else segment(audio, sample_rate)
        result.events = _single_notes(notes, min_midi)
        result.mode = "monophonic"
        # Power chords up the neck: a root and its fifth share a fundamental
        # an octave under the root, which the salience hears as one pitch -
        # so "one note at a time" read E5s on the A string as single notes an
        # octave low, or through distortion as almost nothing (F1 0-0.19,
        # against 0.60-1.00 read as chords). A string sounds its 5th and 7th
        # harmonics; that common fundamental has none of its own.
        if mode == "auto" and result.events and _odd_harmonics(audio, sample_rate, result.events) < ODD_HARMONICS:
            chords, said = _read_chords(audio, sample_rate, frames, onsets, energies, min_midi)
            if _chord_share(chords) >= CHORD_SHARE:
                result.events = chords
                result.mode = "polyphonic"
                result.notes_text.extend(said)
                result.notes_text.append("power chords: the single notes heard were their common fundamental, an octave under the root, where nothing is played")
    elif arpeggio is not None:
        result.events = arpeggio
        result.mode = "arpeggio"
        result.notes_text.append("one note per attack, each left to ring - read as an arpeggio")
    else:
        result.events, said = _read_chords(audio, sample_rate, frames, onsets, energies, min_midi)
        result.mode = "polyphonic"
        result.notes_text.extend(said)

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


def _read_chords(audio, sample_rate: int, frames, onsets, energies, min_midi: int) -> tuple[list[NoteEvent], list[str]]:
    """The part read as chords, and what was said about the reading."""
    said = []
    events = _notes_from_frames(frames, onsets, sample_rate, energies)
    # The note-by-note estimate hears a chord's common fundamental, an
    # octave or two below the chord, more often than the chord. The chords
    # read from their pitch classes and voiced from the spectrum are kept
    # whenever there are any: on every synthetic chord part they read at
    # least as well, and the pitch-class similarity that used to judge
    # between the two - blind to octaves - picked the note-by-note estimate
    # on clean power chords (F1 0.64 against 0.95, and 0.59 against 0.96 in
    # drop C).
    chords = _chords_from_chroma(audio, sample_rate, onsets, min_midi)
    if chords and _keep_chords(audio, sample_rate, chords, events):
        events = chords
        said.append("chords read from their pitch classes and voiced as a guitarist would")
    events, collapsed = _collapse_single_notes(audio, sample_rate, events)
    if collapsed:
        said.append(f"{collapsed} attack(s) were one note with distortion's harmonics on it, not a chord")
    return events, said


#: The 5th and 7th harmonics of the notes the single-note reading heard,
#: against their 4th, 6th and 8th. A string sounds all of them (0.57-0.67
#: measured, from an open-E riff to drop C and B standard, through crunch and
#: high gain). The common fundamental of a power chord - an octave under its
#: root, where nothing is played - has no 5th or 7th harmonic from either
#: string, only what distortion's intermodulation adds (0.19-0.28), and
#: clean, next to none. How much is heard at that fundamental itself did not
#: tell them apart: a cab takes a drop-C riff's away as well.
ODD_HARMONICS = 0.4

#: ...and the chord reading has to keep at least this share of the attacks as
#: chords (0.47 and up on the power-chord parts measured).
CHORD_SHARE = 0.4


def _odd_harmonics(audio: np.ndarray, sample_rate: int, events: list[NoteEvent]) -> float:
    """Median, over the notes, of their 5th and 7th harmonics against their
    4th, 6th and 8th, just after the attack."""
    ratios = []
    for event in events:
        a = int((event.start + 0.02) * sample_rate)
        b = int(min(event.start + event.duration, event.start + 0.3) * sample_rate)
        piece = audio[a:b]
        if len(piece) < 2048:
            continue
        size = 1 << int(np.ceil(np.log2(max(len(piece), 16384))))
        spectrum = np.abs(np.fft.rfft(piece * np.hanning(len(piece)), size))
        freqs = np.fft.rfftfreq(size, 1 / sample_rate)
        f0 = 440.0 * 2 ** ((event.midi - 69) / 12)

        def peak(hz: float) -> float:
            band = (freqs >= hz * 0.985) & (freqs <= hz * 1.015)
            return float(spectrum[band].max()) if band.any() else 0.0

        odd = peak(5 * f0) + peak(7 * f0)
        even = peak(4 * f0) + peak(6 * f0) + peak(8 * f0)
        ratios.append(odd / max(even, 1e-12))
    return float(np.median(ratios)) if ratios else 1.0


def _chord_share(events: list[NoteEvent]) -> float:
    """The share of attacks that sound more than one note."""
    attacks: dict[float, int] = {}
    for event in events:
        key = round(event.start, 3)
        attacks[key] = attacks.get(key, 0) + 1
    return sum(1 for count in attacks.values() if count >= 2) / len(attacks) if attacks else 0.0


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
#: The ways each shape is played, from the root up, for the spectrum to
#: choose between (:func:`_voice`): close, octave-doubled, barre, open six
#: strings - and, for a major, minor or seventh chord, the power chord its
#: "third" and "seventh" may really be: the root's fifth and seventh
#: harmonics. A drop-C power chord's C2 has both strong enough to read as a
#: C7 (F1 0.72 -> 0.95 driven with the power voicings offered).
CHORD_VOICING_OPTIONS = {
    "5": [(0, 7), (0, 7, 12)],
    "": [(0, 4, 7), (0, 4, 7, 12), (0, 7, 12, 16), (0, 7, 12, 16, 19), (0, 7, 12, 16, 19, 24), (0, 4, 7, 12, 16, 24), (0, 7), (0, 7, 12)],
    "m": [(0, 3, 7), (0, 3, 7, 12), (0, 7, 12, 15), (0, 7, 12, 15, 19), (0, 7, 12, 15, 19, 24), (0, 7), (0, 7, 12)],
    "sus4": [(0, 5, 7), (0, 7, 12, 17, 19)],
    "sus2": [(0, 2, 7), (0, 7, 12, 14, 19)],
    "7": [(0, 4, 7, 10), (0, 7, 10, 16, 19), (0, 7), (0, 7, 12)],
    "m7": [(0, 3, 7, 10), (0, 7, 10, 15, 19), (0, 7), (0, 7, 12)],
    "maj7": [(0, 4, 7, 11), (0, 7, 11, 16, 19), (0, 7), (0, 7, 12)],
}

#: What each note of a voicing beyond two costs against the spectrum's fit.
#: Distortion fills in every harmonic, and a five-string barre then fits a
#: two-note power chord as well as the power chord does. Swept 0.02-0.07 on
#: synthetic triads, sevenths, power chords, open chords and re-strums,
#: clean through high gain: 0.04 scored closest against the parts.
VOICING_NOTE_COST = 0.04

#: How much better a close voicing an octave above the heard root has to fit
#: than the best voicing on it, to be the one played. A bass bleeding into
#: the stem rings an octave under a chord, and the bass reading takes it for
#: the root: close triads and sevenths came back voiced from the bass's note
#: (F1 0.62-0.76 with a bass 20 dB under the guitar; 0.91-0.97 this way).
#: Below 0.06 the margin moved real low voicings - open chords, driven
#: power chords - up an octave.
OCTAVE_UP_MARGIN = 0.06

#: Harmonics in a voicing's template, and how each falls from the last.
VOICING_PARTIALS, VOICING_DECAY = 10, 0.8

#: The semitones a voicing's spectrum is read over: A1 to E7.
VOICING_RANGE = (28, 100)

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

#: How far below the stem's inferred lowest string a chord's own heard root
#: may sit: a drop tuning lowers its bottom string a whole step.
DROP_STEP = 2

#: How much better the higher candidate root must fit the notes to beat the
#: lower one. Tuned on synthetic parts (two render seeds): 0.02 gives back
#: most of the power chords' gain, 0.05 starts costing sevenths, 0.1 triads.
LOWER_ROOT_MARGIN = 0.04


def _bass_notes(audio: np.ndarray, sample_rate: int, start: float, end: float) -> list[float]:
    """Candidates for a chord's root as played: the lowest strong pitch in
    63-350 Hz just after an attack, and the pitch a fifth below it when one
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
    # From just under C2: a drop-C low C at 65 Hz was below the old 70 Hz
    # floor. Not lower: 60 Hz mains hum read as a B1 bass and every chord
    # over it collapsed to that one note.
    band = (freqs >= 63.0) & (freqs <= 350.0)
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


def _semitone_spectrum(audio: np.ndarray, sample_rate: int, start: float, end: float) -> np.ndarray | None:
    """The loudest (compressed) bin in each semitone just after an attack,
    as a unit vector - octaves kept apart, unlike a chroma."""
    a, b = int((start + 0.04) * sample_rate), int(min(end, start + 0.5) * sample_rate)
    piece = audio[a:b]
    if len(piece) < 2048:
        return None
    size = 1 << int(np.ceil(np.log2(max(len(piece), 16384))))
    spectrum = np.sqrt(np.abs(np.fft.rfft(piece * np.hanning(len(piece)), size)))
    low, high = VOICING_RANGE
    centres = 440.0 * 2.0 ** ((np.arange(low, high + 1) - 69) / 12.0)
    edges = np.searchsorted(np.fft.rfftfreq(size, 1 / sample_rate), np.concatenate([centres * 2 ** (-0.5 / 12), [centres[-1] * 2 ** (0.5 / 12)]]))
    # A band above Nyquist (a recording at a few kHz) has no bins at all.
    found = np.array([spectrum[edges[i] : max(edges[i] + 1, edges[i + 1])].max() if edges[i] < len(spectrum) else 0.0 for i in range(len(centres))])
    norm = float(np.linalg.norm(found))
    return found / norm if norm > 0 else None


def _voicing_template(notes) -> np.ndarray:
    low, high = VOICING_RANGE
    template = np.zeros(high - low + 1)
    for note in notes:
        for k in range(1, VOICING_PARTIALS + 1):
            m = note + int(round(12 * np.log2(k)))
            if low <= m <= high:
                template[m - low] += VOICING_DECAY ** (k - 1)
    return template / np.linalg.norm(template)


def _voice(audio: np.ndarray, sample_rate: int, start: float, end: float, base: int, shape: str) -> tuple[int, ...] | None:
    """How a chord is played, from what its octaves show.

    The chroma names the chord but folds every octave into one: an E5 played
    as two notes and a five-string E major barre look alike there, the E2's
    fifth harmonic standing in for the third. Here a real G#3 sits at 208 Hz
    and the harmonic at 412, so the voicing whose notes and harmonics best
    match the spectrum after the attack - less :data:`VOICING_NOTE_COST` a
    note - is the one played. Read as fixed barre shapes, synthetic triads
    came back at F1 0.49-0.51 a note; chosen this way, 0.93-0.97.
    """
    observed = _semitone_spectrum(audio, sample_rate, start, end)
    if observed is None:
        return None

    def fit(voicing, root: int) -> float:
        return float(np.dot(observed, _voicing_template([root + tone for tone in voicing]))) - VOICING_NOTE_COST * (len(voicing) - 2)

    played = max(CHORD_VOICING_OPTIONS[shape], key=lambda voicing: fit(voicing, base))
    if shape == "5":
        return played
    # The heard root may be a bass bleeding into the stem (OCTAVE_UP_MARGIN).
    close = [v for v in CHORD_VOICING_OPTIONS[shape] if len(v) <= 4 and max(v) <= 12 and v not in ((0, 7), (0, 7, 12))]
    if close:
        higher = max(close, key=lambda voicing: fit(voicing, base + 12))
        if fit(higher, base + 12) >= fit(played, base) + OCTAVE_UP_MARGIN:
            return tuple(tone + 12 for tone in higher)
    return played


def _chords_from_chroma(audio: np.ndarray, sample_rate: int, onsets, min_midi: int, *, hop_s: float = 0.02) -> list[NoteEvent]:
    """A chord at each attack, from the pitch classes that ring after it.

    The root is the lowest strong pitch in the bass - what a guitarist's
    root is, or the pitch a fifth below it when a driven power chord's root
    rings quieter than its fifth (:func:`_bass_notes`) - and the shape the
    one whose pitch classes, harmonics included, best match the chroma once
    each tone beyond a power chord has paid :data:`CHORD_TONE_COST`. Voiced
    as the octaves after the attack say it was played (:func:`_voice`).

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
        # The lowest string inferred for the whole stem misses a drop D's low
        # string when E2 is played too: a whole-stem spectrum cannot part
        # D2's lobe from E2's, so every D5 came back an octave up. A root
        # that is itself the lowest strong pitch after the attack - measured
        # over half a second, where the two part - is the note played, a
        # drop tuning's whole step below that floor at most: further down it
        # is hum or a bass bleeding into the stem. A fifth below, only
        # offered, is not.
        heard = bool(played) and played[0] == basses[0] and base >= min_midi - DROP_STEP
        while base < min_midi and not heard:
            base += 12
        for tone in _voice(audio, sample_rate, start, end, base, name) or CHORD_VOICINGS[name]:
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

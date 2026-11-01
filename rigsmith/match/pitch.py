"""What the guitar was tuned to when the take was made.

Two questions, and only one of them is the obvious one.

The obvious one is the tuning: was this drop D or E standard. That can only
be answered by what was actually played - if the low string never sounds, no
amount of analysis knows it was there - so this reports the lowest note it
heard and says plainly that a lower one may simply not have been played.

The one that matters more for measurement is whether the instrument was at
concert pitch. A take recorded thirty cents flat puts every harmonic in a
slightly different place than the record it is being compared with, and the
band comparison reads that as a tone difference. It is small, it is real, and
nothing else here would ever surface it.

The estimator is autocorrelation, which is old, cheap, and entirely adequate
for a monophonic-ish guitar part. It is not a polyphonic transcriber and does
not pretend to be: chords get a confidence low enough to be ignored.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

#: Concert pitch. Everything is measured against 12-TET from here.
A4_HZ = 440.0

NOTE_NAMES = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")

#: The guitar and 4-string bass usable fundamental range, generously bounded:
#: a 4-string bass E1 is 41.2 Hz and the 24th fret of the high E is 1319 Hz.
#: Drop/5-string bass callers pass ``min_f0=25.0`` to reach A0 (27.5 Hz).
MIN_F0 = 40.0
BASS_MIN_F0 = 25.0
MAX_F0 = 1400.0

#: A lag half as long whose correlation comes within this share of the
#: strongest is the note; the strongest is then its octave below. Every
#: note correlates at twice its period too, and with the floor low enough
#: for a drop-A riff, an E2's doubled period is inside the search.
SUBOCTAVE_SHARE = 0.9

#: Deviation from concert pitch worth mentioning. Five cents is inaudible and
#: within the estimator's own error; fifteen starts to move harmonics enough
#: for a band comparison to notice.
CENTS_TOLERANCE = 15.0

#: A window has to be at least this periodic to count as a pitched note.
MIN_CLARITY = 0.55

#: The lowest strings of the tunings people actually use.
COMMON_TUNINGS = {
    "E standard": "E2",
    "Eb standard": "D#2",
    "D standard": "D2",
    "drop D": "D2",
    "drop C#": "C#2",
    "drop C": "C2",
    "drop B": "B1",
    "C standard": "C2",
    "open G": "D2",
    "open D": "D2",
    "Bass E standard": "E1",
    "Bass drop D": "D1",
    "Bass Eb standard": "D#1",
    "Bass D standard": "D1",
    "Bass C standard": "C1",
    "Bass 5-string standard": "B0",
    "Bass 5-string drop A": "A0",
}


def note_name(frequency: float) -> tuple[str, float]:
    """Nearest 12-TET note and the deviation from it, in cents."""
    if frequency <= 0:
        return "?", 0.0
    semitones = 12 * math.log2(frequency / A4_HZ)
    nearest = round(semitones)
    cents = (semitones - nearest) * 100.0

    index = (nearest + 9) % 12  # A4 is index 9 of octave 4
    octave = 4 + (nearest + 9) // 12
    return f"{NOTE_NAMES[index]}{octave}", cents


@dataclass
class PitchReport:
    """What was heard, and how much of it to believe."""

    notes: int = 0
    """How many windows held something pitched enough to measure."""
    cents_offset: float = 0.0
    """Median deviation from concert pitch, across those windows."""
    lowest_hz: float = 0.0
    lowest_note: str = ""
    lowest_cents: float = 0.0
    frequencies: list[float] = field(default_factory=list)

    @property
    def measured(self) -> bool:
        """Enough pitched material to say anything at all."""
        return self.notes >= 5

    @property
    def in_concert_pitch(self) -> bool:
        return not self.measured or abs(self.cents_offset) <= CENTS_TOLERANCE

    def candidate_tunings(self) -> list[str]:
        """Tunings whose lowest string matches the lowest note heard."""
        if not self.measured or not self.lowest_note:
            return []
        return sorted(
            name for name, low in COMMON_TUNINGS.items() if low == self.lowest_note
        )

    def concerns(self, *, stated_tuning: str | None = None) -> list[str]:
        notes: list[str] = []
        if not self.measured:
            return notes

        if not self.in_concert_pitch:
            direction = "sharp" if self.cents_offset > 0 else "flat"
            notes.append(
                f"this take is about {abs(self.cents_offset):.0f} cents "
                f"{direction} of concert pitch. Every harmonic sits slightly "
                "off where the record's does, and a band comparison reads "
                "that as a tone difference."
            )

        if stated_tuning:
            expected = COMMON_TUNINGS.get(stated_tuning.strip())
            if expected and self.lowest_note and self.lowest_note != expected:
                notes.append(
                    f"labelled '{stated_tuning}', whose lowest string is "
                    f"{expected}, but the lowest note here is "
                    f"{self.lowest_note}. That is not proof of a wrong label - "
                    "a lower string simply may not have been played."
                )
        return notes

    def describe(self) -> str:
        if not self.measured:
            return "not enough pitched material to measure tuning"
        bits = [f"lowest note {self.lowest_note}"]
        candidates = self.candidate_tunings()
        if candidates:
            bits.append("consistent with " + " or ".join(candidates))
        bits.append(
            "at concert pitch"
            if self.in_concert_pitch
            else f"{self.cents_offset:+.0f} cents off concert pitch"
        )
        return ", ".join(bits)

    def to_dict(self) -> dict:
        return {
            "measured": self.measured,
            "notes": self.notes,
            "cents_offset": self.cents_offset,
            "in_concert_pitch": self.in_concert_pitch,
            "lowest_hz": self.lowest_hz,
            "lowest_note": self.lowest_note,
            "candidate_tunings": self.candidate_tunings(),
            "describe": self.describe(),
        }


def _estimate_f0(
    window: np.ndarray, sample_rate: int, *, min_f0: float = MIN_F0
) -> tuple[float, float]:
    """Autocorrelation pitch for one window: ``(frequency, clarity)``.

    Clarity is the normalised autocorrelation at the chosen lag - near 1 for
    a clean periodic note, low for a chord or a transient. It is what keeps
    this from reporting confident nonsense about polyphonic material.
    """
    window = window - window.mean()
    energy = float(np.dot(window, window))
    if energy <= 1e-9:
        return 0.0, 0.0

    # Full autocorrelation via FFT, which is the only part that has to be fast.
    size = 1 << (2 * len(window) - 1).bit_length()
    spectrum = np.fft.rfft(window, size)
    correlation = np.fft.irfft(spectrum * np.conjugate(spectrum), size)[: len(window)]

    min_lag = max(2, int(sample_rate / MAX_F0))
    max_lag = min(len(correlation) - 1, int(sample_rate / min_f0))
    if max_lag <= min_lag:
        return 0.0, 0.0

    # Skip the initial descending lobe from lag 0 so low bass notes (whose
    # first zero-crossing sits past min_lag) do not pick min_lag as the peak.
    search_start = min_lag
    while search_start < max_lag and correlation[search_start] > 0 and correlation[search_start] >= correlation[search_start + 1]:
        search_start += 1
    if search_start >= max_lag:
        return 0.0, 0.0

    segment = correlation[search_start : max_lag + 1]
    if min_f0 < 40.0:
        taper = np.maximum(len(window) * 0.25, len(window) - np.arange(search_start, max_lag + 1, dtype=np.float64))
        peak = int(np.argmax(segment / taper)) + search_start
    else:
        peak = int(np.argmax(segment)) + search_start
    # Prefer the shorter period when a divisor (1/2 or 1/3 period) is nearly as strong.
    divisors = (2, 3) if min_f0 < 40.0 else (2,)
    improved = True
    while improved:
        improved = False
        for div in divisors:
            if peak // div >= search_start:
                center = int(round(peak / div))
                radius = max(3, int(round(center * 0.04)))
                lo = max(search_start, center - radius)
                hi = min(len(correlation), center + radius + 1)
                around = correlation[lo:hi]
                if len(around):
                    shorter = int(np.argmax(around)) + lo
                    if correlation[shorter] >= SUBOCTAVE_SHARE * correlation[peak]:
                        peak = shorter
                        improved = True
                        break
    clarity = float(correlation[peak] / correlation[0]) if correlation[0] else 0.0
    if clarity < MIN_CLARITY:
        return 0.0, clarity

    # Parabolic interpolation around the peak, so the answer is not quantised
    # to whole samples - at 48 kHz that quantisation alone is tens of cents.
    # Debias the triangular overlap taper (N - lag) at long lags so low bass
    # strings (A0/B0/E1) do not read a few cents sharp.
    if 0 < peak < len(correlation) - 1:
        n = float(len(window))
        before = correlation[peak - 1] / max(1.0, n - (peak - 1))
        centre = correlation[peak] / max(1.0, n - peak)
        after = correlation[peak + 1] / max(1.0, n - (peak + 1))
        denominator = before - 2 * centre + after
        if denominator < 0:
            peak = peak + 0.5 * (before - after) / denominator

    return (sample_rate / peak if peak else 0.0), clarity


def analyse(
    audio: np.ndarray, sample_rate: int, *, window_seconds: float = 0.09
) -> PitchReport:
    """Measure the pitch content of a recording."""
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim > 1:
        audio = audio.mean(axis=tuple(range(1, audio.ndim)))

    window = max(256, int(window_seconds * sample_rate))
    hop = window // 2
    if len(audio) < window:
        return PitchReport()

    peak = float(np.abs(audio).max())
    if peak <= 0:
        return PitchReport()
    floor = peak * 0.05

    frequencies: list[float] = []
    for start in range(0, len(audio) - window, hop):
        chunk = audio[start : start + window]
        if float(np.abs(chunk).max()) < floor:
            continue
        frequency, _ = _estimate_f0(chunk, sample_rate)
        if MIN_F0 <= frequency <= MAX_F0:
            frequencies.append(frequency)

    report = PitchReport(notes=len(frequencies), frequencies=frequencies)
    if not frequencies:
        return report

    deviations = [note_name(f)[1] for f in frequencies]
    report.cents_offset = float(np.median(deviations))

    # The lowest note, taken as a low percentile rather than the minimum -
    # one bad window should not decide what the guitar was tuned to.
    report.lowest_hz = float(np.percentile(frequencies, 5))
    report.lowest_note, report.lowest_cents = note_name(report.lowest_hz)
    return report


#: Enough material to measure, short enough that a five-minute file is not a
#: five-minute wait. Taken from the middle, where the playing usually is.
MAX_ANALYSIS_SECONDS = 30.0


def analyse_file(path, *, max_seconds: float = MAX_ANALYSIS_SECONDS, **kwargs):
    """Measure a file on disk, sampling the middle of a long one."""
    from .source import load

    audio, sample_rate = load(path)
    limit = int(max_seconds * sample_rate)
    if len(audio) > limit:
        start = (len(audio) - limit) // 2
        audio = audio[start : start + limit]
    return analyse(audio, sample_rate, **kwargs)

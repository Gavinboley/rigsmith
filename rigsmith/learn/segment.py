"""Cutting a DI into notes.

A DI is the easy case for this and it is worth saying why: there is no amp
smearing the attacks, no reverb filling the gaps, no second instrument. A
plucked note is a sharp broadband rise followed by an exponential decay, and
that shape is what everything here looks for.

What comes out is a list of :class:`Note` - where it starts and stops, the
pitch it settled on, how hard it was played, how quickly it died and how
bright it was. That is enough to build a sampler from (:mod:`.profile`) and to
notice a guitar that is out of tune, has a dead fret, or has one string much
quieter than the others - each of which would otherwise be measured later as
a tone difference.

Onsets are found from spectral flux - the frame-to-frame *increase* in
log-magnitude, summed over frequency - with an adaptive threshold, so a soft
note after a loud one still counts. Where the flux misses a legato note (a
hammer-on has almost no attack), a jump in the stable pitch splits it anyway.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import NamedTuple

import numpy as np

from ..match.pitch import MAX_F0, MIN_F0, _estimate_f0
from .instrument import hz_to_midi, midi_to_note

#: STFT shape used throughout. 2048 at 48 kHz resolves the low E's harmonics;
#: a 256-sample hop places an onset to within about 5 ms.
N_FFT = 2048
HOP = 256

#: Two onsets closer than this are one onset with a messy attack.
MIN_GAP_S = 0.06

#: Flux is read up to here for onsets. Above it a guitar has little left to
#: rise, and a drum bleeding into a separated stem has plenty - a hi-hat 35 dB
#: under the guitar is a huge rise in bins the guitar left near silent. Read
#: over every bin, bleed at -25 dB turned clean power chords into a chord on
#: every eighth (F1 0.92 -> 0.61) and high-gain re-strums into a third of
#: themselves (0.33); read to 6 kHz, 0.91 and 0.86.
FLUX_BAND_HZ = 6000.0

#: A note shorter than this has nothing to sample.
MIN_NOTE_S = 0.08

#: Where the pitch is read: after the pick noise, before the decay thins out.
PITCH_FROM_S = 0.04
PITCH_TO_S = 0.30
#: Below this, a note's clarity is measured over four of its cycles.
LOW_NOTE_HZ = 75.0
#: A note shorter than this is read from nearly its attack.
SHORT_NOTE_S = 0.2
SHORT_PITCH_FROM_S = 0.005

#: A note ends when it has fallen this far below its own peak.
END_DROP_DB = 45.0

#: A semitone-and-a-bit jump in the settled pitch means a new note, even with
#: no attack - a hammer-on, a pull-off, a slide arriving.
PITCH_SPLIT_SEMITONES = 0.8


@dataclass
class Note:
    """One note as it was played."""

    start: float
    end: float
    hz: float = 0.0
    midi: float = 0.0
    """Fractional: 40.12 is an E2 twelve cents sharp."""
    clarity: float = 0.0
    """How periodic it was. Low means a chord, a mute, or a scrape."""
    peak_db: float = -120.0
    rms_db: float = -120.0
    decay_db_s: float = 0.0
    """How fast it died, in dB per second. Negative."""
    centroid_hz: float = 0.0
    """Brightness during the sustain."""
    attack_centroid_hz: float = 0.0
    """Brightness of the first 30 ms - the pick, mostly."""
    string: int | None = None
    fret: int | None = None
    label: str | None = None
    """What a protocol expected here, if one was being followed."""
    flags: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def pitched(self) -> bool:
        return self.hz > 0 and self.clarity >= 0.55

    @property
    def nearest(self) -> int:
        return int(round(self.midi)) if self.pitched else 0

    @property
    def cents(self) -> float:
        return 100.0 * (self.midi - self.nearest) if self.pitched else 0.0

    @property
    def name(self) -> str:
        return midi_to_note(self.midi) if self.pitched else "?"

    @property
    def t60(self) -> float | None:
        return -60.0 / self.decay_db_s if self.decay_db_s < -0.5 else None

    def describe(self) -> str:
        where = f"  s{self.string}f{self.fret}" if self.string else ""
        pitch = f"{self.name} {self.cents:+.0f}c" if self.pitched else "unpitched"
        return (
            f"{self.start:7.2f}s  {pitch:<12} {self.peak_db:6.1f} dBFS  "
            f"{self.duration:4.2f}s{where}"
            + (f"  [{', '.join(self.flags)}]" if self.flags else "")
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(
            name=self.name, cents=round(self.cents, 1), pitched=self.pitched,
            duration=round(self.duration, 4), t60=self.t60,
        )
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Note":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------
# spectral helpers
# --------------------------------------------------------------------------


def _frames(audio: np.ndarray, n_fft: int = N_FFT, hop: int = HOP) -> np.ndarray:
    """Magnitude spectrogram, frames along axis 0."""
    padded = np.pad(audio, (n_fft // 2, n_fft // 2))
    count = 1 + (len(padded) - n_fft) // hop
    if count <= 0:
        return np.zeros((0, n_fft // 2 + 1))
    strides = (padded.strides[0] * hop, padded.strides[0])
    frames = np.lib.stride_tricks.as_strided(padded, (count, n_fft), strides)
    window = np.hanning(n_fft)
    return np.abs(np.fft.rfft(frames * window, axis=1))


def onset_strength(audio: np.ndarray, sample_rate: int, *, band_hz: float | None = None) -> np.ndarray:
    """Spectral flux with a small frequency max-filter (SuperFlux-style).

    The max-filter keeps vibrato and slight pitch drift from reading as a
    stream of tiny onsets: a partial that moved one bin is compared against
    the loudest of its neighbours in the previous frame, not its old bin.
    Bins more than 60 dB under the loudest in the file are left out, because
    noise twitching near the floor rises in half of them every frame.
    ``band_hz`` leaves out the bins above it (see :data:`FLUX_BAND_HZ`).
    """
    magnitude = _frames(np.asarray(audio, dtype=np.float64))
    if len(magnitude) < 2:
        return np.zeros(len(magnitude))
    if band_hz:
        magnitude = magnitude[:, : int(band_hz / (sample_rate / N_FFT)) + 1]
    top = float(magnitude.max()) or 1.0
    gated = np.maximum(magnitude, top * 1e-3)
    log = 20 * np.log10(gated / top)
    from scipy.ndimage import maximum_filter1d

    reference = maximum_filter1d(log, size=3, axis=1)
    rise = log[1:] - reference[:-1]
    flux = np.maximum(rise, 0.0).mean(axis=1)
    return np.concatenate([[0.0], flux])


def _pick_peaks(strength: np.ndarray, sample_rate: int, *, sensitivity: float = 1.0) -> list[int]:
    """Frames where the flux peaks above a moving median."""
    if len(strength) == 0 or float(strength.max()) <= 0:
        return []
    from scipy.ndimage import median_filter

    frame_s = HOP / sample_rate
    span = max(3, int(0.5 / frame_s) | 1)
    baseline = median_filter(strength, size=span, mode="nearest")
    scale = float(np.percentile(strength, 99)) or float(strength.max())
    threshold = baseline + (0.2 / max(sensitivity, 1e-3)) * scale

    gap = max(1, int(MIN_GAP_S / frame_s))
    peaks: list[int] = []
    for i in range(1, len(strength) - 1):
        if strength[i] < threshold[i]:
            continue
        if strength[i] < strength[i - 1] or strength[i] < strength[i + 1]:
            continue
        if peaks and i - peaks[-1] < gap:
            if strength[i] > strength[peaks[-1]]:
                peaks[-1] = i
            continue
        peaks.append(i)
    return peaks


#: A pluck raises the level. Flux alone also fires on the beating inside a
#: decaying note, which raises nothing - so an onset has to be confirmed by
#: the level climbing at least this much over the 40 ms before it.
ONSET_RISE_DB = 4.0

#: Under distortion a note picked on another string while the last one
#: rings barely moves the level - the amp holds it flat - so the rise test
#: threw away every attack of a driven arpeggio but two in 128. What does
#: change is the spectrum: the new note's partials land in bins that were
#: quiet. Asked for (:func:`pitch_onsets`), an onset is confirmed by that
#: too - when this share of the live energy after it sits in bins that
#: jumped by at least ``NEW_PITCH_DB``. Measured: a driven arpeggio's
#: attacks bring 0.04 and up, a decaying note's own flux brings none.
NEW_PITCH_DB = 9.0
NEW_PITCH_SHARE = 0.02

#: The flux threshold for those onsets, as :func:`_pick_peaks`'s
#: sensitivity. A new pitch arriving is confirmation enough that a softer
#: peak can be let through: driven, a Travis pattern's thumb and fingers
#: then read note by note (F1 0.40-0.55 -> 0.90-0.92) and no chord part on
#: the 99-part grid was taken for an arpeggio.
PITCH_ONSET_SENSITIVITY = 2.0

#: A part cut from a record usually starts on a downbeat - an attack at its
#: very first sample, where the rise test has no "before" to rise from, and
#: every part's first note went missing. Audio that opens within this much
#: of its own loud level, with no onset in its first 50 ms, starts with one -
#: when it has attacks of its own later and quieter moments
#: ``OPENING_DYNAMICS_DB`` under the opening (silence, steady noise and room
#: tone have neither), and the opening holds for 20-60 ms (a count-in click
#: does not).
OPENING_DB = 20.0
OPENING_DYNAMICS_DB = 6.0

#: The first frames' windows hang over the zero padding before the audio,
#: so anything that opens non-silent shows a rise there - steady noise read
#: as a note at 0. The opening is left to :func:`_opening` and its tests.
EDGE_FRAMES = N_FFT // HOP // 2


def rise_threshold(envelope: np.ndarray) -> float:
    """How much a level has to climb to count as an attack, for this material.

    A clean DI swings 30-40 dB between a fresh pluck and a decayed note, so a
    4 dB rise is a modest test. A distorted stem is compressed to within a few
    dB of itself - a new pluck can raise it by one - and the same test throws
    away five notes in six. So the threshold scales with the material's own
    dynamic range: a quarter of the spread of its active frames.
    """
    active = envelope[envelope > np.percentile(envelope, 30)]
    if len(active) < 8:
        return ONSET_RISE_DB
    spread = float(np.percentile(active, 90) - np.percentile(active, 10))
    return float(np.clip(0.25 * spread, 1.0, ONSET_RISE_DB))


def _rises(envelope: np.ndarray, frame: int, sample_rate: int, *, sensitivity: float, needed: float = ONSET_RISE_DB) -> bool:
    span = max(1, int(0.04 * sample_rate / HOP))
    before = envelope[max(0, frame - span) : max(1, frame - 1)]
    after = envelope[frame : frame + span]
    if len(after) == 0:
        return False
    if len(before) == 0:
        return True
    # Compare power, not dB, for the "before": a mean of dB values is dragged
    # down by one silent hop and would wave through a rise that is not there.
    level_before = 10 * np.log10(np.mean(10 ** (before / 10)))
    return float(after.max()) - float(level_before) >= needed / max(sensitivity, 1e-3)


def _envelope_db(audio: np.ndarray, sample_rate: int, hop: int = HOP) -> np.ndarray:
    """RMS level per hop, in dBFS."""
    usable = (len(audio) // hop) * hop
    if usable == 0:
        return np.array([-120.0])
    blocks = audio[:usable].reshape(-1, hop)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    return 20 * np.log10(np.maximum(rms, 1e-6))


def _centroid(segment: np.ndarray, sample_rate: int) -> float:
    if len(segment) < 64:
        return 0.0
    spectrum = np.abs(np.fft.rfft(segment * np.hanning(len(segment))))
    freqs = np.fft.rfftfreq(len(segment), 1 / sample_rate)
    total = float(spectrum.sum())
    return float((freqs * spectrum).sum() / total) if total > 0 else 0.0


def _settled_pitch(segment: np.ndarray, sample_rate: int) -> tuple[float, float]:
    """Median pitch over the stable part of a note, and its clarity."""
    window = int(0.046 * sample_rate)
    hop = window // 2
    first = int(PITCH_FROM_S * sample_rate)
    # A palm-muted note is over in 70 ms: waiting 40 ms for it to settle
    # left only its tail to read, and a stream of chugs came back unpitched.
    if len(segment) < SHORT_NOTE_S * sample_rate:
        first = int(SHORT_PITCH_FROM_S * sample_rate)
    last = min(len(segment), int(PITCH_TO_S * sample_rate))
    estimates, clarities = [], []
    loudest = float(np.max(np.abs(segment))) if len(segment) else 0.0
    for start in range(first, max(first + 1, last - window), hop):
        chunk = segment[start : start + window]
        if len(chunk) < window:
            break
        # A window that has already decayed into silence says nothing about
        # the note, and counting its zero clarity would call a short note
        # unpitched.
        if float(np.max(np.abs(chunk))) < loudest * 0.03:
            continue
        hz, clarity = _estimate_f0(chunk, sample_rate)
        clarities.append(clarity)
        if MIN_F0 <= hz <= MAX_F0:
            estimates.append(hz)
    if not estimates:
        return 0.0, float(np.median(clarities)) if clarities else 0.0
    # Octave errors are the autocorrelator's characteristic mistake; the
    # median of a handful of windows throws out the odd one.
    hz = float(np.median(estimates))
    clarity = float(np.median(clarities))
    # A 46 ms window holds two and a half cycles of a drop-A string, and the
    # correlation of so few reads as unclear however clean the note is. A low
    # note's clarity is measured over four cycles of it instead.
    if hz < LOW_NOTE_HZ:
        long_window = int(4 * sample_rate / hz)
        longer = [
            _estimate_f0(segment[start : start + long_window], sample_rate)[1]
            for start in range(first, max(first + 1, len(segment) - long_window), long_window // 2)
            if len(segment[start : start + long_window]) == long_window
            and float(np.max(np.abs(segment[start : start + long_window]))) >= loudest * 0.03
        ]
        if longer:
            clarity = max(clarity, float(np.median(longer)))
    return hz, clarity


# --------------------------------------------------------------------------
# the segmenter
# --------------------------------------------------------------------------


def detect_onsets(audio, sample_rate: int, *, sensitivity: float = 1.0) -> list[float]:
    """Onset times in seconds."""
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    strength = onset_strength(audio, sample_rate, band_hz=FLUX_BAND_HZ)
    peaks = [p for p in _pick_peaks(strength, sample_rate, sensitivity=sensitivity) if p >= EDGE_FRAMES]
    envelope = _envelope_db(audio, sample_rate)
    needed = rise_threshold(envelope)
    peaks = [p for p in peaks if _rises(envelope, p, sample_rate, sensitivity=sensitivity, needed=needed)]
    coarse = [max(0.0, (p - 1) * HOP / sample_rate) for p in peaks]
    return _opening(audio, sample_rate, envelope, [refine_onset(audio, sample_rate, t) for t in coarse])


def _opening(audio: np.ndarray, sample_rate: int, envelope: np.ndarray, onsets: list[float]) -> list[float]:
    """``onsets``, with one at the start when the audio opens already
    playing (see :data:`OPENING_DB`)."""
    span = max(1, int(0.05 * sample_rate / HOP))
    if not onsets or onsets[0] <= 0.05 or len(envelope) <= 3 * span:
        return onsets
    opening = float(envelope[:span].max())
    if opening < float(np.percentile(envelope, 90)) - OPENING_DB:
        return onsets
    # Notes decay and phrases breathe; steady noise and room tone do not.
    if opening < float(np.percentile(envelope, 10)) + OPENING_DYNAMICS_DB:
        return onsets
    held = float(np.median(envelope[int(0.02 * sample_rate / HOP) : int(0.06 * sample_rate / HOP) + 1]))
    if held < opening - OPENING_DB:
        return onsets
    return [refine_onset(audio, sample_rate, 0.0)] + onsets


class PitchOnsets(NamedTuple):
    times: list[float]
    pitch_only: float
    """Share of the onsets that only a new pitch confirmed: near 1 for a
    driven arpeggio, near 0 for anything strummed."""
    new_share: float
    """Median :func:`new_pitch_share` at the onsets: 1 where every attack
    changes chord, near 0 where a chord is struck again, between for an
    arpeggio - one new note among ringing ones."""


def pitch_onsets(audio, sample_rate: int) -> PitchOnsets:
    """Onsets confirmed by a rise in level *or* by a new pitch arriving.

    For parts where notes ring over each other - arpeggios, let-ring
    picking - where an attack under distortion shows only as new partials.
    """
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    magnitude = _frames(audio)
    if len(magnitude) < 2:
        return PitchOnsets([], 0.0, 0.0)
    # Candidates from the whole spectrum, and from the guitar's band where
    # the whole spectrum has none: a driven Travis pattern's picks show mostly
    # above the band, while a drum's bleed there sets a threshold an
    # arpeggio's own attacks fall under. A drum brings no new pitch, so the
    # confirmation below still keeps it out.
    gap = max(1, int(MIN_GAP_S * sample_rate / HOP))
    full = _pick_peaks(onset_strength(audio, sample_rate), sample_rate, sensitivity=PITCH_ONSET_SENSITIVITY)
    banded = _pick_peaks(onset_strength(audio, sample_rate, band_hz=FLUX_BAND_HZ), sample_rate, sensitivity=PITCH_ONSET_SENSITIVITY)
    candidates = sorted(full + [b for b in banded if all(abs(b - f) >= gap for f in full)])
    peaks = []
    for peak in candidates:
        if peak >= EDGE_FRAMES and (not peaks or peak - peaks[-1] >= gap):
            peaks.append(peak)
    envelope = _envelope_db(audio, sample_rate)
    needed = rise_threshold(envelope)
    top = float(magnitude.max()) or 1.0
    level = 20 * np.log10(np.maximum(magnitude, top * 1e-5))
    kept, shares, pitch_only = [], [], 0
    for peak in peaks:
        share = new_pitch_share(level, peak)
        if _rises(envelope, peak, sample_rate, sensitivity=1.0, needed=needed):
            kept.append(peak)
        elif share >= NEW_PITCH_SHARE:
            kept.append(peak)
            pitch_only += 1
        else:
            continue
        shares.append(share)
    if not kept:
        return PitchOnsets([], 0.0, 0.0)
    coarse = [max(0.0, (p - 1) * HOP / sample_rate) for p in kept]
    # Refined one by one, two close onsets could swap; the notes read from
    # them are rung out in order.
    onsets = _opening(audio, sample_rate, envelope, sorted(refine_onset(audio, sample_rate, t) for t in coarse))
    return PitchOnsets(onsets, pitch_only / len(kept), float(np.median(shares)))


def new_pitch_share(level_db: np.ndarray, frame: int) -> float:
    """How much of the energy after ``frame`` is in bins that were quiet.

    "Before" is the loudest each bin was over the 40 ms up to the attack, so
    a partial that was already ringing - however it beats - is not new.
    "After" starts once the analysis window has cleared the attack itself.
    """
    reach = N_FFT // HOP // 2
    before = level_db[max(0, frame - 3 * reach) : max(1, frame - reach)]
    after = level_db[frame + reach : frame + reach + 6]
    if not len(after) or not len(before):
        return 0.0
    before = before.max(axis=0)
    after = after.mean(axis=0)
    live = after > after.max() - 40.0
    new = live & (after - before > NEW_PITCH_DB)
    power = 10 ** (after / 10)
    total = float(power[live].sum())
    return float(power[new].sum()) / total if total > 0 else 0.0


def refine_onset(audio: np.ndarray, sample_rate: int, coarse: float) -> float:
    """Move an STFT onset to where the attack actually starts.

    Spectral flux places an onset by frame, and a centred 2048-point frame
    reports it about 17 ms early on every note - consistent, so harmless for
    counting notes and ruinous for aligning a DI to a record sample by sample.
    So the time domain decides: the first millisecond, near the coarse
    estimate, where the level climbs past a fifth of the attack's peak.
    """
    ms = max(1, sample_rate // 1000)
    first = max(0, int((coarse - 0.01) * sample_rate))
    last = min(len(audio), int((coarse + 0.06) * sample_rate))
    span = audio[first:last]
    if len(span) < 4 * ms:
        return coarse
    usable = (len(span) // ms) * ms
    blocks = np.abs(span[:usable]).reshape(-1, ms).max(axis=1)
    if not len(blocks) or blocks.max() <= 0:
        return coarse
    # Measure against the level just before, so a note arriving over the
    # previous one's tail is found where *it* starts, not where the tail was.
    before = float(np.abs(audio[max(0, first - 10 * ms) : first]).max()) if first else 0.0
    threshold = before + 0.2 * (float(blocks.max()) - before)
    crossing = np.nonzero(blocks > threshold)[0]
    if not len(crossing):
        return coarse
    return round((first + int(crossing[0]) * ms) / sample_rate, 4)


#: The noise floor is the envelope's 10th percentile - the gaps between
#: phrases - but no higher than this far under the part's loud level. A part
#: that never goes quiet, each note held into the next through high gain, has
#: its playing level for a 10th percentile, and every note failed "12 dB over
#: the floor": the demo song's solo rhythm read as no notes at all, and a
#: driven pedal note at 0.50, against 0.99 with the floor held down.
FLOOR_UNDER_LOUD_DB = 30.0


def _floor(envelope: np.ndarray) -> float:
    return min(float(np.percentile(envelope, 10)), float(np.percentile(envelope, 95)) - FLOOR_UNDER_LOUD_DB)


def segment(
    audio,
    sample_rate: int,
    *,
    sensitivity: float = 1.0,
    split_on_pitch: bool = True,
) -> list[Note]:
    """Every note in a DI, measured.

    Args:
        sensitivity: Above 1 finds softer onsets (and more false ones).
        split_on_pitch: Also split where the settled pitch jumps without an
            attack - legato playing. Off for chords, where it only adds noise.
    """
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if len(audio) < int(MIN_NOTE_S * sample_rate):
        return []

    onsets = detect_onsets(audio, sample_rate, sensitivity=sensitivity)
    envelope = _envelope_db(audio, sample_rate)
    floor = _floor(envelope)
    total = len(audio) / sample_rate

    notes: list[Note] = []
    for index, start in enumerate(onsets):
        boundary = onsets[index + 1] if index + 1 < len(onsets) else total
        note = _measure(audio, sample_rate, start, boundary, envelope, floor)
        if note is None:
            continue
        notes.append(note)

    if split_on_pitch:
        notes = _split_legato(audio, sample_rate, notes, envelope, floor)
    return notes


def _measure(audio, sample_rate, start, boundary, envelope, floor) -> Note | None:
    first = int(start * sample_rate)
    last = int(boundary * sample_rate)
    if last - first < int(MIN_NOTE_S * sample_rate):
        return None
    segment_audio = audio[first:last]

    attack = segment_audio[: int(0.05 * sample_rate)]
    peak = float(np.max(np.abs(attack))) if len(attack) else 0.0
    peak_db = 20 * math.log10(max(peak, 1e-6))
    # Something this far down is the noise floor twitching, not a note.
    if peak_db < floor + 12:
        return None

    # The end: where the envelope has fallen END_DROP_DB below its peak, or
    # hits the floor, whichever comes first - not merely the next onset.
    hop_first = first // HOP
    hop_last = max(hop_first + 1, last // HOP)
    local = envelope[hop_first:hop_last]
    if len(local) == 0:
        return None
    top = float(local.max())
    cutoff = max(top - END_DROP_DB, floor + 6)
    below = np.nonzero(local[int(np.argmax(local)):] < cutoff)[0]
    end_hop = hop_first + int(np.argmax(local)) + (int(below[0]) if len(below) else len(local))
    end = min(boundary, end_hop * HOP / sample_rate)
    if end - start < MIN_NOTE_S:
        end = min(boundary, start + MIN_NOTE_S)

    hz, clarity = _settled_pitch(segment_audio, sample_rate)
    note = Note(start=round(start, 4), end=round(end, 4), hz=hz, clarity=clarity)
    if hz > 0:
        note.midi = hz_to_midi(hz)
    note.peak_db = round(peak_db, 2)
    body = audio[first : int(end * sample_rate)]
    note.rms_db = round(20 * math.log10(max(float(np.sqrt(np.mean(body * body))), 1e-6)), 2)

    # Decay: a straight line through the envelope in dB, from just after the
    # peak to the end. A plucked string is close enough to exponential that
    # the slope is a fair summary; the residual is not worth keeping.
    peak_hop = int(np.argmax(local))
    tail = local[peak_hop : end_hop - hop_first]
    tail = tail[tail > floor + 3]
    if len(tail) >= 6:
        times = np.arange(len(tail)) * HOP / sample_rate
        slope = float(np.polyfit(times, tail, 1)[0])
        note.decay_db_s = round(min(slope, 0.0), 2)

    sustain = segment_audio[int(0.06 * sample_rate) : int(0.3 * sample_rate)]
    note.centroid_hz = round(_centroid(sustain, sample_rate), 1)
    note.attack_centroid_hz = round(_centroid(segment_audio[: int(0.03 * sample_rate)], sample_rate), 1)

    if peak > 0.98:
        note.flags.append("clipped")
    if not note.pitched:
        note.flags.append("unpitched")
    return note


def _split_legato(audio, sample_rate, notes, envelope, floor) -> list[Note]:
    """Split notes whose pitch moves a semitone or more without a new attack."""
    window = int(0.046 * sample_rate)
    hop = window // 2
    result: list[Note] = []
    for note in notes:
        if not note.pitched or note.duration < 0.35:
            result.append(note)
            continue
        first = int(note.start * sample_rate)
        last = int(note.end * sample_rate)
        reference = note.midi
        split_at = None
        for start in range(first + int(PITCH_TO_S * sample_rate), last - window, hop):
            hz, clarity = _estimate_f0(audio[start : start + window], sample_rate)
            if clarity < 0.75 or not (MIN_F0 <= hz <= MAX_F0):
                continue
            if abs(hz_to_midi(hz) - reference) >= PITCH_SPLIT_SEMITONES:
                # Confirm on the next window, so one octave error is not a note.
                hz2, c2 = _estimate_f0(audio[start + hop : start + hop + window], sample_rate)
                if c2 >= 0.75 and hz2 > 0 and abs(hz_to_midi(hz2) - hz_to_midi(hz)) < 0.5:
                    split_at = start / sample_rate
                    break
        if split_at is None:
            result.append(note)
            continue
        head = _measure(audio, sample_rate, note.start, split_at, envelope, floor - 30)
        tail = _measure(audio, sample_rate, split_at, note.end, envelope, floor - 30)
        for piece in (head, tail):
            if piece is not None:
                if piece is tail:
                    piece.flags.append("legato")
                result.append(piece)
        if head is None and tail is None:
            result.append(note)
    return result


def summarise(notes: list[Note]) -> dict:
    """What a set of notes says about the instrument and the playing."""
    pitched = [n for n in notes if n.pitched]
    if not notes:
        return {"notes": 0}
    cents = [n.cents for n in pitched]
    return {
        "notes": len(notes),
        "pitched": len(pitched),
        "range": [midi_to_note(min(n.midi for n in pitched)), midi_to_note(max(n.midi for n in pitched))] if pitched else None,
        "median_cents": round(float(np.median(cents)), 1) if cents else None,
        "peak_db": {
            "median": round(float(np.median([n.peak_db for n in notes])), 1),
            "max": round(float(max(n.peak_db for n in notes)), 1),
            "min": round(float(min(n.peak_db for n in notes)), 1),
        },
        "clipped": sum(1 for n in notes if "clipped" in n.flags),
        "unpitched": len(notes) - len(pitched),
    }

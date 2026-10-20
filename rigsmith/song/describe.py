"""What a part sounds like, in the terms a rig is built from.

Not a classifier with a confident label, and not a substitute for listening -
a set of measurements, each mapped onto the knob or block it most suggests,
with the number left visible so the mapping can be disagreed with:

* **gain** - how hard the amp is driven, from how flat the spectrum is
  between the harmonics, how little the peaks stand above the average, and
  how much energy sits up where distortion puts it;
* **voicing** - brightness, the mid scoop or hump, how much low end and how
  tight it is;
* **dynamics** - how compressed it is;
* **space** - a delay and its time against the tempo (a dotted eighth is a
  very particular decision), how long notes take to die into the room;
* **movement** - chorus or flanger wobble, tremolo.

A separated stem carries the record's mix bus, mastering limiter and Demucs's
artefacts as well as the amp. The dynamics figures in particular describe
the master as much as the guitar, and the summary says so.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

#: Where an amp's controls act, roughly: a mid knob centres somewhere
#: between 500 and 800 Hz, so the mid band is drawn around that rather than
#: at a textbook octave boundary - drawn at 600-1600 it missed a 500 Hz scoop
#: entirely.
BANDS = (
    ("low", 80, 200),
    ("low-mid", 200, 400),
    ("mid", 400, 1200),
    ("high-mid", 1200, 3500),
    ("presence", 3500, 8000),
)


@dataclass
class ToneSketch:
    gain: float = 0.0
    """0 clean - 1 saturated."""
    gain_label: str = ""
    centroid_hz: float = 0.0
    tilt_db_per_octave: float = 0.0
    bands_db: dict[str, float] = field(default_factory=dict)
    """Each band's level relative to the mean of all five."""
    mid_shape: str = ""
    low_end: str = ""
    crest_db: float = 0.0
    dynamic_range_db: float = 0.0
    compression: str = ""
    delay_ms: float | None = None
    delay_note: str | None = None
    delay_strength: float = 0.0
    decay_ms: float | None = None
    space: str = ""
    modulation_hz: float | None = None
    modulation: str = ""
    modulation_depth: float | None = None
    """A tremolo's depth, 0-1: how far the level dips each cycle (1 is to silence)."""
    wobble_cents: float | None = None
    """How far sustained notes wander in pitch, peak, after bends are
    removed - a few cents for a chorus, tens for vibrato, ~0 for neither."""
    wobble_share: float | None = None
    """The share of notes that wobble - all of them for an effect, some of
    them for a player's hand."""
    gated: bool = False
    width: float | None = None
    suggestions: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)

    def summary(self) -> str:
        bits = [self.gain_label, self.mid_shape, self.low_end]
        if self.delay_ms:
            bits.append(f"{self.delay_ms:.0f} ms delay" + (f" ({self.delay_note})" if self.delay_note else ""))
        if self.space:
            bits.append(self.space)
        if self.modulation:
            bits.append(self.modulation)
        if self.gated:
            bits.append("gated")
        return ", ".join(b for b in bits if b)

    def describe(self) -> str:
        lines = [self.summary()]
        lines.append(
            f"  gain {self.gain:.2f}, brightness {self.centroid_hz:.0f} Hz ({self.tilt_db_per_octave:+.1f} dB/oct), "
            f"crest {self.crest_db:.1f} dB"
        )
        lines.append("  bands: " + ", ".join(f"{k} {v:+.1f}" for k, v in self.bands_db.items()))
        lines.extend(f"  try: {s}" for s in self.suggestions)
        lines.extend(f"  note: {c}" for c in self.caveats)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["summary"] = self.summary()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "ToneSketch":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


def _mono(audio):
    audio = np.asarray(audio, dtype=np.float64)
    return audio.mean(axis=1) if audio.ndim > 1 else audio


def _spectrum(audio, sample_rate, n_fft=8192):
    hop = n_fft // 2
    window = np.hanning(n_fft)
    frames = []
    loudness = []
    for start in range(0, max(1, len(audio) - n_fft), hop):
        chunk = audio[start : start + n_fft]
        if len(chunk) < n_fft:
            break
        mag = np.abs(np.fft.rfft(chunk * window))
        frames.append(mag)
        loudness.append(float(np.sum(mag ** 2)))
    if not frames:
        return np.zeros(n_fft // 2 + 1), np.fft.rfftfreq(n_fft, 1 / sample_rate), np.zeros((0, n_fft // 2 + 1))
    frames = np.array(frames)
    loudness = np.array(loudness)
    # Only frames that hold playing - a gap's spectrum is the noise floor.
    keep = loudness > loudness.max() * 10 ** (-30 / 10)
    power = (frames[keep] ** 2).mean(axis=0)
    return power, np.fft.rfftfreq(n_fft, 1 / sample_rate), frames[keep]


def _band_db(power, freqs, low, high):
    mask = (freqs >= low) & (freqs < high)
    return 10 * math.log10(max(float(power[mask].mean()) if mask.any() else 0.0, 1e-20))


def _periodicity(audio, sample_rate, *, n: int = 2048, hop: int = 1024) -> float:
    """Median normalised autocorrelation peak over the playing frames, 70 Hz-1.2 kHz.

    About 0.95 for single notes up the neck, 0.75 for a low single-note
    riff, 0.4 for chords - which is what the gain estimate needs to know,
    because each of those fills the spectrum differently when driven.
    """
    x = np.asarray(audio, dtype=np.float64)
    if len(x) < n * 2:
        return 0.0
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::hop]
    energy = (frames**2).sum(axis=1)
    frames = frames[energy > energy.max() * 10 ** (-25 / 10)]
    if not len(frames):
        return 0.0
    frames = frames - frames.mean(axis=1, keepdims=True)
    spectrum = np.fft.rfft(frames, 2 * n, axis=1)
    correlation = np.fft.irfft(np.abs(spectrum) ** 2, axis=1)[:, :n]
    zero = np.maximum(correlation[:, :1], 1e-20)
    low, high = int(sample_rate / 1200), int(sample_rate / 70)
    return float(np.median((correlation[:, low:high] / zero).max(axis=1)))


#: How much of the crest's drive evidence the flatness needs before it fully
#: counts (see :func:`_gain`).
FLATNESS_NEEDS_CREST = 0.4


def _gain(frames, freqs, crest_db, periodicity: float | None = None) -> float:
    """0-1 drive estimate.

    Two things distortion does, scaled and weighted: it flattens the peaks
    (crest factor falls from ~14 dB clean to ~6 saturated) and it fills the
    spectrum in between harmonics. How much it fills depends on what is
    played, so ``periodicity`` (:func:`_periodicity`) picks the scale:

    - single notes up the neck have harmonics too far apart to fill - 1-5 kHz
      flatness sits at ~0.06 at any drive - so crest carries the estimate
      (before, a lead through a high-gain amp could not read above 0.6);
    - chords fill it fast, from ~0.1 clean to ~0.4 driven, through
      intermodulation, so their flatness scale is wider (before, a chord
      part through a medium-gain amp read as fuzz);
    - low single-note riffs are what the original scales were set on.

    The scales were set on the simulated amp's voicings; a mastered stem's
    limiter lowers the crest of everything, which is the main reason this
    reads a little high on records.

    Flatness counts only as far as the crest agrees (``FLATNESS_NEEDS_CREST``):
    a pick's attack is broadband too, and clean chugs and arpeggios, all
    attacks, sat at 0.34-0.49 flatness with a clean crest - they read as
    edge of breakup or crunch. Across 88 synthetic parts and drives the
    range came out right 52 times against 49, within one 82 against 79.
    """
    if not len(frames):
        return 0.0
    band = (freqs >= 1000) & (freqs <= 5000)
    segment = frames[:, band] + 1e-12
    flatness = float(np.median(np.exp(np.mean(np.log(segment), axis=1)) / np.mean(segment, axis=1)))
    if periodicity is not None and periodicity >= 0.85:
        c_term = np.clip((14.0 - crest_db) / 8.5, 0, 1)
        f_term = np.clip((flatness - 0.06) / 0.1, 0, 1)
        return float(np.clip(0.9 * c_term + 0.1 * f_term, 0, 1))
    c_term = np.clip((14.0 - crest_db) / 7.0, 0, 1)
    if periodicity is not None and periodicity < 0.55:
        f_term = np.clip((flatness - 0.2) / 0.2, 0, 1)
    else:
        f_term = np.clip((flatness - 0.2) / 0.12, 0, 1)
    f_term *= min(1.0, float(c_term) / FLATNESS_NEEDS_CREST)
    return float(np.clip(0.6 * c_term + 0.4 * f_term, 0, 1))


def _gain_label(gain: float) -> str:
    if gain < 0.2:
        return "clean"
    if gain < 0.38:
        return "edge of breakup"
    if gain < 0.58:
        return "crunch"
    if gain < 0.8:
        return "high gain"
    return "saturated (fuzz or very high gain)"


def _envelope(audio, sample_rate, hop_s=0.01):
    hop = max(1, int(hop_s * sample_rate))
    usable = (len(audio) // hop) * hop
    if not usable:
        return np.zeros(0)
    blocks = audio[:usable].reshape(-1, hop)
    return np.sqrt(np.mean(blocks * blocks, axis=1))


def power_cepstrum(audio, sample_rate: int, *, size: int = 1 << 17) -> np.ndarray | None:
    """The power cepstrum of the 150 Hz-5 kHz band, averaged over windows of
    ``size`` samples - where an echo shows as a peak at its delay."""
    from scipy.signal import butter, sosfiltfilt

    # Too short for the window - or for the filter, which needs some
    # samples either side and raised on a near-empty stem.
    if len(audio) < size // 2:
        return None
    sos = butter(4, [150.0, min(5000.0, 0.45 * sample_rate)], btype="bandpass", fs=sample_rate, output="sos")
    signal = sosfiltfilt(sos, audio)
    window = np.hanning(size)
    total = np.zeros(size)
    count = 0
    for start in range(0, max(1, len(signal) - size // 2), size // 2):
        chunk = signal[start : start + size]
        if len(chunk) < size:
            chunk = np.pad(chunk, (0, size - len(chunk)))
        if float(np.sqrt(np.mean(chunk ** 2))) < 1e-5:
            continue
        spectrum = np.abs(np.fft.rfft(chunk * window)) ** 2
        total += np.fft.irfft(np.log(spectrum + 1e-12), size)
        count += 1
    return total / count if count else None


def _delay(audio, sample_rate, tempo) -> tuple[float | None, float, str | None]:
    """An echo's time, from the power cepstrum.

    The obvious approach - autocorrelate the attacks - hears the riff: eighth
    notes at 120 BPM put a peak at 250 ms whether or not there is a delay.
    The difference between an echo and a riff is that an echo is a *copy* of
    the waveform, phase and all, while a re-picked note is a new one. A copy
    at lag L ripples the log spectrum with period 1/L, which is a peak at
    quefrency L in the cepstrum; re-picked notes, which do not share phase,
    put nothing there. So the cepstrum, averaged over windows, is searched
    from 80 ms to a second for a peak standing well clear of its surroundings.
    """
    from scipy.ndimage import median_filter

    cepstrum = power_cepstrum(audio, sample_rate)
    if cepstrum is None:
        return None, 0.0, None
    size = len(cepstrum)
    lo, hi = int(0.08 * sample_rate), min(size // 2, int(1.0 * sample_rate))
    region = cepstrum[lo:hi]
    baseline = median_filter(region, size=int(0.05 * sample_rate) | 1)
    residual = region - baseline
    spread = float(np.median(np.abs(residual))) or 1e-12
    z_all = residual / spread
    # Measured on synthetic parts, a real echo stands an order of magnitude
    # clear of everything else in the cepstrum (z near 700 at the echo, 70 at
    # its first feedback repeat) while the impostors sit at 40-50: a pitch's
    # rahmonics (85 ms is seven periods of a low E), and a part's own note
    # spacing when it is steady (notes every 500 ms modulate the spectrum the
    # way a 500 ms echo would). So a lag is accepted only if its peak is very
    # strong, or clearly raised *and* repeated at twice the lag - which is
    # what feedback does and rhythm mostly does not. Rahmonics and the note
    # spacing are ruled out by name unless the peak is very strong.
    # Pitch periods and note spacing cost a pitch analysis and a note
    # segmentation; most parts have no candidate echo at all, so they are
    # only worked out if one turns up.
    cache: dict[str, object] = {}

    def periods() -> list[float]:
        if "periods" not in cache:
            cache["periods"] = _pitch_periods_ms(audio, sample_rate)
        return cache["periods"]  # type: ignore[return-value]

    def spacing() -> float | None:
        if "spacing" not in cache:
            cache["spacing"] = _note_spacing_ms(audio, sample_rate)
        return cache["spacing"]  # type: ignore[return-value]

    def z_at(milliseconds: float) -> float:
        index = int(milliseconds / 1000.0 * sample_rate) - lo
        if index < 0 or index >= len(z_all):
            return 0.0
        return float(z_all[max(0, index - 24) : index + 25].max())

    lag = None
    z = 0.0
    for index in np.argsort(z_all)[::-1][:24]:
        candidate_z = float(z_all[index])
        if candidate_z < 40:
            break
        candidate = (lo + int(index)) / sample_rate * 1000.0
        strong = candidate_z >= 150
        repeated = z_at(2 * candidate) >= 15
        if not (strong or repeated):
            continue
        # Under 120 ms the cepstrum is crowded with the rahmonics of low
        # notes, so a slapback has to be both strong and repeated.
        if candidate < 120 and not (strong and repeated):
            continue
        if not strong:
            rahmonic = any(
                abs(candidate / period - round(candidate / period)) < 0.012 and round(candidate / period) <= 16
                for period in periods()
            )
            gap = spacing()
            rhythmic = gap is not None and any(
                abs(candidate - gap * k) / (gap * k) < 0.04 for k in (0.5, 1, 2, 3, 4)
            )
            if rahmonic or rhythmic:
                continue
        lag, z = candidate, candidate_z
        break
    if lag is None:
        return None, 0.0, None
    return round(lag, 1), round(z, 1), delay_note(lag, tempo)


def delay_note(lag_ms: float | None, tempo: float | None) -> str | None:
    """The note value an echo's time is at a tempo, within 3%, or None."""
    if not lag_ms or not tempo or tempo <= 0:
        return None
    beat = 60000.0 / tempo
    for name, fraction in (("quarter", 1.0), ("dotted eighth", 0.75), ("eighth", 0.5), ("triplet quarter", 2 / 3), ("dotted quarter", 1.5), ("sixteenth", 0.25), ("half", 2.0)):
        if abs(lag_ms - beat * fraction) / (beat * fraction) < 0.03:
            return name
    return None


def retime(data: dict, tempo: float | None) -> dict:
    """A stored sketch with its tempo-relative readings worked out again.

    Only the delay's note value depends on the tempo; the measurements
    themselves do not, so a corrected tempo needs no re-analysis.
    """
    sketch = ToneSketch.from_dict(data)
    sketch.delay_note = delay_note(sketch.delay_ms, tempo)
    sketch.suggestions = _suggestions(sketch)
    return {**data, **sketch.to_dict()}


def _pitch_periods_ms(audio, sample_rate) -> list[float]:
    """Periods of the most common pitches, from a slice of the busiest part."""
    from ..learn.instrument import midi_to_hz
    from ..learn.transcribe import _spectrogram, salience_frames

    span = min(len(audio), 10 * sample_rate)
    frames = salience_frames(_spectrogram(audio[:span], sample_rate), sample_rate, min_midi=33, max_polyphony=3)
    counts: dict[int, int] = {}
    for found in frames:
        for pitch, _ in found:
            counts[pitch] = counts.get(pitch, 0) + 1
    common = sorted(counts, key=counts.get, reverse=True)[:4]
    return [1000.0 / midi_to_hz(p) for p in common]


def _note_spacing_ms(audio, sample_rate) -> float | None:
    """The playing's own note spacing - from the played notes, not the echoes.

    An echo makes onsets too, so spacing measured over every onset of a
    delayed part comes out at a fraction of the echo time, and the echo then
    looks like rhythm. Echoes are quieter copies; only notes within 6 dB of
    the loud ones count - the loudest level a fifth of the notes reach, not
    the loudest note: a clean intro whose last half-second was the distorted
    verse coming in measured every one of its own notes 20 dB under that, had
    no spacing, and its notes every 500 ms passed for a quarter-note delay.
    """
    from ..learn.segment import segment

    notes = segment(audio[: 30 * sample_rate], sample_rate, split_on_pitch=False)
    if len(notes) < 6:
        return None
    peaks = sorted((n.peak_db for n in notes), reverse=True)
    loudest = next((p for p in peaks if sum(1 for q in peaks if p - 6 <= q <= p) >= 0.2 * len(peaks)), peaks[len(peaks) // 2])
    played = [n.start for n in notes if n.peak_db >= loudest - 6]
    if len(played) < 4:
        return None
    gaps = np.diff(played)
    gaps = gaps[gaps > 0.05]
    return float(np.median(gaps) * 1000.0) if len(gaps) else None


#: How far under the note the playing has to fall, 40 ms after a stop, for
#: the stop to count. Within this, the note is still sounding or the next
#: one has started: between the chords of a part that never rests, a 6 dB
#: dip and the next chord ringing on at the note's own level read as a
#: reverb tail - "a long wash" on dry power chords, 1.4-5.3 s.
STILL_PLAYING_DB = 4.0


def _decay(audio, sample_rate) -> float | None:
    """How long the room takes to fall 20 dB once playing stops, in ms; 0 for dry.

    Measured where loud playing stops - a fall of 6 dB within 30 ms - and
    only where nothing new starts for at least 80 ms after: 40 ms past the
    stop, a dry part has fallen 30 dB or more below the note, and a
    reverberant one is still there and falling at the room's rate, whose
    slope is the decay time. Three such stops are needed; a part that never
    pauses reads as unknown (None) rather than as a guess.

    The note's level is the reference, not the part's noise floor: a long
    reverb fills the gaps and raises the floor, which made reverberant parts
    read as dry. The old measure timed any 20 dB fall with no new attack, so
    a dry lead's own string decay read as a plate and every reverb longer
    than the gap between notes read as nothing. On synthetic parts (a lead,
    held chords and an eighth-note riff, with a two-beat rest each phrase)
    and exponential reverbs of 0.3-4 s, this classifies 19 of 20 rooms
    right; the other is unknown. Times read a little short on long tails.
    """
    from scipy.signal import butter, sosfiltfilt

    audio = np.asarray(audio, dtype=np.float64)
    hop = int(0.005 * sample_rate)
    if len(audio) < 200 * hop:
        return None
    # The guitar's body, away from low strings that sustain and fizz.
    sos = butter(4, [300.0, min(5000.0, 0.45 * sample_rate)], btype="bandpass", fs=sample_rate, output="sos")
    filtered = sosfiltfilt(sos, audio)
    frames = len(filtered) // hop
    env = np.sqrt(np.mean(filtered[: frames * hop].reshape(frames, hop) ** 2, axis=1))
    db = np.convolve(20 * np.log10(np.maximum(env, 1e-9)), np.ones(3) / 3, mode="same")
    peak = float(np.percentile(db, 99))
    rt60s: list[float] = []
    dry = usable = 0
    i = 3
    while i < len(db) - 30:
        if db[i - 3] - db[i + 3] >= 6 and db[i - 3] > peak - 25:
            reference = float(np.max(db[max(0, i - 12) : i]))
            start = i + 8
            end = start
            while end < min(len(db) - 3, start + 120) and db[end + 3] - db[end] < 4:
                end += 1
            tail = db[start:end]
            if end - start >= 16 and float(np.mean(tail[:4])) - reference < -STILL_PLAYING_DB:
                usable += 1
                if float(np.mean(tail[:4])) - reference < -30:
                    dry += 1
                else:
                    slope = float(np.polyfit(np.arange(len(tail)) * 0.005, tail, 1)[0])
                    if slope < -2:
                        rt60s.append(60.0 / -slope)
            i = max(end, i + 1)
            continue
        i += 1
    if usable < 3:
        return None
    if dry >= 0.6 * usable or not rt60s:
        return 0.0
    # RT60 is the time to fall 60 dB; the space bands are set in 20 dB.
    return round(float(np.median(rt60s)) * 1000.0 / 3.0, 1)


def _modulation(audio, sample_rate, *, note_rate_hz=None) -> tuple[float | None, str, float | None]:
    """A steady wobble in level, 2-10 Hz, inside sustained sound: (rate, label, depth).

    Measured only within stretches that stay loud - each one detrended for
    its own decay - because the gaps between notes put the rhythm's pulse
    and every harmonic of it into a whole-part envelope, and a part with a
    note every 1.6 s read as a 2.5 Hz tremolo. What is left must also not sit
    on the playing's own pulse (or a small multiple of it), and must be deep
    enough to hear.

    "Loud" is judged against the sound's own neighbourhood - within 25 dB
    of the loudest moment in the surrounding 0.45 s - rather than against
    the part as a whole. Judged against the whole part, a deep tremolo's
    troughs fell out of every stretch and broke it into pieces too short to
    measure, so the choppiest tremolos (depth 0.7 and up) were never heard
    at all. A gap between notes is still far below its neighbourhood and
    still ends a stretch. The depth is the level swing as a tremolo pedal's
    0-1 depth: on synthetic parts it reads 0.32, 0.50, 0.69 and 0.88 for 0.3,
    0.5, 0.7 and 0.9, clean or driven.
    """
    from scipy.ndimage import maximum_filter1d

    hop_s = 0.01
    env = _envelope(audio, sample_rate, hop_s)
    if len(env) < 300:
        return None, "", None
    db = 20 * np.log10(np.maximum(env, 1e-9))
    held = maximum_filter1d(db, size=int(0.45 / hop_s))
    loud = (db > held - 25) & (held > max(np.percentile(held, 40), held.max() - 30))
    edges = np.diff(np.concatenate([[0], loud.astype(int), [0]]))
    starts, ends = np.where(edges == 1)[0], np.where(edges == -1)[0]
    pad = int(8 / hop_s)
    rates = np.fft.rfftfreq(pad, hop_s)
    spectra, depths, weights = [], [], []
    for a, b in zip(starts, ends):
        if b - a < int(0.6 / hop_s):
            continue
        t = np.arange(b - a) * hop_s
        level = db[a:b] - np.polyval(np.polyfit(t, db[a:b], 2), t)
        power = np.abs(np.fft.rfft(level * np.hanning(len(level)), n=pad)) ** 2
        spectra.append(power / max(power.sum(), 1e-20) * len(level))
        depths.append(float(np.sqrt(2 * np.mean(level**2))))
        weights.append(len(level))
    if not spectra or sum(weights) * hop_s < 1.5:
        return None, "", None
    total = np.sum(spectra, axis=0)
    band = (rates >= 2.0) & (rates <= 10.0)
    peak = int(np.where(band)[0][np.argmax(total[band])])
    near = band & (np.abs(rates - rates[peak]) <= 0.5)
    concentration = float(total[near].sum() / max(total[(rates >= 0.5) & (rates <= 12)].sum(), 1e-20))
    depth_db = float(np.average(depths, weights=weights))
    if concentration < 0.35 or depth_db < 1.0:
        return None, "", None
    rate = float(rates[peak])
    if callable(note_rate_hz):
        note_rate_hz = note_rate_hz()
    if note_rate_hz:
        for multiple in (0.5, 1.0, 2.0, 3.0, 4.0):
            if abs(rate - note_rate_hz * multiple) / (note_rate_hz * multiple) < 0.08:
                return None, "", None
    # depth_db is the swing's amplitude, half its peak-to-peak; a gain that
    # dips to (1 - depth) swings 20*log10(1 / (1 - depth)) dB peak to peak.
    depth = 1.0 - 10 ** (-depth_db / 10.0)
    return round(rate, 2), "tremolo", round(float(min(max(depth, 0.0), 1.0)), 2)


def _princarg(phase):
    return (phase + np.pi) % (2 * np.pi) - np.pi


def _pitch_wobble(audio, sample_rate, *, n_fft: int = 2048, hop: int = 256) -> dict | None:
    """How sustained notes move in pitch: what a chorus or vibrato does.

    Level cannot see a chorus - it moves pitch and phase. So each note
    (from the onset segmenter) gets one partial, chosen from the note's
    time-averaged spectrum - per-frame tracking fails, because a chorus's
    moving comb makes the loudest partial jump between harmonics - and that
    partial's instantaneous frequency is read from the phase vocoder,
    magnitude-weighted over its neighbouring bins. Moments where the dry and
    delayed copies cancel spike it, so those are interpolated over rather
    than counted. A straight line through each note removes bends and drift;
    what is left is the wobble, and its spectrum gives a rate.

    On a synthetic part: no effect reads 0.1-0.4 cents, a chorus 4-27,
    vibrato ~30; rates of 1.5 Hz and up come out right, slower ones cannot be
    resolved inside a single note and are reported as slow.
    """
    from ..learn.segment import segment

    x = np.asarray(audio, dtype=np.float64)
    count = 1 + (len(x) - n_fft) // hop
    if count < 40:
        return None
    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop][:count]
    freqs = np.fft.rfftfreq(n_fft, 1 / sample_rate)
    low, high = np.searchsorted(freqs, 150), np.searchsorted(freqs, 2500)
    # Only the bins a note's partial can be chosen from (plus the two either
    # side it is weighted over) need phase - a tenth of the spectrum.
    first, last = max(low - 2, 0), high + 3
    spec = np.fft.rfft(frames * np.hanning(n_fft)[None, :], axis=1)[:, first:last]
    magnitude, phase = np.abs(spec), np.angle(spec)
    low, high = low - first, high - first
    frame_s = hop / sample_rate
    bins = np.arange(first, last)
    advance = 2 * np.pi * bins * hop / n_fft
    deviation = _princarg(np.diff(phase, axis=0) - advance[None, :])
    instantaneous = (bins[None, :] + deviation * n_fft / (2 * np.pi * hop)) * sample_rate / n_fft

    depths, weights, spectra = [], [], []
    pad = int(8 / frame_s)
    rates = np.fft.rfftfreq(pad, frame_s)
    for note in segment(x, sample_rate):
        start = int((note.start + 0.08) / frame_s) + 1  # past the attack
        end = min(count, int(note.end / frame_s))
        if end - start < int(0.45 / frame_s):
            continue
        k = low + int(np.argmax(magnitude[start:end, low:high].mean(axis=0)))
        around = slice(max(k - 2, 0), min(k + 3, magnitude.shape[1]))
        weight = magnitude[start:end, around] ** 2
        f = (instantaneous[start - 1 : end - 1, around] * weight).sum(axis=1) / np.maximum(weight.sum(axis=1), 1e-20)
        valid = f > 0
        if valid.mean() < 0.7:
            continue
        cents = np.zeros(len(f))
        cents[valid] = 1200 * np.log2(f[valid] / np.median(f[valid]))
        level = magnitude[start:end, k]
        bad = ~valid | (np.abs(cents) > 50) | (level < level.max() * 10 ** (-30 / 20))
        if bad.mean() > 0.3:
            continue
        t = np.arange(len(cents)) * frame_s
        cents = np.interp(t, t[~bad], cents[~bad])
        cents = cents - np.polyval(np.polyfit(t, cents, 1), t)
        width = max(1, int(1 / 24 / frame_s))
        smooth = np.convolve(cents, np.ones(width) / width, mode="same")
        depths.append(float(np.sqrt(2 * np.mean(smooth**2))))
        power = np.abs(np.fft.rfft(smooth * np.hanning(len(smooth)), n=pad)) ** 2
        spectra.append(power / max(power.sum(), 1e-20) * len(smooth))
        weights.append(len(smooth))
    if len(depths) < 2:
        return None
    total = np.sum(spectra, axis=0)
    band = (rates >= 0.2) & (rates <= 9)
    peak = int(np.where(band)[0][np.argmax(total[band])])
    near = band & (np.abs(rates - rates[peak]) <= 0.6)
    return {
        "depth": float(np.average(depths, weights=weights)),
        "rate": float(rates[peak]),
        "concentration": float(total[near].sum() / max(total[band].sum(), 1e-20)),
        "share": float(np.mean(np.array(depths) >= WOBBLE_CENTS)),
        "notes": len(depths),
    }


#: Below this the pitch is steady. An unaffected part reads under half a cent.
WOBBLE_CENTS = 3.0


def _pitch_modulation(audio, sample_rate) -> tuple[float | None, str, float | None, float | None]:
    """(rate, label, depth, share) for chorus or vibrato, from :func:`_pitch_wobble`."""
    found = _pitch_wobble(audio, sample_rate)
    if not found or found["depth"] < WOBBLE_CENTS:
        return None, "", (round(found["depth"], 1) if found else None), (round(found["share"], 2) if found else None)
    depth, share = round(found["depth"], 1), round(found["share"], 2)
    if found["rate"] >= 4.0 and found["concentration"] >= 0.5 and depth >= 12:
        label = "vibrato" if share >= 0.7 else "vibrato on some notes (likely the player's hand)"
        return round(found["rate"], 2), label, depth, share
    rate = round(found["rate"], 2) if found["rate"] <= 3.5 and found["concentration"] >= 0.3 else None
    return rate, "chorus" if share >= 0.5 else "a chorus-like wobble on some notes", depth, share


def sketch(audio, sample_rate: int, *, tempo: float | None = None, from_stem: bool = True, pitch: bool = True) -> ToneSketch:
    """Measure a part and suggest where to start building it.

    ``pitch`` False skips looking for chorus and vibrato - for audio known to
    be two performances summed (the centre of a double-tracked part), which
    beat against each other exactly like a chorus.
    """
    stereo = np.asarray(audio, dtype=np.float64)
    audio = _mono(stereo)
    result = ToneSketch()
    if len(audio) < sample_rate:
        result.caveats.append("under a second of audio - too little to describe")
        return result

    power, freqs, frames = _spectrum(audio, sample_rate)
    total = float(power.sum()) or 1e-12
    result.centroid_hz = round(float((freqs * power).sum() / total), 1)
    bands = {name: _band_db(power, freqs, lo, hi) for name, lo, hi in BANDS}
    mean = float(np.mean(list(bands.values())))
    result.bands_db = {k: round(v - mean, 1) for k, v in bands.items()}
    octaves = np.log2(np.array([math.sqrt(lo * hi) for _, lo, hi in BANDS]))
    slope = float(np.polyfit(octaves, np.array(list(bands.values())), 1)[0])
    result.tilt_db_per_octave = round(slope, 2)

    body = audio[np.abs(audio) > np.max(np.abs(audio)) * 10 ** (-40 / 20)]
    rms = float(np.sqrt(np.mean(body ** 2))) if len(body) else 1e-12
    result.crest_db = round(20 * math.log10(max(float(np.max(np.abs(audio))), 1e-12) / max(rms, 1e-12)), 1)
    env_db = 20 * np.log10(np.maximum(_envelope(audio, sample_rate, 0.05), 1e-9))
    active = env_db[env_db > env_db.max() - 40]
    result.dynamic_range_db = round(float(np.percentile(active, 95) - np.percentile(active, 10)), 1) if len(active) else 0.0
    result.compression = (
        "heavily compressed" if result.dynamic_range_db < 6 else "compressed" if result.dynamic_range_db < 12 else "dynamic"
    )

    result.gain = round(_gain(frames, freqs, result.crest_db, _periodicity(audio, sample_rate)), 3)
    result.gain_label = _gain_label(result.gain)

    mid = result.bands_db["mid"]
    around = (result.bands_db["low-mid"] + result.bands_db["high-mid"]) / 2
    if mid < around - 4:
        result.mid_shape = "scooped mids"
    elif mid > around + 3:
        result.mid_shape = "mid-forward"
    else:
        result.mid_shape = "even mids"
    low = result.bands_db["low"]
    result.low_end = "heavy low end" if low > 4 else "lean low end" if low < -4 else ""

    result.delay_ms, result.delay_strength, result.delay_note = _delay(audio, sample_rate, tempo)
    result.decay_ms = _decay(audio, sample_rate)
    if result.decay_ms is not None:
        result.space = "dry" if result.decay_ms < 120 else "a room" if result.decay_ms < 350 else "a plate or hall" if result.decay_ms < 900 else "a long wash"
    def note_rate() -> float | None:
        spacing = _note_spacing_ms(audio, sample_rate)
        return (1000.0 / spacing) if spacing else None

    result.modulation_hz, result.modulation, result.modulation_depth = _modulation(audio, sample_rate, note_rate_hz=note_rate)
    if pitch and not result.modulation and result.gain < 0.85:
        # Pitch movement is only looked for under tremolo's absence, and not
        # on the most saturated parts, where distortion's intermodulation
        # makes every partial wander.
        if stereo.ndim > 1 and stereo.shape[1] == 2:
            # Each side on its own, and the steadier one decides: a chorus
            # (mono or stereo) moves both sides, while a double-tracked part
            # is steady on each side and only beats when they are summed.
            sides = [_pitch_modulation(stereo[:, c], sample_rate) for c in (0, 1)]
            rate, label, result.wobble_cents, result.wobble_share = min(sides, key=lambda side: side[2] if side[2] is not None else -1.0)
        else:
            rate, label, result.wobble_cents, result.wobble_share = _pitch_modulation(audio, sample_rate)
        if label:
            result.modulation, result.modulation_hz = label, rate
            if label.startswith("chorus") and stereo.ndim == 1:
                result.caveats.append("a double-tracked part summed to mono also wobbles like a chorus")

    # Gated: hard silence *between notes* of a driven part - not a channel
    # with nothing on it, which is just silence.
    quiet = np.abs(audio) < 1e-5
    playing = 1.0 - float(quiet.mean())
    runs = np.diff(np.concatenate([[0], quiet.astype(int), [0]]))
    lengths = np.where(runs == -1)[0] - np.where(runs == 1)[0] if (runs == 1).any() else np.zeros(0)
    gaps = int(np.sum((lengths > 0.03 * sample_rate) & (lengths < 1.5 * sample_rate))) if len(lengths) else 0
    result.gated = playing > 0.3 and gaps >= 3 and result.gain > 0.5

    if stereo.ndim > 1 and stereo.shape[1] == 2:
        side = (stereo[:, 0] - stereo[:, 1]) / 2
        mid_sig = (stereo[:, 0] + stereo[:, 1]) / 2
        result.width = round(float(np.sqrt(np.mean(side ** 2)) / max(np.sqrt(np.mean(mid_sig ** 2)), 1e-12)), 3)

    result.suggestions = _suggestions(result)
    if from_stem:
        result.caveats.append(
            "measured on a separated stem: dynamics and the top end include the "
            "record's mix bus and mastering as well as the guitar rig"
        )
    return result


def _suggestions(s: ToneSketch) -> list[str]:
    out = []
    if s.gain < 0.2:
        out.append("a clean amp, or a crunch amp with the gain low")
    elif s.gain < 0.58:
        out.append("an overdriven amp around the middle of its gain, or a clean amp pushed by a drive pedal")
    else:
        out.append("a high-gain amp; if the low end is tight, a boost or Tube Screamer in front with its drive low")
    if s.mid_shape == "scooped mids":
        out.append("mids down a few dB (or an EQ cut around 800 Hz)")
    elif s.mid_shape == "mid-forward":
        out.append("mids up, or a mid-voiced amp")
    if s.low_end == "lean low end":
        out.append("a high-pass around 100 Hz, or bass down")
    if s.delay_ms:
        out.append(f"a delay at {s.delay_ms:.0f} ms" + (f" ({s.delay_note} at the song's tempo)" if s.delay_note else ""))
    if s.space in ("a plate or hall", "a long wash"):
        out.append("a reverb with a longer tail, mix kept low")
    elif s.space == "a room":
        out.append("a short room reverb")
    if s.modulation:
        rate = f" at about {s.modulation_hz:.1f} Hz" if s.modulation_hz else ""
        if s.modulation == "tremolo" and s.modulation_depth:
            out.append(f"a tremolo{rate}, depth about {s.modulation_depth:.0%}")
        elif s.modulation.startswith("chorus"):
            out.append(f"a chorus{rate}, depth modest (the notes move about {s.wobble_cents:.0f} cents)")
        elif s.modulation.startswith("vibrato on some"):
            out.append("no vibrato block - the wobble is on some notes only, which is a hand, not a pedal")
        elif s.modulation.startswith("vibrato"):
            out.append(f"a vibrato block{rate}")
        else:
            out.append(f"a {s.modulation} block{rate}")
    if s.gated:
        out.append("a noise gate - the gaps between notes are silent, not hiss")
    return out

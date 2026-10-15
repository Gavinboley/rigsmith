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


def _gain(frames, freqs, crest_db) -> float:
    """0-1 drive estimate.

    Two things distortion does, scaled and weighted: it flattens the peaks
    (crest factor falls from ~14 dB clean to ~7 saturated) and it fills the
    spectrum in between harmonics (1-5 kHz flatness rises from ~0.2 to ~0.3).
    The scales were set on the simulated amp's voicings, from clean to its
    hottest; a mastered stem's limiter lowers the crest of everything, which
    is the main reason this reads a little high on records.
    """
    if not len(frames):
        return 0.0
    band = (freqs >= 1000) & (freqs <= 5000)
    segment = frames[:, band] + 1e-12
    flatness = float(np.median(np.exp(np.mean(np.log(segment), axis=1)) / np.mean(segment, axis=1)))
    c_term = np.clip((14.0 - crest_db) / 7.0, 0, 1)
    f_term = np.clip((flatness - 0.2) / 0.12, 0, 1)
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
    from scipy.signal import butter, sosfiltfilt

    sos = butter(4, [150.0, min(5000.0, 0.45 * sample_rate)], btype="bandpass", fs=sample_rate, output="sos")
    signal = sosfiltfilt(sos, audio)
    size = 1 << 17
    if len(signal) < size // 2:
        return None, 0.0, None
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
    if not count:
        return None, 0.0, None
    cepstrum = total / count
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
    note = None
    if tempo and tempo > 0:
        beat = 60000.0 / tempo
        for name, fraction in (("quarter", 1.0), ("dotted eighth", 0.75), ("eighth", 0.5), ("triplet quarter", 2 / 3), ("dotted quarter", 1.5), ("sixteenth", 0.25), ("half", 2.0)):
            if abs(lag - beat * fraction) / (beat * fraction) < 0.03:
                note = name
                break
    return round(lag, 1), round(z, 1), note


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
    the loudest count.
    """
    from ..learn.segment import segment

    notes = segment(audio[: 30 * sample_rate], sample_rate, split_on_pitch=False)
    if len(notes) < 6:
        return None
    loudest = max(n.peak_db for n in notes)
    played = [n.start for n in notes if n.peak_db >= loudest - 6]
    if len(played) < 4:
        return None
    gaps = np.diff(played)
    gaps = gaps[gaps > 0.05]
    return float(np.median(gaps) * 1000.0) if len(gaps) else None


def _decay(audio, sample_rate) -> float | None:
    """How long playing takes to fall 20 dB once it stops, in ms - the room."""
    env = _envelope(audio, sample_rate, 0.005)
    if len(env) < 200:
        return None
    db = 20 * np.log10(np.maximum(env, 1e-9))
    top = float(np.percentile(db, 95))
    times = []
    i = 0
    while i < len(db) - 40:
        # A release: a loud frame followed by a sustained fall with no new attack.
        if db[i] > top - 6 and db[i + 1] < db[i]:
            j = i
            while j < len(db) - 1 and db[j + 1] <= db[j] + 0.5 and db[j] > db[i] - 20:
                j += 1
            if db[j] <= db[i] - 20:
                times.append((j - i) * 5.0)
                i = j
        i += 1
    if len(times) < 3:
        return None
    return float(np.median(times))


def _modulation(audio, sample_rate, *, note_rate_hz=None) -> tuple[float | None, str]:
    """A steady wobble in level, 0.3-10 Hz - but not the playing's own pulse.

    Notes every 500 ms put a 2 Hz line in the level envelope exactly as a
    2 Hz tremolo would, so a rate at the note rate (or twice or half it) is
    taken as rhythm.
    """
    hop_s = 0.01
    env = _envelope(audio, sample_rate, hop_s)
    if len(env) < 600:
        return None, ""
    db = 20 * np.log10(np.maximum(env, 1e-9))
    loud = db > np.percentile(db, 60)
    if loud.mean() < 0.3:
        return None, ""
    signal = np.where(loud, db - db[loud].mean(), 0.0)
    spectrum = np.abs(np.fft.rfft(signal * np.hanning(len(signal))))
    freqs = np.fft.rfftfreq(len(signal), hop_s)
    # Only tremolo-speed movement is claimed. Slower wobble in the level
    # envelope is overwhelmingly chord changes and phrasing, and a chorus or
    # phaser moves pitch and phase far more than level - this cannot see it.
    band = (freqs >= 2.0) & (freqs <= 10)
    if not band.any():
        return None, ""
    index = np.where(band)[0][np.argmax(spectrum[band])]
    peak = float(spectrum[index])
    floor = float(np.median(spectrum[band])) or 1e-12
    if peak / floor < 6:
        return None, ""
    rate = float(freqs[index])
    if callable(note_rate_hz):
        note_rate_hz = note_rate_hz()
    if note_rate_hz:
        for multiple in (0.5, 1.0, 2.0, 3.0):
            if abs(rate - note_rate_hz * multiple) / (note_rate_hz * multiple) < 0.08:
                return None, ""
    return round(rate, 2), "tremolo"


def sketch(audio, sample_rate: int, *, tempo: float | None = None, from_stem: bool = True) -> ToneSketch:
    """Measure a part and suggest where to start building it."""
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

    result.gain = round(_gain(frames, freqs, result.crest_db), 3)
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

    result.modulation_hz, result.modulation = _modulation(audio, sample_rate, note_rate_hz=note_rate)

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
        out.append(f"a modulation block at about {s.modulation_hz:.1f} Hz")
    if s.gated:
        out.append("a noise gate - the gaps between notes are silent, not hiss")
    return out

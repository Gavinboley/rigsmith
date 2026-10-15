"""A model of how your guitar's notes are built, fitted from your samples.

The sampler is the most faithful way to play a note the profile has heard.
For the ones it has not - the 17th fret when the protocol stopped at 12, a
string whose samples all clipped - shifting the nearest sample a fourth would
work and would sound like it. This fills those gaps differently: it learns
*what a note on each string is made of* and builds new ones from that.

A plucked string is close to a sum of decaying harmonics plus a burst of pick
noise. So for every sample this measures, per harmonic, how loud it starts
and how fast it dies, plus how inharmonic the string is (a stiff wound string
puts its upper partials slightly sharp, which is a large part of why a low E
sounds like a guitar and not an organ). Those curves are smooth along a
string, so a note between two samples - or a little past the last one - is
an interpolation rather than an invention.

Analysis/resynthesis, not a neural network: it fits in seconds, needs no
GPU and no training data beyond the samples a profile already has. A trained
model (:mod:`.neural`) can take the same place when one exists; this is what
is always available.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .instrument import midi_to_hz

#: Partials are modelled up to this frequency, however many harmonics that
#: takes. A fixed harmonic count looked tidy and was wrong: forty harmonics of
#: a low E stop at 3.3 kHz, and a bridge pickup's DI carries as much energy
#: between 3 and 6 kHz as below 1 kHz. Past ~10 kHz it is pickup roll-off and
#: pick noise, which the noise burst covers.
MAX_PARTIAL_HZ = 10000.0

#: And never more than this many, so a 7-string's low A does not cost a
#: thousand oscillators.
MAX_HARMONICS = 160


def harmonic_count(f0: float, sample_rate: int = 48000) -> int:
    ceiling = min(MAX_PARTIAL_HZ, 0.45 * sample_rate)
    return max(1, min(MAX_HARMONICS, int(ceiling / f0)))

#: Analysis frame length in seconds, and hop.
FRAME_S = 0.05
HOP_S = 0.025


@dataclass
class NoteModel:
    """One analysed note: where each harmonic starts and how fast it goes."""

    midi: float
    string: int | None
    level_db: list[float]
    """Initial amplitude of each harmonic, dB relative to the strongest."""
    decay_db_s: list[float]
    """Decay rate of each harmonic, dB per second."""
    inharmonicity: float = 0.0
    noise_db: list[float] = field(default_factory=list)
    """Pick noise per octave band (125 Hz .. 16 kHz), dB relative."""
    peak_db: float = -12.0

    def to_dict(self) -> dict:
        return {
            "midi": self.midi,
            "string": self.string,
            "level_db": self.level_db,
            "decay_db_s": self.decay_db_s,
            "inharmonicity": self.inharmonicity,
            "noise_db": self.noise_db,
            "peak_db": self.peak_db,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NoteModel":
        return cls(**data)


NOISE_BANDS = (125, 250, 500, 1000, 2000, 4000, 8000, 16000)


def analyse_note(audio: np.ndarray, sample_rate: int, midi: float, *, string: int | None = None) -> NoteModel | None:
    """Fit harmonic levels, decay rates and inharmonicity to one note."""
    audio = np.asarray(audio, dtype=np.float64)
    f0 = midi_to_hz(midi)
    frame = int(FRAME_S * sample_rate)
    hop = int(HOP_S * sample_rate)
    if len(audio) < frame * 3:
        return None
    peak = float(np.max(np.abs(audio)))
    if peak <= 0:
        return None
    count = harmonic_count(f0, sample_rate)
    if count < 2:
        return None

    # Inharmonicity from a long window of the sustain: find each partial's
    # true frequency and fit f_k = k f0 sqrt(1 + B k^2).
    inharmonicity = _fit_inharmonicity(audio, sample_rate, f0, count)
    ks = np.arange(1, count + 1)
    partials = ks * f0 * np.sqrt(1 + inharmonicity * ks * ks)

    # Track each harmonic's amplitude through time by projecting each frame
    # onto a complex exponential at the partial frequency.
    window = np.hanning(frame)
    starts = range(int(0.01 * sample_rate), len(audio) - frame, hop)
    times, tracks = [], []
    t_local = np.arange(frame) / sample_rate
    basis = np.exp(-2j * np.pi * np.outer(partials, t_local)) * window
    norm = window.sum() / 2
    for start in starts:
        chunk = audio[start : start + frame]
        amplitudes = np.abs(basis @ chunk) / norm
        tracks.append(amplitudes)
        times.append(start / sample_rate)
    if len(tracks) < 3:
        return None
    tracks = np.array(tracks)
    times = np.array(times)
    db = 20 * np.log10(np.maximum(tracks, 1e-9))

    levels, decays = [], []
    for k in range(count):
        series = db[:, k]
        top = float(series.max())
        usable = series > top - 50
        if usable.sum() >= 3:
            slope, intercept = np.polyfit(times[usable], series[usable], 1)
        else:
            slope, intercept = -60.0, top
        levels.append(float(intercept))
        decays.append(float(min(slope, -0.5)))
    strongest = max(levels)
    levels = [round(l - strongest, 2) for l in levels]

    # Pick noise: what is left of the first 15 ms once the harmonics are out.
    attack = audio[: int(0.015 * sample_rate)]
    noise_db = _attack_noise(attack, sample_rate, partials)

    return NoteModel(
        midi=float(midi),
        string=string,
        level_db=levels,
        decay_db_s=[round(d, 2) for d in decays],
        inharmonicity=float(inharmonicity),
        noise_db=noise_db,
        peak_db=round(20 * math.log10(peak), 2),
    )


def _fit_inharmonicity(audio, sample_rate, f0, count) -> float:
    start = int(0.08 * sample_rate)
    length = min(len(audio) - start, int(0.6 * sample_rate))
    if length < sample_rate // 10:
        return 0.0
    n_fft = 1 << int(math.ceil(math.log2(length * 2)))
    spectrum = np.abs(np.fft.rfft(audio[start : start + length] * np.hanning(length), n_fft))
    bin_hz = sample_rate / n_fft
    ks, ratios = [], []
    for k in range(2, min(count, 25) + 1):
        expected = k * f0
        span = max(2, int(0.03 * expected / bin_hz))
        centre = int(round(expected / bin_hz))
        low, high = max(1, centre - span), min(len(spectrum) - 2, centre + span)
        if high <= low:
            continue
        index = low + int(np.argmax(spectrum[low:high]))
        if spectrum[index] < spectrum.max() * 1e-3:
            continue
        a, b, c = spectrum[index - 1 : index + 2]
        offset = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) else 0.0
        measured = (index + offset) * bin_hz
        ratio = (measured / (k * f0)) ** 2 - 1
        ks.append(k * k)
        ratios.append(ratio)
    if len(ks) < 4:
        return 0.0
    # ratio = B k^2, least squares through the origin.
    ks, ratios = np.array(ks, dtype=float), np.array(ratios)
    estimate = float((ks * ratios).sum() / (ks * ks).sum())
    return float(np.clip(estimate, 0.0, 5e-4))


def _attack_noise(attack, sample_rate, partials) -> list[float]:
    if len(attack) < 64:
        return [-60.0] * len(NOISE_BANDS)
    n_fft = 1 << int(math.ceil(math.log2(len(attack))))
    spectrum = np.abs(np.fft.rfft(attack * np.hanning(len(attack)), n_fft)) ** 2
    freqs = np.fft.rfftfreq(n_fft, 1 / sample_rate)
    width = sample_rate / len(attack) * 1.5
    for partial in partials:
        spectrum[np.abs(freqs - partial) < width] = 0.0
    bands = []
    for centre in NOISE_BANDS:
        mask = (freqs >= centre / math.sqrt(2)) & (freqs < centre * math.sqrt(2))
        bands.append(10 * math.log10(max(float(spectrum[mask].mean()) if mask.any() else 0.0, 1e-20)))
    top = max(bands)
    return [round(b - top, 2) for b in bands]


@dataclass
class VoiceModel:
    """Every analysed note of a profile, and the means to interpolate them."""

    notes: list[NoteModel] = field(default_factory=list)
    noise_level_db: float = -24.0
    """Pick noise relative to the note peak."""

    def _neighbours(self, midi: float, string: int | None) -> list[tuple[NoteModel, float]]:
        pool = [n for n in self.notes if string is None or n.string == string] or self.notes
        if not pool:
            return []
        below = [n for n in pool if n.midi <= midi]
        above = [n for n in pool if n.midi > midi]
        if below and above:
            lo = max(below, key=lambda n: n.midi)
            hi = min(above, key=lambda n: n.midi)
            span = hi.midi - lo.midi
            w = (midi - lo.midi) / span if span else 0.5
            return [(lo, 1 - w), (hi, w)]
        nearest = min(pool, key=lambda n: abs(n.midi - midi))
        return [(nearest, 1.0)]

    def parameters(self, midi: float, string: int | None = None) -> tuple[np.ndarray, np.ndarray, float, np.ndarray]:
        """Interpolated (levels, decays, inharmonicity, noise) for a pitch.

        Harmonic *k* of the target is interpolated against harmonic *k* of the
        neighbours. Beyond the last sample the nearest note's shape is used
        and its decays steepened with pitch, the way a string's do.
        """
        neighbours = self._neighbours(midi, string)
        if not neighbours:
            raise ValueError("the voice model has no notes")
        count = harmonic_count(midi_to_hz(midi))
        levels = np.zeros(count)
        decays = np.zeros(count)
        inharm = 0.0
        noise = np.zeros(len(NOISE_BANDS))
        for note, weight in neighbours:
            lv = np.full(count, -80.0)
            dc = np.full(count, -60.0)
            n = min(count, len(note.level_db))
            lv[:n] = note.level_db[:n]
            dc[:n] = note.decay_db_s[:n]
            if n < count and n:
                dc[n:] = dc[n - 1]
            levels += weight * lv
            decays += weight * dc
            inharm += weight * note.inharmonicity
            if note.noise_db:
                noise += weight * np.array(note.noise_db)
        if len(neighbours) == 1:
            distance = midi - neighbours[0][0].midi
            # Higher up the neck: shorter string, faster decay. Roughly 4% per
            # semitone, from the profiles this was tuned on.
            decays = decays * (1.04 ** distance)
        return levels, decays, inharm, noise

    def synthesize(
        self,
        midi: float,
        duration: float,
        *,
        string: int | None = None,
        peak_db: float = -12.0,
        sample_rate: int = 48000,
        seed: int | None = None,
    ) -> np.ndarray:
        levels, decays, inharm, noise = self.parameters(midi, string)
        f0 = midi_to_hz(midi)
        length = max(1, int(duration * sample_rate))
        t = np.arange(length) / sample_rate
        rng = np.random.default_rng(seed)
        out = np.zeros(length)
        for k in range(len(levels)):
            harmonic = k + 1
            freq = harmonic * f0 * math.sqrt(1 + inharm * harmonic * harmonic)
            if freq >= 0.48 * sample_rate:
                break
            amplitude = 10 ** (levels[k] / 20)
            if amplitude < 1e-4:
                continue
            envelope = amplitude * 10 ** (decays[k] * t / 20)
            phase = rng.uniform(0, 2 * np.pi)
            out += envelope * np.sin(2 * np.pi * freq * t + phase)

        # A 2 ms rise - a sample-zero sine onset clicks in a way a pick does not.
        rise = min(length, int(0.002 * sample_rate))
        if rise > 1:
            out[:rise] *= np.linspace(0.0, 1.0, rise)

        burst = _shaped_noise(noise, sample_rate, int(0.03 * sample_rate), rng)
        if len(burst):
            burst *= 10 ** (self.noise_level_db / 20) * (np.max(np.abs(out)) or 1.0)
            out[: len(burst)] += burst[:length]

        peak = float(np.max(np.abs(out))) or 1.0
        return out * (10 ** (peak_db / 20) / peak)

    def to_dict(self) -> dict:
        return {"notes": [n.to_dict() for n in self.notes], "noise_level_db": self.noise_level_db}

    @classmethod
    def from_dict(cls, data: dict) -> "VoiceModel":
        return cls(
            notes=[NoteModel.from_dict(n) for n in data.get("notes", [])],
            noise_level_db=float(data.get("noise_level_db", -24.0)),
        )

    def save(self, path: Path) -> Path:
        path.write_text(json.dumps(self.to_dict()), encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path) -> "VoiceModel":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _shaped_noise(bands_db, sample_rate, length, rng) -> np.ndarray:
    if length <= 0 or not len(bands_db):
        return np.zeros(0)
    white = rng.normal(0.0, 1.0, length)
    spectrum = np.fft.rfft(white)
    freqs = np.fft.rfftfreq(length, 1 / sample_rate)
    centres = np.log2(np.array(NOISE_BANDS, dtype=float))
    gains_db = np.interp(np.log2(np.maximum(freqs, 1.0)), centres, np.asarray(bands_db, dtype=float))
    shaped = np.fft.irfft(spectrum * 10 ** (gains_db / 20), length)
    shaped *= np.exp(-np.arange(length) / (0.006 * sample_rate))
    peak = float(np.max(np.abs(shaped))) or 1.0
    return shaped / peak


def fit(samples: list[tuple[np.ndarray, float, int | None]], sample_rate: int) -> VoiceModel:
    """Analyse ``(audio, midi, string)`` samples into a voice model."""
    model = VoiceModel()
    ratios = []
    for audio, midi, string in samples:
        analysed = analyse_note(audio, sample_rate, midi, string=string)
        if analysed is not None:
            model.notes.append(analysed)
            attack = np.asarray(audio[: int(0.015 * sample_rate)])
            if len(attack):
                ratios.append(analysed.peak_db)
    model.notes.sort(key=lambda n: (n.string or 0, n.midi))
    return model


def similarity(original: np.ndarray, resynthesised: np.ndarray, sample_rate: int) -> float:
    """How close a resynthesised note's spectrum is to the original, 0-1.

    The check that says whether the voice model is worth using for a given
    profile: compare each sample against the model's own rebuild of it.
    """
    from ..match.metrics import log_spectral_distance

    n = min(len(original), len(resynthesised))
    if n < 2048:
        return 0.0
    distance = log_spectral_distance(original[:n], resynthesised[:n], sample_rate)
    value = distance.value if hasattr(distance, "value") else float(distance)
    return float(max(0.0, 1.0 - value / 20.0))

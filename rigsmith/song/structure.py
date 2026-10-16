"""Tempo, key, and where the song changes.

Section boundaries come from self-similarity: describe every beat-length
frame by its harmony (chroma) and its timbre (MFCC), compare every frame with
every other, and slide a checkerboard kernel down the diagonal (Foote 2000).
Where the music before a point is self-similar, the music after it is
self-similar, and the two are unlike each other, the kernel lights up - that
is a boundary. Segments are then clustered by their average features, so a
section that comes back gets the same letter, and the letters are named by
the patterns songs actually follow: the loudest thing that repeats most is
the chorus, the thing that alternates with it is the verse, something loud
that happens once in the second half is likely a solo.

Those names are a guess and are presented as one. The boundaries are the
useful part - the user picks the part; the map just puts handles on it.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

from ..learn.align import audio_chroma
from ..learn.instrument import NOTE_NAMES

#: Frame hop for the structural features, seconds.
HOP_S = 0.1

#: Sections shorter than this are merged into a neighbour.
MIN_SECTION_S = 6.0


def _mono(audio: np.ndarray) -> np.ndarray:
    audio = np.asarray(audio, dtype=np.float64)
    return audio.mean(axis=1) if audio.ndim > 1 else audio


def onset_envelope(audio: np.ndarray, sample_rate: int, *, hop_s: float = 0.01) -> np.ndarray:
    """Positive spectral flux per hop - what the beat tracker listens to."""
    audio = _mono(audio)
    n_fft = 2048
    hop = max(1, int(hop_s * sample_rate))
    padded = np.pad(audio, (n_fft // 2, n_fft // 2))
    count = max(0, 1 + (len(padded) - n_fft) // hop)
    if count < 3:
        return np.zeros(count)
    window = np.hanning(n_fft)
    previous = None
    flux = np.zeros(count)
    for i in range(count):
        frame = padded[i * hop : i * hop + n_fft] * window
        mag = np.log1p(100 * np.abs(np.fft.rfft(frame))[: n_fft // 4])
        if previous is not None:
            flux[i] = np.maximum(mag - previous, 0).sum()
        previous = mag
    return flux


#: How much a candidate beat is credited for the pulse at half its length.
HALF_BEAT_WEIGHT = 0.5


def estimate_tempo(audio: np.ndarray, sample_rate: int) -> tuple[float, float]:
    """Tempo in BPM and a 0-1 confidence, from onset autocorrelation.

    Autocorrelation of the onset envelope, weighted towards 120 BPM the way
    listeners are (a log-Gaussian prior), searched over 60-200 BPM. Each
    candidate beat is also credited with half the pulse at half its length:
    a real beat is subdivided (the eighth notes), while a dotted-quarter
    impostor's half falls between the notes. Without that, 155 BPM with
    eighth notes read as 103 - a 3:2 error no halve or double button can
    fix. On 33 synthetic parts (eighth-note riffs with drums and half-time
    drums at 72-185 BPM, quarter notes, a shuffle, guitar alone) it went
    from 19 exact, 8 octave errors and 6 others to 26 exact, 7 octave
    errors and no others. Octave errors - 70 for 140 - remain the known
    failure, and the song page fixes one in a click.
    """
    hop_s = 0.01
    envelope = onset_envelope(audio, sample_rate, hop_s=hop_s)
    if len(envelope) < 400:
        return 0.0, 0.0
    envelope = envelope - envelope.mean()
    size = 1 << int(math.ceil(math.log2(2 * len(envelope))))
    spectrum = np.fft.rfft(envelope, size)
    ac = np.fft.irfft(spectrum * np.conj(spectrum), size)[: len(envelope)]
    if ac[0] <= 0:
        return 0.0, 0.0
    ac /= ac[0]
    lags = np.arange(len(ac)) * hop_s
    bpm = np.where(lags > 0, 60.0 / np.maximum(lags, 1e-9), 0)
    valid = (bpm >= 60) & (bpm <= 200)
    prior = np.exp(-0.5 * (np.log2(np.maximum(bpm, 1e-9) / 120.0) / 0.9) ** 2)
    half = ac[np.clip(np.round(np.arange(len(ac)) / 2).astype(int), 0, len(ac) - 1)]
    half[0] = 0.0
    score = np.where(valid, (ac + HALF_BEAT_WEIGHT * half) * prior, -np.inf)
    best = int(np.argmax(score))
    if not np.isfinite(score[best]):
        return 0.0, 0.0
    # Parabolic refinement of the lag.
    lag = float(best)
    if 0 < best < len(ac) - 1:
        a, b, c = ac[best - 1 : best + 2]
        denominator = a - 2 * b + c
        if denominator:
            lag += 0.5 * (a - c) / denominator
    tempo = 60.0 / (lag * hop_s)
    confidence = float(np.clip(ac[best] / 0.5, 0.0, 1.0))
    return round(tempo, 1), round(confidence, 2)


#: Krumhansl-Kessler key profiles.
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


def estimate_key(chroma: np.ndarray) -> tuple[str, float]:
    """Key name ('E minor') and correlation, from mean chroma."""
    if not len(chroma):
        return "unknown", 0.0
    profile = chroma.mean(axis=0)
    if profile.sum() <= 0:
        return "unknown", 0.0
    best = ("unknown", -1.0)
    for tonic in range(12):
        for name, template in (("major", MAJOR), ("minor", MINOR)):
            rotated = np.roll(template, tonic)
            r = float(np.corrcoef(profile, rotated)[0, 1])
            if r > best[1]:
                best = (f"{NOTE_NAMES[tonic]} {name}", r)
    return best[0], round(best[1], 3)


def _mfcc_frames(audio: np.ndarray, sample_rate: int, hop_s: float) -> np.ndarray:
    """Frames x 13 MFCCs at the structural hop, centred like the chroma."""
    from scipy.fftpack import dct

    from ..match.metrics import EPSILON, _stft_magnitude, mel_filterbank

    n_fft = 4096
    hop = max(1, int(hop_s * sample_rate))
    padded = np.pad(audio, (n_fft // 2, n_fft // 2))
    magnitude = _stft_magnitude(padded, n_fft, hop)
    if not len(magnitude):
        return np.zeros((0, 13))
    mel = (magnitude ** 2) @ mel_filterbank(sample_rate, n_fft, 40).T
    return dct(np.log(mel + EPSILON), type=2, axis=-1, norm="ortho")[:, :13]


def _energy_frames(audio: np.ndarray, sample_rate: int, hop_s: float) -> np.ndarray:
    hop = int(hop_s * sample_rate)
    usable = (len(audio) // hop) * hop
    if usable == 0:
        return np.zeros(0)
    blocks = audio[:usable].reshape(-1, hop)
    return 10 * np.log10(np.mean(blocks * blocks, axis=1) + 1e-12)


def novelty(features: np.ndarray, kernel_s: float = 8.0, hop_s: float = HOP_S) -> np.ndarray:
    """Foote's checkerboard novelty along a self-similarity matrix."""
    n = len(features)
    if n < 4:
        return np.zeros(n)
    norms = np.linalg.norm(features, axis=1, keepdims=True)
    unit = np.divide(features, norms, out=np.zeros_like(features), where=norms > 0)
    similarity = unit @ unit.T
    half = max(2, int(kernel_s / hop_s / 2))
    taper = np.exp(-0.5 * (np.linspace(-2, 2, 2 * half)) ** 2)
    kernel = np.outer(taper, taper)
    sign = np.ones((2 * half, 2 * half))
    sign[:half, half:] = -1
    sign[half:, :half] = -1
    kernel *= sign
    padded = np.pad(similarity, half, mode="edge")
    curve = np.zeros(n)
    for i in range(n):
        block = padded[i : i + 2 * half, i : i + 2 * half]
        curve[i] = float((block * kernel).sum())
    curve = np.maximum(curve, 0)
    top = curve.max() or 1.0
    return curve / top


@dataclass
class Section:
    start: float
    end: float
    label: str
    """A, B, C... - the same letter means the same music."""
    name: str = ""
    """intro, verse, chorus, solo, bridge, outro - a guess."""
    energy_db: float = 0.0
    repeats: int = 1

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_dict(self) -> dict:
        data = asdict(self)
        data["duration"] = round(self.duration, 2)
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Section":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Structure:
    duration: float
    tempo: float = 0.0
    tempo_confidence: float = 0.0
    key: str = "unknown"
    key_confidence: float = 0.0
    sections: list[Section] = field(default_factory=list)

    def describe(self) -> str:
        lines = [
            f"{self.duration:.0f}s, {self.tempo:.0f} BPM ({self.tempo_confidence:.0%}), {self.key}",
        ]
        for section in self.sections:
            minutes = f"{int(section.start // 60)}:{section.start % 60:04.1f}"
            lines.append(f"  {minutes}  {section.label}  {section.name or '?':<7} {section.duration:5.1f}s  {section.energy_db:6.1f} dB")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 2),
            "tempo": self.tempo,
            "tempo_confidence": self.tempo_confidence,
            "key": self.key,
            "key_confidence": self.key_confidence,
            "sections": [s.to_dict() for s in self.sections],
            "describe": self.describe(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Structure":
        return cls(
            duration=data.get("duration", 0.0),
            tempo=data.get("tempo", 0.0),
            tempo_confidence=data.get("tempo_confidence", 0.0),
            key=data.get("key", "unknown"),
            key_confidence=data.get("key_confidence", 0.0),
            sections=[Section.from_dict(s) for s in data.get("sections", [])],
        )


def analyse(audio: np.ndarray, sample_rate: int, *, min_section_s: float | None = None) -> Structure:
    """Map a mix (or a stem) into labelled sections.

    The kernel and the shortest allowed section scale with the song: a
    three-minute track's sections are fifteen to thirty seconds, a sixty-
    second excerpt's are a quarter of that, and one fixed size would either
    miss every boundary in the excerpt or split every phrase of the song.
    """
    audio = _mono(audio)
    duration = len(audio) / sample_rate
    if min_section_s is None:
        min_section_s = float(np.clip(duration / 14, 3.0, MIN_SECTION_S))
    kernel_s = float(np.clip(duration / 10, 4.0, 12.0))
    result = Structure(duration=round(duration, 2))
    result.tempo, result.tempo_confidence = estimate_tempo(audio, sample_rate)

    chroma = audio_chroma(audio, sample_rate, hop_s=HOP_S)
    result.key, result.key_confidence = estimate_key(chroma)
    timbre = _mfcc_frames(audio, sample_rate, HOP_S)
    energy = _energy_frames(audio, sample_rate, HOP_S)
    n = min(len(chroma), len(timbre), len(energy))
    if n < int(2 * min_section_s / HOP_S):
        result.sections = [Section(0.0, round(duration, 2), "A", "whole", float(np.mean(energy)) if len(energy) else 0.0)]
        return result

    chroma, timbre, energy = chroma[:n], timbre[:n], energy[:n]
    chroma_n = chroma / (np.linalg.norm(chroma, axis=1, keepdims=True) + 1e-9)
    timbre_z = (timbre - timbre.mean(axis=0)) / (timbre.std(axis=0) + 1e-9)
    loud = (energy - energy.mean()) / (energy.std() + 1e-9)
    # Smooth over a couple of seconds so a fill does not look like a section.
    from scipy.ndimage import uniform_filter1d

    # Novelty per feature family, each normalised on its own, then averaged.
    # Combined into one feature vector, loudness drowned everything else: the
    # clean intro's jump to the distorted verse scored 0.75 while every
    # verse/chorus change scored 0.07 - real boundaries, invisible.
    smooth = max(1, int(2.0 / HOP_S))
    families = [
        uniform_filter1d(chroma_n, size=smooth, axis=0),
        uniform_filter1d(timbre_z[:, 1:], size=smooth, axis=0),
        uniform_filter1d(loud[:, None], size=smooth, axis=0),
    ]
    curves = np.array([novelty(f, kernel_s=kernel_s) for f in families])
    # Half the strongest family, half their mean: a change any one family sees
    # clearly is a boundary (a guitar-only stem changes riff without changing
    # level), while the mean keeps one noisy family from inventing them.
    curve = 0.5 * curves.max(axis=0) + 0.5 * curves.mean(axis=0)
    features = np.hstack([families[0] * 1.5, families[1] * 0.5, families[2]])

    minimum = int(min_section_s / HOP_S)
    floor = max(0.08, 1.5 * float(np.median(curve)))
    peaks = []
    for i in range(1, n - 1):
        if curve[i] >= curve[i - 1] and curve[i] >= curve[i + 1] and curve[i] > floor:
            if peaks and i - peaks[-1] < minimum:
                if curve[i] > curve[peaks[-1]]:
                    peaks[-1] = i
                continue
            peaks.append(i)
    edges = [0] + [p for p in peaks if minimum <= p <= n - minimum] + [n]

    segments = []
    for a, b in zip(edges[:-1], edges[1:]):
        segments.append((a, b, features[a:b].mean(axis=0), float(np.mean(energy[a:b]))))

    # Standardise each dimension across the segments before comparing them,
    # so the dimensions that actually vary between sections decide - raw,
    # the shared character of one guitar through one amp made every section
    # look like every other.
    means = np.array([s[2] for s in segments])
    spread = means.std(axis=0)
    standard = (means - means.mean(axis=0)) / np.where(spread > 1e-9, spread, 1.0)
    labels = _cluster(list(standard))
    sections = [
        Section(round(a * HOP_S, 2), round(min(b * HOP_S, duration), 2), labels[i], energy_db=round(e, 1))
        for i, (a, b, _, e) in enumerate(segments)
    ]
    sections = _merge_runs(sections)
    counts: dict[str, int] = {}
    for section in sections:
        counts[section.label] = counts.get(section.label, 0) + 1
    for section in sections:
        section.repeats = counts[section.label]
    _name(sections)
    result.sections = sections
    return result


def _merge_runs(sections: list["Section"]) -> list["Section"]:
    """One section where the clustering heard the same music twice in a row.

    A long chorus over a four-chord cycle can light the curve up halfway
    through; when both halves come back with the same letter, they were one.
    """
    merged: list[Section] = []
    for section in sections:
        if merged and merged[-1].label == section.label:
            last = merged[-1]
            a, b = last.end - last.start, section.end - section.start
            last.energy_db = round((last.energy_db * a + section.energy_db * b) / max(a + b, 1e-9), 1)
            last.end = section.end
        else:
            merged.append(section)
    return merged


def _cluster(vectors: list[np.ndarray], threshold: float = 0.6) -> list[str]:
    """Greedy agglomeration by cosine distance: same music, same letter."""
    labels: list[str] = []
    centroids: list[np.ndarray] = []
    for vector in vectors:
        unit = vector / (np.linalg.norm(vector) + 1e-9)
        best, best_d = None, threshold
        for index, centroid in enumerate(centroids):
            distance = 1 - float(unit @ (centroid / (np.linalg.norm(centroid) + 1e-9)))
            if distance < best_d:
                best, best_d = index, distance
        if best is None:
            centroids.append(unit.copy())
            labels.append(chr(ord("A") + min(len(centroids) - 1, 25)))
        else:
            centroids[best] = centroids[best] + unit
            labels.append(chr(ord("A") + best))
    return labels


def _name(sections: list[Section]) -> None:
    """Name sections by the patterns songs follow. A guess, said as one.

    Bookends first: a first or last section clearly quieter than the rest is
    the intro or outro, whatever it shares a letter with - otherwise a quiet
    intro and outro that match each other are the only "repeating" pair and
    get called the verse. Then, among what is left, the loudest repeating
    letter is the chorus and the next most repeated is the verse.
    """
    if not sections:
        return
    if len(sections) == 1:
        sections[0].name = "whole"
        return
    middle = [s.energy_db for s in sections[1:-1]] or [s.energy_db for s in sections]
    typical = float(np.median(middle))
    bookends = set()
    if sections[0].energy_db < typical - 2.0:
        sections[0].name = "intro"
        bookends.add(0)
    if sections[-1].energy_db < typical - 2.0:
        sections[-1].name = "outro"
        bookends.add(len(sections) - 1)

    body = [(i, s) for i, s in enumerate(sections) if i not in bookends]
    counts: dict[str, int] = {}
    loudness: dict[str, list[float]] = {}
    for _, s in body:
        counts[s.label] = counts.get(s.label, 0) + 1
        loudness.setdefault(s.label, []).append(s.energy_db)
    repeating = [k for k, c in counts.items() if c >= 2]
    chorus = max(repeating, key=lambda k: float(np.mean(loudness[k]))) if repeating else None
    others = [k for k in repeating if k != chorus]
    verse = max(others, key=lambda k: counts[k]) if others else None
    end = sections[-1].end
    for index, section in body:
        if section.label == chorus:
            section.name = "chorus"
        elif section.label == verse:
            section.name = "verse"
        elif counts.get(section.label, 0) == 1 and section.start > end * 0.4 and section.energy_db >= typical:
            section.name = "solo"
        elif counts.get(section.label, 0) == 1:
            section.name = "bridge"
        else:
            section.name = "section"

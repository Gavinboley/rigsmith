"""How many guitars there are, and where each one sits.

Rock records stack guitars in a handful of conventional ways, and the stereo
image gives most of them away:

* a **double-tracked rhythm** - the same riff played twice, one take hard left
  and one hard right. Lots of energy at both extremes, the two sides playing
  the same notes at the same moments, yet barely coherent as waveforms
  (they are two performances).
* a **centred lead or single rhythm** - energy at the middle, highly coherent
  between the channels because it is one mono signal panned centre.
* a **wide stereo effect** - energy in the middle but *incoherent*: a stereo
  chorus, a ping-pong delay, a room mic pair.
* **two different parts** on the two sides - energy at both extremes, but the
  sides do not play together.

No model separates guitar layers (as of 2026 nothing public does), so this
works from the stereo image, bin by bin in the STFT, using two numbers:

* **pan** - the level difference between the channels, -1 (left) to +1;
* **inter-channel coherence** - whether the two channels move together over a
  few frames. One mono source panned centre is coherent; two performances of
  the same riff are not, because their timing and pitch drift against each
  other note by note, even though they put energy in the same bins.

The second number is the one that matters, and it is easy to miss: a
double-tracked rhythm does *not* show up as energy at the extremes of a pan
histogram - both takes play the same notes, so most bins sit near the middle.
What gives it away is that the middle is incoherent. So the centre layer is
what is both central and coherent; what is left in each channel is that
side's layer; and whether the sides are one part played twice or two
different parts is decided by whether their note onsets line up.

Where two layers share a pan position, a coherence and a frequency at the
same moment, nothing short of a trained model separates them, and the
extracted layers will bleed.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

N_FFT = 4096
HOP = 1024

#: Pan positions that count as "hard" and as "centre", in -1..1.
HARD = 0.55
CENTRE = 0.25

#: A side or the centre needs this share of the energy to count as a layer.
MIN_SHARE = 0.08


@dataclass
class Layer:
    layer_id: str
    """left, right, centre, wide."""
    pan: float
    share: float
    """Fraction of the stem's energy in this layer."""
    coherence: float | None = None
    role: str = ""
    """What it probably is - a guess, presented as one."""
    partner: str | None = None
    """For a double track: the layer playing the same part."""
    path: str | None = None
    sections: dict[str, float] = field(default_factory=dict)
    """Section start (as text) -> share of this layer active there."""
    descriptors: dict = field(default_factory=dict)

    def describe(self) -> str:
        bits = [f"{self.layer_id:<6} pan {self.pan:+.2f}  {self.share:.0%} of the energy"]
        if self.role:
            bits.append(self.role)
        if self.partner:
            bits.append(f"doubled with {self.partner}")
        return "  ".join(bits)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["describe"] = self.describe()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Layer":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class LayerMap:
    stereo: bool
    layers: list[Layer] = field(default_factory=list)
    width: float = 0.0
    """0 for mono, towards 1 for everything at the extremes."""
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "stereo": self.stereo,
            "width": round(self.width, 3),
            "summary": self.summary,
            "layers": [layer.to_dict() for layer in self.layers],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LayerMap":
        return cls(
            stereo=bool(data.get("stereo")),
            layers=[Layer.from_dict(l) for l in data.get("layers", [])],
            width=float(data.get("width", 0.0)),
            summary=data.get("summary", ""),
        )


def _stft(channel: np.ndarray, sample_rate: int):
    from scipy.signal import stft

    _, _, spectrum = stft(channel, fs=sample_rate, nperseg=N_FFT, noverlap=N_FFT - HOP, boundary="even")
    return spectrum


def _istft(spectrum: np.ndarray, sample_rate: int, length: int) -> np.ndarray:
    from scipy.signal import istft

    _, audio = istft(spectrum, fs=sample_rate, nperseg=N_FFT, noverlap=N_FFT - HOP, boundary=True)
    out = np.zeros(length)
    out[: min(length, len(audio))] = audio[:length]
    return out


def pan_map(left: np.ndarray, right: np.ndarray, sample_rate: int):
    """Per-bin pan position (-1 left .. +1 right) and energy."""
    L = _stft(left, sample_rate)
    R = _stft(right, sample_rate)
    mag_l, mag_r = np.abs(L), np.abs(R)
    total = mag_l + mag_r
    pan = np.divide(mag_r - mag_l, total, out=np.zeros_like(total), where=total > 1e-12)
    energy = mag_l ** 2 + mag_r ** 2
    return L, R, pan, energy


def _coherence(a: np.ndarray, b: np.ndarray, sample_rate: int = 48000) -> float:
    """Magnitude-squared coherence averaged over the guitar's band."""
    from scipy.signal import coherence

    if len(a) < N_FFT * 2:
        return 0.0
    freqs, values = coherence(a, b, fs=sample_rate, nperseg=N_FFT)
    band = (freqs >= 100) & (freqs <= 5000)
    return float(np.mean(values[band])) if band.any() else 0.0


def _onset_envelope(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    from .structure import onset_envelope

    return onset_envelope(audio, sample_rate, hop_s=0.02)


def _plays_together(a: np.ndarray, b: np.ndarray, sample_rate: int, *, window_s: float = 8.0) -> float:
    """How much of the time two parts play the same rhythm, 0-1.

    Judged window by window, over the windows where *both* are playing - a
    side that has the intro to itself is not evidence against the verse
    being doubled. In each, the smoothed onset envelopes are correlated with
    up to 40 ms of slop: two takes of one riff land within tens of
    milliseconds of each other, not on the same 20 ms frame, and unsmoothed
    flux peaks that narrow miss each other. The answer is the share of
    co-active windows that correlate above 0.5.
    """
    from scipy.ndimage import gaussian_filter1d

    hop_s = 0.02
    ea = gaussian_filter1d(_onset_envelope(a, sample_rate), 1.5)
    eb = gaussian_filter1d(_onset_envelope(b, sample_rate), 1.5)
    n = min(len(ea), len(eb))
    if n < 50:
        return 0.0
    span = max(50, int(window_s / hop_s))
    level_a = _window_levels(a, sample_rate, window_s)
    level_b = _window_levels(b, sample_rate, window_s)
    agree, both = 0, 0
    for index, start in enumerate(range(0, n - span // 2, span)):
        if index >= len(level_a) or index >= len(level_b):
            break
        if level_a[index] < -20 or level_b[index] < -20:
            continue
        x, y = ea[start : start + span], eb[start : start + span]
        m = min(len(x), len(y))
        x, y = x[:m] - x[:m].mean(), y[:m] - y[:m].mean()
        norm = float(np.linalg.norm(x) * np.linalg.norm(y)) or 1.0
        best = max(
            float(np.dot(x[s:], y[: m - s]) if s >= 0 else np.dot(x[: m + s], y[-s:])) / norm
            for s in range(-2, 3)
        )
        both += 1
        agree += best > 0.5
    return agree / both if both else 0.0


def _window_levels(audio: np.ndarray, sample_rate: int, window_s: float) -> list[float]:
    """RMS per window in dB relative to the loudest window."""
    span = int(window_s * sample_rate)
    levels = []
    for start in range(0, len(audio) - span // 2, span):
        piece = audio[start : start + span]
        levels.append(float(np.sqrt(np.mean(piece ** 2))) if len(piece) else 0.0)
    top = max(levels) if levels else 0.0
    return [20 * np.log10(max(v, 1e-12) / top) if top > 0 else -120.0 for v in levels]


def _smoothed_coherence(L: np.ndarray, R: np.ndarray) -> np.ndarray:
    """Per-bin coherence over a few frames and neighbouring bins."""
    from scipy.ndimage import uniform_filter

    size = (3, 9)  # bins x frames
    cross = L * np.conj(R)
    real = uniform_filter(cross.real, size=size)
    imag = uniform_filter(cross.imag, size=size)
    power_l = uniform_filter(np.abs(L) ** 2, size=size)
    power_r = uniform_filter(np.abs(R) ** 2, size=size)
    denominator = np.sqrt(np.maximum(power_l * power_r, 0.0))
    return np.divide(np.sqrt(real ** 2 + imag ** 2), denominator, out=np.zeros_like(denominator), where=denominator > 1e-20)


def analyse(
    audio: np.ndarray,
    sample_rate: int,
    *,
    sections=None,
    extract_to: Path | None = None,
) -> LayerMap:
    """Find the guitar layers in a stereo stem; optionally write each out."""
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim == 1 or audio.shape[1] < 2:
        result = LayerMap(stereo=False, summary="mono - one layer, or several that cannot be told apart by position")
        result.layers.append(Layer("centre", 0.0, 1.0, coherence=1.0, role="everything (mono stem)"))
        return result

    left, right = audio[:, 0], audio[:, 1]
    if float(np.max(np.abs(left - right))) < 1e-6 * max(float(np.max(np.abs(left))), 1e-12):
        result = LayerMap(stereo=False, summary="both channels identical - a mono stem in a stereo file")
        result.layers.append(Layer("centre", 0.0, 1.0, coherence=1.0, role="everything (mono stem)"))
        return result

    L, R, pan, energy = pan_map(left, right, sample_rate)
    icc = _smoothed_coherence(L, R)
    total = float(energy.sum()) or 1e-12
    result = LayerMap(stereo=True)
    result.width = float((np.abs(pan) * energy).sum() / total)

    # The centre layer: central *and* coherent, softly.
    centred = np.exp(-0.5 * (pan / 0.22) ** 2)
    together = 1 / (1 + np.exp(-(icc - 0.75) * 18))
    centre_mask = centred * together
    rest = 1.0 - centre_mask

    length = len(left)
    candidates = {
        "centre": ((L + R) * 0.5 * centre_mask, float((energy * centre_mask).sum() / total), 0.0),
        "left": (L * rest, float((np.abs(L * rest) ** 2).sum() / total), -1.0),
        "right": (R * rest, float((np.abs(R * rest) ** 2).sum() / total), 1.0),
    }
    extracted: dict[str, np.ndarray] = {}
    for name, (spectrum, share, position) in candidates.items():
        if share < MIN_SHARE:
            continue
        extracted[name] = _istft(spectrum, sample_rate, length)
        result.layers.append(Layer(name, position, round(share, 3)))

    layers = {l.layer_id: l for l in result.layers}
    if "left" in extracted and "right" in extracted:
        coherent = _coherence(extracted["left"], extracted["right"], sample_rate)
        played_together = _plays_together(extracted["left"], extracted["right"], sample_rate)
        for name in ("left", "right"):
            layers[name].coherence = round(coherent, 3)
        if played_together >= 0.6:
            role = "double-tracked part" if coherent < 0.6 else "one part spread wide (a stereo effect)"
            layers["left"].role = layers["right"].role = role
            if coherent < 0.6:
                layers["left"].partner, layers["right"].partner = "right", "left"
        else:
            layers["left"].role = "a part of its own"
            layers["right"].role = "a different part"
    elif "left" in extracted or "right" in extracted:
        side = "left" if "left" in extracted else "right"
        layers[side].role = f"a part panned {side}"
    if "centre" in layers:
        layers["centre"].coherence = round(float((icc * energy * centre_mask).sum() / max(float((energy * centre_mask).sum()), 1e-20)), 3)

    for layer in result.layers:
        signal = extracted.get(layer.layer_id)
        if signal is None:
            continue
        layer.descriptors = _role_hints(signal, sample_rate)
        if layer.layer_id == "centre":
            if layer.descriptors.get("monophonic"):
                layer.role = "centred single-note part - probably the lead"
            else:
                layer.role = "centred part"
        if sections:
            layer.sections = _activity(signal, sample_rate, sections)
        if extract_to is not None:
            from ..match.render_offline import save_audio

            extract_to.mkdir(parents=True, exist_ok=True)
            layer.path = str(save_audio(extract_to / f"layer-{layer.layer_id}.wav", signal, sample_rate))

    result.summary = _summary(result)
    return result


def _role_hints(signal: np.ndarray, sample_rate: int) -> dict:
    """Register and polyphony, which separate a lead from a rhythm part."""
    from ..learn.transcribe import salience_frames, _spectrogram

    # A slice from the busiest part keeps this cheap on a long stem.
    span = int(20 * sample_rate)
    if len(signal) > span:
        # A one-second moving energy by cumulative sum - a direct
        # convolution here is 10^11 operations on a full song.
        cumulative = np.concatenate([[0.0], np.cumsum(signal ** 2)])
        energy = cumulative[sample_rate:] - cumulative[:-sample_rate]
        start = int(np.argmax(energy[: max(1, len(energy) - span)]))
        signal = signal[start : start + span]
    frames = salience_frames(_spectrogram(signal, sample_rate), sample_rate, min_midi=36, max_polyphony=4)
    active = [f for f in frames if f]
    if not active:
        return {}
    polyphony = float(np.median([len(f) for f in active]))
    pitches = [p for f in active for p, _ in f]
    return {
        "polyphony": round(polyphony, 2),
        "monophonic": polyphony <= 1.0,
        "median_midi": float(np.median(pitches)),
        "register": "high" if np.median(pitches) >= 62 else "low" if np.median(pitches) < 52 else "middle",
    }


def _activity(signal: np.ndarray, sample_rate: int, sections) -> dict[str, float]:
    """How present this layer is in each section, relative to its loudest."""
    levels = {}
    for section in sections:
        a, b = int(section.start * sample_rate), int(section.end * sample_rate)
        piece = signal[a:b]
        levels[f"{section.start:.2f}"] = float(np.sqrt(np.mean(piece ** 2))) if len(piece) else 0.0
    top = max(levels.values()) if levels else 0.0
    return {k: round(v / top, 3) if top > 0 else 0.0 for k, v in levels.items()}


def _summary(result: LayerMap) -> str:
    names = {l.layer_id: l for l in result.layers}
    if "left" in names and "right" in names and names["left"].partner:
        text = "a double-tracked part, one take each side"
    elif "left" in names and "right" in names and "spread" in names["left"].role:
        text = "one part spread across the stereo field"
    elif "left" in names and "right" in names:
        text = "different parts on the left and right"
    elif "left" in names or "right" in names:
        side = "left" if "left" in names else "right"
        text = f"a part panned {side}"
    else:
        text = "nothing off-centre"
    if "centre" in names:
        text += ", plus a centred part" + (" that looks like the lead" if "lead" in names["centre"].role else "")
    return text

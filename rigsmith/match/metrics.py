"""Audio similarity metrics for comparing a rendered tone to a target.

Implemented on numpy and scipy alone. That is deliberate rather than
incidental: these numbers only mean anything relative to each other, so a
metric that changed depending on which optional packages happened to be
installed would silently reorder candidates between two users - or between
a developer's machine and CI, which is how this was found.

**The distinction that matters.** These metrics fall into two families, and
mixing them up produces confident nonsense:

*Aligned* metrics - :func:`esr`, :func:`multi_resolution_stft_loss` - compare
two signals sample for sample. They are only meaningful when both come from
the *same performance*: your DI reamped through a candidate rig versus your
DI through the reference. This is the setting NAM itself uses, where ESR
around 0.04 is good and above 0.1 means something broke.

*Distribution* metrics - :func:`spectral_summary`, MFCC statistics - compare
aggregate character and do not care about timing. These are what you use
against a commercial recording, because a record is a different performance
played by a different person on a different guitar, and no amount of
alignment makes sample-wise error meaningful. They give a *ranking*, never an
absolute verdict.

:mod:`rigsmith.match.score` enforces which family applies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

import numpy as np

EPSILON = 1e-12


class ComparisonMode(str, Enum):
    """Whether two signals can be compared sample for sample."""

    ALIGNED = "aligned"
    """Same performance, time-aligned. Sample-wise error is meaningful."""

    DISTRIBUTION = "distribution"
    """Different performances. Only aggregate character can be compared."""


@dataclass
class MetricResult:
    """One measurement, with enough context to interpret it."""

    name: str
    value: float
    mode: ComparisonMode
    lower_is_better: bool = True
    interpretation: str | None = None
    caveat: str | None = None

    def render(self) -> str:
        direction = "lower is better" if self.lower_is_better else "higher is better"
        line = f"{self.name}: {self.value:.5f} ({direction})"
        if self.interpretation:
            line += f" - {self.interpretation}"
        if self.caveat:
            line += f"\n    note: {self.caveat}"
        return line


# --------------------------------------------------------------------------
# preparation
# --------------------------------------------------------------------------


def to_mono(signal: np.ndarray) -> np.ndarray:
    """Collapse to a 1-D float64 signal."""
    array = np.asarray(signal, dtype=np.float64)
    if array.ndim == 1:
        return array
    if array.ndim == 2:
        # Accept either (samples, channels) or (channels, samples), averaging
        # across whichever axis is the channel axis - the shorter one.
        channel_axis = 0 if array.shape[0] < array.shape[1] else 1
        return array.mean(axis=channel_axis)
    raise ValueError(f"expected a 1-D or 2-D signal, got shape {array.shape}")


def normalise_rms(signal: np.ndarray, target_rms: float = 0.1) -> np.ndarray:
    """Scale to a target RMS.

    Loudness is not tone. Without this, a candidate that is merely louder
    scores as more different, which is exactly backwards.
    """
    signal = np.asarray(signal, dtype=np.float64)
    rms = float(np.sqrt(np.mean(signal**2)))
    if rms < EPSILON:
        return signal
    return signal * (target_rms / rms)


def match_length(a: np.ndarray, b: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Trim both signals to the shorter length."""
    n = min(len(a), len(b))
    return a[:n], b[:n]


# --------------------------------------------------------------------------
# aligned metrics
# --------------------------------------------------------------------------


def esr(target: np.ndarray, prediction: np.ndarray, *, normalise: bool = True) -> float:
    """Error-to-signal ratio: ``sum((t - p)^2) / sum(t^2)``.

    The metric the NAM and GuitarML communities train and validate against.
    Roughly 0.04 is a good capture; above 0.1 something has gone wrong.

    Only meaningful on time-aligned signals from the same performance.
    """
    target = to_mono(target)
    prediction = to_mono(prediction)
    target, prediction = match_length(target, prediction)
    if normalise:
        target = normalise_rms(target)
        prediction = normalise_rms(prediction)

    denominator = float(np.sum(target**2))
    if denominator < EPSILON:
        return float("inf")
    return float(np.sum((target - prediction) ** 2) / denominator)


def _stft_magnitude(signal: np.ndarray, n_fft: int, hop: int) -> np.ndarray:
    """Magnitude STFT with a Hann window, numpy only."""
    if len(signal) < n_fft:
        signal = np.pad(signal, (0, n_fft - len(signal)))
    window = np.hanning(n_fft)
    frame_count = 1 + (len(signal) - n_fft) // hop
    frames = np.lib.stride_tricks.sliding_window_view(signal, n_fft)[::hop][:frame_count]
    spectrum = np.fft.rfft(frames * window, axis=-1)
    return np.abs(spectrum)


def spectral_convergence(target_mag: np.ndarray, pred_mag: np.ndarray) -> float:
    denominator = float(np.linalg.norm(target_mag))
    if denominator < EPSILON:
        return float("inf")
    return float(np.linalg.norm(target_mag - pred_mag) / denominator)


def multi_resolution_stft_loss(
    target: np.ndarray,
    prediction: np.ndarray,
    *,
    fft_sizes: tuple[int, ...] = (512, 1024, 2048),
    normalise: bool = True,
) -> float:
    """Multi-resolution STFT loss (spectral convergence + log-magnitude L1).

    Looks at several time/frequency trade-offs at once, so it catches both
    transient and sustained differences. Standard in neural audio-effect
    modelling; ``auraloss`` implements the reference version.

    Aligned signals only.
    """
    target = to_mono(target)
    prediction = to_mono(prediction)
    target, prediction = match_length(target, prediction)
    if normalise:
        target = normalise_rms(target)
        prediction = normalise_rms(prediction)

    losses = []
    for n_fft in fft_sizes:
        hop = max(1, n_fft // 4)
        if len(target) < n_fft:
            continue
        target_mag = _stft_magnitude(target, n_fft, hop)
        pred_mag = _stft_magnitude(prediction, n_fft, hop)
        convergence = spectral_convergence(target_mag, pred_mag)
        log_l1 = float(
            np.mean(np.abs(np.log(target_mag + EPSILON) - np.log(pred_mag + EPSILON)))
        )
        losses.append(convergence + log_l1)

    if not losses:
        return float("nan")
    return float(np.mean(losses))


# --------------------------------------------------------------------------
# distribution metrics
# --------------------------------------------------------------------------


def _hz_to_mel(hz: np.ndarray | float) -> np.ndarray | float:
    return 2595.0 * np.log10(1.0 + np.asarray(hz) / 700.0)


def _mel_to_hz(mel: np.ndarray | float) -> np.ndarray | float:
    return 700.0 * (10.0 ** (np.asarray(mel) / 2595.0) - 1.0)


def mel_filterbank(
    sample_rate: int, n_fft: int, n_mels: int = 40, fmin: float = 20.0, fmax: float | None = None
) -> np.ndarray:
    """Triangular mel filterbank, shape ``(n_mels, n_fft // 2 + 1)``."""
    fmax = fmax or sample_rate / 2
    mel_points = np.linspace(_hz_to_mel(fmin), _hz_to_mel(fmax), n_mels + 2)
    hz_points = _mel_to_hz(mel_points)
    bins = np.floor((n_fft + 1) * hz_points / sample_rate).astype(int)
    bins = np.clip(bins, 0, n_fft // 2)

    filters = np.zeros((n_mels, n_fft // 2 + 1))
    for i in range(n_mels):
        left, centre, right = bins[i], bins[i + 1], bins[i + 2]
        if centre == left:
            centre = left + 1
        if right <= centre:
            right = centre + 1
        # Clamp rather than break: bailing out left the remaining high
        # bands as all-zero rows, which silently drops the top of the
        # spectrum from every MFCC on the fallback path.
        right = min(right, n_fft // 2)
        if right <= centre:
            continue
        filters[i, left:centre] = np.linspace(0, 1, centre - left, endpoint=False)
        filters[i, centre:right] = np.linspace(1, 0, right - centre, endpoint=False)
    return filters


def mfcc(
    signal: np.ndarray,
    sample_rate: int,
    *,
    n_mfcc: int = 13,
    n_fft: int = 2048,
    n_mels: int = 40,
) -> np.ndarray:
    """MFCCs, shape ``(n_mfcc, frames)``.

    Deliberately self-contained rather than delegating to librosa when it
    happens to be installed. This once used librosa if available, and the two
    implementations disagreed enough to change the *ranking* of candidates -
    so the same comparison gave different answers depending on which optional
    extras a user had. A metric whose result depends on the environment is
    worse than a slightly less canonical one, because the whole purpose here
    is comparing candidates against each other.

    The absolute values are not meant to match librosa's, and nothing in
    rigsmith compares them to anything outside itself.
    """
    signal = to_mono(signal)
    hop = n_fft // 4
    magnitude = _stft_magnitude(signal, n_fft, hop)
    power = magnitude**2
    filters = mel_filterbank(sample_rate, n_fft, n_mels)
    mel_energy = power @ filters.T
    log_mel = np.log(mel_energy + EPSILON)

    from scipy.fftpack import dct

    coefficients = dct(log_mel, type=2, axis=-1, norm="ortho")[:, :n_mfcc]
    return coefficients.T


def mfcc_distance(
    a: np.ndarray, b: np.ndarray, sample_rate: int, *, n_mfcc: int = 13
) -> float:
    """Distance between the MFCC *distributions* of two signals.

    Compares per-coefficient means and standard deviations rather than
    frame-by-frame, so it does not require the performances to line up.
    """
    a_mfcc = mfcc(a, sample_rate, n_mfcc=n_mfcc)
    b_mfcc = mfcc(b, sample_rate, n_mfcc=n_mfcc)

    a_stats = np.concatenate([a_mfcc.mean(axis=1), a_mfcc.std(axis=1)])
    b_stats = np.concatenate([b_mfcc.mean(axis=1), b_mfcc.std(axis=1)])
    scale = np.maximum(np.abs(a_stats), 1.0)
    return float(np.mean(np.abs(a_stats - b_stats) / scale))


@dataclass
class SpectralSummary:
    """Timbre descriptors averaged over a signal."""

    centroid_hz: float
    rolloff_hz: float
    flatness: float
    bandwidth_hz: float
    crest_factor: float
    rms: float

    def distance(self, other: "SpectralSummary") -> float:
        """Scale-aware distance to another summary, 0 = identical."""
        pairs = [
            (self.centroid_hz, other.centroid_hz, 1000.0),
            (self.rolloff_hz, other.rolloff_hz, 2000.0),
            (self.flatness, other.flatness, 0.1),
            (self.bandwidth_hz, other.bandwidth_hz, 1000.0),
            (self.crest_factor, other.crest_factor, 2.0),
        ]
        return float(np.mean([abs(a - b) / scale for a, b, scale in pairs]))

    def render(self) -> str:
        return (
            f"centroid {self.centroid_hz:7.1f} Hz  "
            f"rolloff85 {self.rolloff_hz:7.1f} Hz  "
            f"flatness {self.flatness:.4f}  "
            f"bandwidth {self.bandwidth_hz:7.1f} Hz  "
            f"crest {self.crest_factor:.2f}"
        )


def spectral_summary(
    signal: np.ndarray,
    sample_rate: int,
    *,
    n_fft: int = 2048,
    rolloff_percent: float = 0.85,
) -> SpectralSummary:
    """Centroid, rolloff, flatness, bandwidth, and crest factor.

    These describe where the energy sits and how peaky it is - brightness,
    fizz, and compression, roughly. Timing-independent, so they work against
    a commercial recording.
    """
    signal = to_mono(signal)
    if len(signal) < n_fft:
        signal = np.pad(signal, (0, n_fft - len(signal)))

    hop = n_fft // 4
    magnitude = _stft_magnitude(signal, n_fft, hop)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    total = magnitude.sum(axis=1, keepdims=True)
    safe_total = np.where(total < EPSILON, EPSILON, total)
    probability = magnitude / safe_total

    centroid = float(np.mean(probability @ freqs))

    cumulative = np.cumsum(probability, axis=1)
    rolloff_idx = np.argmax(cumulative >= rolloff_percent, axis=1)
    rolloff = float(np.mean(freqs[rolloff_idx]))

    geometric = np.exp(np.mean(np.log(magnitude + EPSILON), axis=1))
    arithmetic = np.mean(magnitude, axis=1) + EPSILON
    flatness = float(np.mean(geometric / arithmetic))

    deviation = freqs[None, :] - (probability @ freqs)[:, None]
    bandwidth = float(np.mean(np.sqrt(np.sum(probability * deviation**2, axis=1))))

    rms = float(np.sqrt(np.mean(signal**2)))
    peak = float(np.max(np.abs(signal)))
    crest = peak / rms if rms > EPSILON else 0.0

    return SpectralSummary(
        centroid_hz=centroid,
        rolloff_hz=rolloff,
        flatness=flatness,
        bandwidth_hz=bandwidth,
        crest_factor=crest,
        rms=rms,
    )


def log_spectral_distance(
    a: np.ndarray, b: np.ndarray, sample_rate: int, *, n_fft: int = 2048
) -> float:
    """Distance between the two signals' average log spectra, in dB.

    Timing-independent: both spectra are averaged over time first, so this
    answers "does it have the same frequency balance" rather than "does it
    do the same thing at the same moment".
    """
    a = normalise_rms(to_mono(a))
    b = normalise_rms(to_mono(b))

    hop = n_fft // 4
    a_spectrum = _stft_magnitude(a, n_fft, hop).mean(axis=0)
    b_spectrum = _stft_magnitude(b, n_fft, hop).mean(axis=0)

    a_db = 20 * np.log10(a_spectrum + EPSILON)
    b_db = 20 * np.log10(b_spectrum + EPSILON)
    return float(np.sqrt(np.mean((a_db - b_db) ** 2)))


# --------------------------------------------------------------------------
# where two signals differ
# --------------------------------------------------------------------------

#: Bands named the way guitarists talk about them, not the way a
#: third-octave analyser does. The point is to be actionable: "too much fizz"
#: leads somewhere, "energy elevated in band 27" does not.
GUITAR_BANDS: tuple[tuple[str, float, float, str], ...] = (
    ("sub", 20, 80, "rumble below the low E - usually the cab IR or a high-pass"),
    ("low end", 80, 180, "weight and thump - amp bass, or cab choice"),
    ("body", 180, 400, "the note's fundamental through most of the neck"),
    ("mud", 400, 700, "boxiness; a little goes a long way"),
    ("mids", 700, 1600, "where the guitar sits in a mix - amp mids"),
    ("presence", 1600, 3200, "attack and cut - amp presence, mic position"),
    ("bite", 3200, 6000, "pick definition and edge"),
    ("fizz", 6000, 11000, "buzz on top of distortion - cab IR and mic more than amp"),
    ("air", 11000, 18000, "openness; often mostly the recording"),
)

#: Below this a difference is not worth acting on. Roughly the point at which
#: an A/B becomes hard rather than obvious.
AUDIBLE_DB = 1.5


@dataclass(frozen=True)
class BandDifference:
    """How one band of a candidate compares with the target."""

    name: str
    low: float
    high: float
    delta_db: float
    """Positive means the candidate has more energy here than the target."""
    meaning: str

    @property
    def audible(self) -> bool:
        return abs(self.delta_db) >= AUDIBLE_DB

    def describe(self) -> str:
        direction = "too much" if self.delta_db > 0 else "not enough"
        return (
            f"{self.name:9} {self.delta_db:+5.1f} dB  {direction} "
            f"({self.low:.0f}-{self.high:.0f} Hz) - {self.meaning}"
        )


def band_differences(
    target: np.ndarray,
    candidate: np.ndarray,
    sample_rate: int,
    *,
    n_fft: int = 2048,
) -> list[BandDifference]:
    """Compare two signals band by band and say where they differ.

    A composite score ranks; this says what to change. Both signals are
    RMS-normalised first, so the answer is about tone rather than about
    whichever was recorded louder.

    It is a description of a measured difference, not a diagnosis. The
    suggested causes are conventional, and conventional is not always right -
    a bright candidate can be the mic position, the cab, the amp's presence
    control, or the fact that the target is a finished mix with a de-esser
    on it.
    """
    target = normalise_rms(to_mono(np.asarray(target, dtype=np.float64)))
    candidate = normalise_rms(to_mono(np.asarray(candidate, dtype=np.float64)))

    hop = n_fft // 4
    target_mag = _stft_magnitude(target, n_fft, hop).mean(axis=0)
    candidate_mag = _stft_magnitude(candidate, n_fft, hop).mean(axis=0)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    nyquist = sample_rate / 2
    results: list[BandDifference] = []
    for name, low, high, meaning in GUITAR_BANDS:
        if low >= nyquist:
            continue
        mask = (freqs >= low) & (freqs < min(high, nyquist))
        if not mask.any():
            continue
        target_energy = float(target_mag[mask].mean()) + 1e-12
        candidate_energy = float(candidate_mag[mask].mean()) + 1e-12
        results.append(
            BandDifference(
                name=name,
                low=low,
                high=min(high, nyquist),
                delta_db=20 * math.log10(candidate_energy / target_energy),
                meaning=meaning,
            )
        )
    return results


def render_band_differences(
    differences: list[BandDifference], *, label: str = "candidate"
) -> str:
    """Say where a candidate differs from the target, loudest first."""
    if not differences:
        return "Nothing to compare."

    audible = [d for d in differences if d.audible]
    if not audible:
        return (
            f"{label} sits within {AUDIBLE_DB} dB of the target in every band. "
            "Whatever is still wrong is not a broad tonal balance problem - "
            "look at feel, dynamics, and note attack, which none of this "
            "measures."
        )

    audible.sort(key=lambda d: abs(d.delta_db), reverse=True)
    lines = [f"{label} against the target, biggest differences first:", ""]
    lines.extend(f"  {d.describe()}" for d in audible)
    lines.append(
        "\nThese are measured differences with a conventional reading attached. "
        "The reading can be wrong: a bright candidate might be the mic "
        "position, the cab, the presence control, or a finished mix that had "
        "treble taken off it. Change one thing and measure again."
    )
    return "\n".join(lines)

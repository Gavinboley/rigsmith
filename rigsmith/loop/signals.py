"""Test signals, and finding them again.

Two tools, both standard and both worth getting exactly right:

**The exponential sine sweep** (Farina 2000, AES 108). Played through a
system and deconvolved with its own time-reversed, amplitude-compensated
inverse, it yields the system's linear impulse response - and, because each
harmonic of a swept sine arrives *earlier* than the fundamental by a fixed
log-frequency offset, the distortion products land as separate, earlier
impulses instead of polluting the linear one. So one sweep gives latency,
polarity, frequency response and a distortion estimate at once, even through
an amp that is distorting.

**GCC-PHAT** (Knapp & Carter 1976). Cross-correlation with every frequency
weighted to unit magnitude, so the peak is set by phase alignment across the
whole band rather than by whichever frequency happens to be loudest. It finds
a delay to the sample through distortion, EQ and noise that would smear a
plain correlation into a hill.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def exponential_sweep(
    seconds: float,
    sample_rate: int,
    *,
    f_start: float = 20.0,
    f_end: float | None = None,
    level_db: float = -12.0,
    fade_s: float = 0.02,
) -> np.ndarray:
    """A log sine sweep with short fades, at ``level_db`` dBFS peak."""
    f_end = f_end or min(20000.0, 0.45 * sample_rate)
    length = int(seconds * sample_rate)
    t = np.arange(length) / sample_rate
    rate = math.log(f_end / f_start)
    sweep = np.sin(2 * np.pi * f_start * seconds / rate * (np.exp(t * rate / seconds) - 1.0))
    fade = int(fade_s * sample_rate)
    if fade > 1 and 2 * fade < length:
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade))
        sweep[:fade] *= ramp
        sweep[-fade:] *= ramp[::-1]
    return sweep * 10 ** (level_db / 20)


def inverse_sweep(sweep: np.ndarray, sample_rate: int, *, f_start: float = 20.0, f_end: float | None = None) -> np.ndarray:
    """Farina's inverse filter: time-reversed, with a -6 dB/octave tilt.

    The sweep spends longer at low frequencies, so it carries more energy
    there; the tilt takes that back out, leaving a flat deconvolution.
    """
    seconds = len(sweep) / sample_rate
    f_end = f_end or min(20000.0, 0.45 * sample_rate)
    rate = math.log(f_end / f_start)
    t = np.arange(len(sweep)) / sample_rate
    envelope = np.exp(-t * rate / seconds)
    inverse = sweep[::-1] * envelope
    # Normalise so a perfect loop deconvolves to a unit impulse.
    response = np.fft.irfft(np.fft.rfft(sweep, 2 * len(sweep)) * np.fft.rfft(inverse, 2 * len(sweep)))
    peak = float(np.max(np.abs(response))) or 1.0
    return inverse / peak


def deconvolve(recorded: np.ndarray, sweep: np.ndarray, inverse: np.ndarray) -> np.ndarray:
    """Impulse response of whatever the sweep passed through.

    The linear response's peak sits at index ``len(sweep) - 1 + latency``;
    harmonic distortion arrives before it.
    """
    size = 1 << int(math.ceil(math.log2(len(recorded) + len(inverse))))
    return np.fft.irfft(np.fft.rfft(recorded, size) * np.fft.rfft(inverse, size), size)[: len(recorded) + len(inverse) - 1]


def gcc_phat(signal: np.ndarray, reference: np.ndarray, *, max_lag: int | None = None, interp: int = 1) -> tuple[float, float, float]:
    """Delay of ``signal`` relative to ``reference``, in samples.

    Returns ``(delay, peak_value, sharpness)``. ``peak_value`` is signed - a
    negative peak means the signal is inverted. ``sharpness`` is the peak over
    the median absolute correlation: below about 8 the answer is a guess.
    """
    n = len(signal) + len(reference)
    size = 1 << int(math.ceil(math.log2(n)))
    a = np.fft.rfft(signal, size)
    b = np.fft.rfft(reference, size)
    cross = a * np.conj(b)
    cross /= np.maximum(np.abs(cross), 1e-12)
    correlation = np.fft.irfft(cross, size * interp)
    shift = size * interp
    max_shift = shift // 2 if max_lag is None else min(shift // 2, int(max_lag * interp))
    correlation = np.concatenate((correlation[-max_shift:], correlation[: max_shift + 1]))
    index = int(np.argmax(np.abs(correlation)))
    peak = float(correlation[index])
    # Sub-sample refinement on the magnitude.
    delay = float(index - max_shift)
    if 0 < index < len(correlation) - 1:
        before, centre, after = np.abs(correlation[index - 1 : index + 2])
        denominator = before - 2 * centre + after
        if denominator:
            delay += 0.5 * (before - after) / denominator
    median = float(np.median(np.abs(correlation))) or 1e-12
    return delay / interp, peak, abs(peak) / median


@dataclass
class SweepAnalysis:
    """What one sweep through a system says about it."""

    latency_samples: float
    polarity: int
    """+1 or -1."""
    response_db: list[float]
    """Linear magnitude response at :data:`ANALYSIS_BANDS`, relative to 1 kHz."""
    thd_percent: float
    """Second-to-fifth harmonic energy over the linear response, as a percent."""
    snr_db: float
    """Impulse peak over the impulse response's own noise floor."""
    gain_db: float
    """Loop gain at 1 kHz."""

    def to_dict(self) -> dict:
        return {
            "latency_samples": round(self.latency_samples, 2),
            "polarity": self.polarity,
            "response_db": [round(v, 2) for v in self.response_db],
            "bands_hz": list(ANALYSIS_BANDS),
            "thd_percent": round(self.thd_percent, 2),
            "snr_db": round(self.snr_db, 1),
            "gain_db": round(self.gain_db, 2),
        }


ANALYSIS_BANDS = (40, 63, 100, 160, 250, 400, 630, 1000, 1600, 2500, 4000, 6300, 10000, 16000)


def analyse_sweep(
    recorded: np.ndarray,
    sweep: np.ndarray,
    sample_rate: int,
    *,
    f_start: float = 20.0,
    f_end: float | None = None,
) -> SweepAnalysis:
    """Latency, polarity, response and distortion from one recorded sweep."""
    inverse = inverse_sweep(sweep, sample_rate, f_start=f_start, f_end=f_end)
    ir = deconvolve(np.asarray(recorded, dtype=np.float64), sweep, inverse)
    origin = len(sweep) - 1
    linear = ir[origin:]
    peak_index = int(np.argmax(np.abs(linear[: min(len(linear), sample_rate)])))
    peak = float(linear[peak_index])
    latency = float(peak_index)
    if 0 < peak_index < len(linear) - 1:
        before, centre, after = np.abs(linear[peak_index - 1 : peak_index + 2])
        denominator = before - 2 * centre + after
        if denominator:
            latency += 0.5 * (before - after) / denominator

    # The linear IR: a window around the peak. Harmonics live before it.
    pre = int(0.002 * sample_rate)
    post = int(0.1 * sample_rate)
    start = max(0, peak_index - pre)
    window = linear[start : peak_index + post]
    spectrum = np.abs(np.fft.rfft(window, 1 << 15))
    freqs = np.fft.rfftfreq(1 << 15, 1 / sample_rate)
    reference = float(np.interp(1000.0, freqs, spectrum)) or 1e-12
    response = [20 * math.log10(max(float(np.interp(b, freqs, spectrum)), 1e-12) / reference) for b in ANALYSIS_BANDS]

    # Harmonic k arrives L * ln(k) / ln(f_end/f_start) seconds early.
    f_end_used = f_end or min(20000.0, 0.45 * sample_rate)
    seconds = len(sweep) / sample_rate
    harmonic_energy = 0.0
    linear_energy = float(np.sum(window ** 2)) or 1e-20
    for k in range(2, 6):
        advance = int(seconds * math.log(k) / math.log(f_end_used / f_start) * sample_rate)
        centre = origin + peak_index - advance
        a, b = max(0, centre - pre), max(0, centre + int(0.01 * sample_rate))
        harmonic_energy += float(np.sum(ir[a:b] ** 2))
    thd = 100.0 * math.sqrt(harmonic_energy / linear_energy)

    tail = linear[peak_index + int(0.3 * sample_rate) : peak_index + int(0.6 * sample_rate)]
    noise = float(np.sqrt(np.mean(tail ** 2))) if len(tail) else 1e-12
    snr = 20 * math.log10(abs(peak) / max(noise, 1e-12))
    gain_db = 20 * math.log10(reference)

    return SweepAnalysis(
        latency_samples=latency,
        polarity=1 if peak >= 0 else -1,
        response_db=response,
        thd_percent=thd,
        snr_db=snr,
        gain_db=gain_db,
    )


def sync_mark(sample_rate: int, *, seconds: float = 0.2, level_db: float = -20.0, seed: int = 1729) -> np.ndarray:
    """The short burst put at each end of a capture to find it again.

    Gaussian noise, band-limited to 200 Hz - 6 kHz, rather than a sweep. The
    mark goes through the rig, and the rig distorts; by Bussgang's theorem a
    Gaussian signal through a memoryless nonlinearity still cross-correlates
    with its original at the true delay, so the peak GCC-PHAT finds is the
    right one even through a high-gain amp. A distorted sweep instead grows
    harmonic copies of itself that correlate tens of milliseconds early.
    The band keeps it inside what any cab IR or gate lets through.
    """
    from scipy.signal import butter, sosfiltfilt

    rng = np.random.default_rng(seed)
    length = int(seconds * sample_rate)
    noise = rng.normal(0.0, 1.0, length + 2048)
    sos = butter(4, [200.0, min(6000.0, 0.45 * sample_rate)], btype="bandpass", fs=sample_rate, output="sos")
    burst = sosfiltfilt(sos, noise)[1024 : 1024 + length]
    fade = int(0.005 * sample_rate)
    if fade > 1 and 2 * fade < length:
        ramp = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, fade))
        burst[:fade] *= ramp
        burst[-fade:] *= ramp[::-1]
    peak = float(np.max(np.abs(burst))) or 1.0
    return burst / peak * 10 ** (level_db / 20)


def estimate_drift(first_delay: float, second_delay: float, span_samples: float) -> float:
    """Clock drift in parts per million from two sync positions.

    If the second mark arrives later than the first by more than the gap that
    was played between them, the recording clock runs fast relative to the
    playback clock.
    """
    if span_samples <= 0:
        return 0.0
    return 1e6 * (second_delay - first_delay) / span_samples


def resample_by(audio: np.ndarray, ratio: float) -> np.ndarray:
    """Stretch by ``ratio`` (output length = input length * ratio)."""
    if abs(ratio - 1.0) < 1e-9:
        return audio
    from fractions import Fraction

    from scipy.signal import resample_poly

    fraction = Fraction(ratio).limit_denominator(1_000_000)
    if fraction.numerator * fraction.denominator > 10_000_000_000:
        positions = np.arange(int(len(audio) * ratio)) / ratio
        return np.interp(positions, np.arange(len(audio)), audio)
    return resample_poly(audio, fraction.numerator, fraction.denominator)


def fractional_shift(audio: np.ndarray, samples: float) -> np.ndarray:
    """Delay (positive) or advance (negative) by a fractional number of samples."""
    whole = int(math.floor(samples))
    fraction = samples - whole
    out = np.roll(audio, whole) if whole else audio.copy()
    if whole > 0:
        out[:whole] = 0.0
    elif whole < 0:
        out[whole:] = 0.0
    if fraction > 1e-6:
        size = 1 << int(math.ceil(math.log2(len(out) * 2)))
        freqs = np.fft.rfftfreq(size)
        spectrum = np.fft.rfft(out, size) * np.exp(-2j * np.pi * freqs * fraction)
        out = np.fft.irfft(spectrum, size)[: len(audio)]
    return out

"""How close a loop capture is to what it was meant to be.

The question has a different honest answer depending on what the capture is
being compared with, and this module asks which before answering:

* **The same DI through something else** - an offline render of the same
  capture, a previous pass, a reference rig. Same performance, so the
  sample-aligned metrics apply: ESR, null depth, multi-resolution STFT. First
  the two are fine-aligned, because the sync marks travel through the rig and
  pick up its group delay - a few samples, constant, and not tone. The delay
  removed is reported, so a large one is visible rather than absorbed.
* **A record's stem** - a different performance. Then it is
  :func:`rigsmith.core.matching.score` in distribution mode, or performance
  mode if the DI was rendered from the record's own notes.

The verdict bands for ESR are the ones the NAM community uses for captures:
under 0.01 indistinguishable, under 0.035 very close, under 0.1 usable,
above that something is genuinely different.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..match.metrics import band_differences, esr, multi_resolution_stft_loss, normalise_rms, to_mono
from .signals import fractional_shift, gcc_phat

#: (upper ESR, verdict) - NAM's capture-quality language.
ESR_VERDICTS = (
    (0.01, "indistinguishable - under 0.01, where NAM captures are called great"),
    (0.035, "very close - the 0.01-0.035 band"),
    (0.1, "usable but audibly different - 0.035-0.1"),
    (0.3, "clearly different - 0.1-0.3"),
    (math.inf, "a different sound, or something went wrong (level, clipping, routing)"),
)

#: The furthest a fine alignment will move one signal against the other.
MAX_FINE_SHIFT = 256


def verdict(value: float) -> str:
    for ceiling, words in ESR_VERDICTS:
        if value < ceiling:
            return words
    return ESR_VERDICTS[-1][1]


def fine_align(reference: np.ndarray, signal: np.ndarray, *, max_shift: int = MAX_FINE_SHIFT) -> tuple[np.ndarray, float, int]:
    """Shift ``signal`` onto ``reference``; return ``(shifted, delay, polarity)``."""
    n = min(len(reference), len(signal))
    reference, signal = reference[:n], signal[:n]
    delay, peak, _ = gcc_phat(signal, reference, max_lag=max_shift)
    polarity = 1 if peak >= 0 else -1
    shifted = fractional_shift(signal, -delay) * polarity
    return shifted, float(delay), polarity


@dataclass
class Assessment:
    """A capture against a same-performance target."""

    esr: float
    null_db: float
    """How far the difference sits below the target. -40 dB is close."""
    stft: float
    delay_samples: float
    polarity: int
    verdict: str
    bands: list = field(default_factory=list)
    level_difference_db: float = 0.0
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"ESR {self.esr:.4f}: {self.verdict}",
            f"  null {self.null_db:.1f} dB, multi-res STFT {self.stft:.3f}",
            f"  fine-aligned by {self.delay_samples:+.2f} samples"
            + (" and polarity-flipped" if self.polarity < 0 else "")
            + " (constant delay is not tone)",
        ]
        if abs(self.level_difference_db) >= 0.5:
            lines.append(f"  level differs by {self.level_difference_db:+.1f} dB (compared at matched loudness)")
        audible = [b for b in self.bands if b.audible]
        audible.sort(key=lambda b: abs(b.delta_db), reverse=True)
        if audible:
            lines.append("  where it differs: " + ", ".join(f"{b.name} {b.delta_db:+.1f} dB" for b in audible[:4]))
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "esr": self.esr,
            "null_db": self.null_db,
            "stft": self.stft,
            "delay_samples": self.delay_samples,
            "polarity": self.polarity,
            "verdict": self.verdict,
            "level_difference_db": self.level_difference_db,
            "bands": [
                {"name": b.name, "delta_db": b.delta_db, "audible": b.audible, "meaning": b.meaning}
                for b in self.bands
            ],
            "notes": self.notes,
            "render": self.render(),
        }


def assess(target: np.ndarray, capture: np.ndarray, sample_rate: int) -> Assessment:
    """Sample-aligned comparison of two renditions of the same DI."""
    target = to_mono(np.asarray(target, dtype=np.float64))
    capture = to_mono(np.asarray(capture, dtype=np.float64))
    n = min(len(target), len(capture))
    if n < sample_rate // 10:
        raise ValueError("too little audio to assess - under 100 ms in common")
    target, capture = target[:n], capture[:n]

    t_rms = float(np.sqrt(np.mean(target ** 2))) or 1e-12
    c_rms = float(np.sqrt(np.mean(capture ** 2))) or 1e-12
    level = 20 * math.log10(c_rms / t_rms)

    shifted, delay, polarity = fine_align(target, capture)
    # Compare away from the very ends, where a fractional shift wraps.
    edge = min(MAX_FINE_SHIFT, n // 10)
    t_body = normalise_rms(target[edge : n - edge])
    c_body = normalise_rms(shifted[edge : n - edge])
    value = esr(t_body, c_body, normalise=False)
    residual = t_body - c_body
    null = 10 * math.log10(max(float(np.sum(residual ** 2)), 1e-20) / max(float(np.sum(t_body ** 2)), 1e-20))
    stft = multi_resolution_stft_loss(t_body, c_body)

    result = Assessment(
        esr=float(value),
        null_db=float(null),
        stft=float(stft),
        delay_samples=delay,
        polarity=polarity,
        verdict=verdict(value),
        bands=band_differences(t_body, c_body, sample_rate),
        level_difference_db=float(level),
    )
    if abs(delay) > 64:
        result.notes.append(
            f"a {delay:+.0f}-sample offset was removed - larger than a rig's own "
            "delay, so check the two really are the same DI"
        )
    if polarity < 0:
        result.notes.append("one of the two is polarity-inverted; it was flipped for the comparison")
    return result

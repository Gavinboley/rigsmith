"""Tone matching: render candidates and measure how close they get.

Three layers, usable independently:

1. **Offline simulation** (:mod:`rigsmith.match.render_offline`) - run a DI
   through a NAM capture and cab IR in Python. Fast, no hardware, approximate.
2. **Hardware in the loop** (:mod:`rigsmith.match.render_device`) - reamp a DI
   through the actual Core over USB and record the result. Slow, and the only
   thing that is genuinely the device's tone.
3. **Human blind A/B** (:mod:`rigsmith.match.ab`) - the arbiter. Metrics rank
   candidates; ears decide.

See :mod:`rigsmith.match.score` for why the distinction between *aligned* and
*unaligned* comparison is enforced rather than left to the caller.
"""

from .metrics import (
    ComparisonMode,
    MetricResult,
    esr,
    log_spectral_distance,
    mfcc,
    multi_resolution_stft_loss,
    spectral_summary,
)

__all__ = [
    "ComparisonMode",
    "MetricResult",
    "esr",
    "log_spectral_distance",
    "mfcc",
    "multi_resolution_stft_loss",
    "spectral_summary",
]

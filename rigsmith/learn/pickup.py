"""The pickup, as the circuit it is - so one guitar can be heard as another.

A passive magnetic pickup is a coil (a few henries) in series with its own
resistance, loaded by the cable's capacitance and the volume pot: a
second-order low-pass with a resonant peak. That peak - where it sits and how
sharp it is - is most of what separates a Strat's single coil (high and
sharp, around 4-5 kHz on a normal cable) from a humbucker (lower and broader,
around 2-3 kHz). The strings, the wood and the hands matter too, but they are
not what makes a humbucker DI sound like a humbucker.

So two things are possible from a DI alone:

- **Estimate** where your pickup's resonance is, by fitting that low-pass
  (plus a tilt for the strings) to the long-term spectrum of what you played.
- **Swap** it: divide your resonance out and put another in - a named type
  ("strat", "p90", "paf", "emg 81"), or another learned guitar - all the way,
  or any amount in between, since the two parameters morph smoothly.

That is the honest scope. It moves the one feature that dominates a clean DI
and leaves the rest alone, which is why ``amount`` exists: half way from a
Les Paul to a Strat is a real, useful sound, and the ear is the judge of how
far to go.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

import numpy as np

#: Fit range. Below 150 Hz is the fundamentals of whatever was played, above
#: 12 kHz is noise; the resonance always lives between.
FIT_LOW_HZ = 150.0
FIT_HIGH_HZ = 12000.0

#: How far the swap may push any frequency - beyond this it is inventing
#: content that was never in the DI (or digging out hiss that was).
MAX_CORRECTION_DB = 15.0


@dataclass(frozen=True)
class PickupType:
    name: str
    f0_hz: float
    q: float
    description: str
    aliases: tuple[str, ...] = ()


#: Typical resonances on a ~3 m cable into a high-impedance input. These are
#: the centre of each family, not any one pickup: a bridge pickup sits lower
#: than the neck one of the same type, a longer cable pulls every one down.
PICKUPS: dict[str, PickupType] = {
    p.name: p
    for p in (
        PickupType("strat", 4600, 2.6, "single coil, Stratocaster style", ("stratocaster", "single coil", "single-coil", "sss", "fender")),
        PickupType("tele", 5200, 2.4, "Telecaster bridge single coil", ("telecaster", "tele bridge", "broadcaster")),
        PickupType("p90", 3600, 1.9, "P-90 soapbar/dog-ear", ("p-90", "p 90", "soapbar", "dog ear", "dog-ear", "les paul junior", "les paul special")),
        PickupType("paf", 2900, 1.6, "vintage-output humbucker (PAF style)", ("humbucker", "humbuckers", "les paul", "sg", "es-335", "335", "gibson", "'57 classic", "57 classic", "burstbucker", "seth lover")),
        PickupType("hot-humbucker", 2300, 1.3, "high-output passive humbucker", ("jb", "distortion", "super distortion", "dimarzio", "duncan", "invader", "x2n", "high output", "high-output", "500t", "dirty fingers")),
        PickupType("active", 7500, 0.9, "active humbucker (EMG 81/85 style)", ("emg", "emg 81", "emg 85", "81", "85", "fishman", "fluence", "active")),
        PickupType("filtertron", 4200, 1.8, "Filter'Tron / Gretsch-style humbucker", ("filter'tron", "filtertron", "gretsch", "tv jones")),
    )
}


@dataclass
class Resonance:
    """A pickup's resonant low-pass, measured or named."""

    f0_hz: float
    q: float
    tilt_db_per_octave: float = 0.0
    fit_error_db: float = 0.0
    """RMS distance between the model and the spectrum it was fitted to."""
    label: str = ""

    @property
    def reads_as(self) -> str:
        """The named type this resonance is closest to."""
        def distance(p: PickupType) -> float:
            return abs(np.log2(self.f0_hz / p.f0_hz)) * 2 + abs(np.log2(self.q / p.q))

        return min(PICKUPS.values(), key=distance).name

    @property
    def clear(self) -> bool:
        """Whether there was a peak to measure at all: an active pickup, or a
        DI that went through an EQ on the way, has none."""
        return self.q >= 1.0 and self.fit_error_db < 4.0

    def describe(self) -> str:
        text = f"resonance {self.f0_hz / 1000:.1f} kHz, Q {self.q:.1f}"
        prefix = f"{self.label}: " if self.label else ""
        if self.label in PICKUPS or self.label == "flat":
            return prefix + text
        if not self.clear:
            return f"{prefix}{text} - no clear peak (active pickup, or an EQ'd DI)"
        return f"{prefix}{text} - reads as {PICKUPS[self.reads_as].description}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["reads_as"] = self.reads_as
        data["clear"] = self.clear
        data["describe"] = self.describe()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Resonance":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


FLAT = Resonance(f0_hz=20000.0, q=0.707, label="flat")


def response_db(freqs, f0_hz: float, q: float) -> np.ndarray:
    """|H| in dB of a second-order low-pass with unity gain at DC."""
    ratio = np.asarray(freqs, dtype=np.float64) / float(f0_hz)
    magnitude = 1.0 / np.sqrt((1 - ratio**2) ** 2 + (ratio / float(q)) ** 2)
    return 20 * np.log10(np.maximum(magnitude, 1e-12))


def named(text: str) -> Resonance | None:
    """A resonance for a description - "Fender Stratocaster", "P-90", "EMG 81"."""
    lowered = (text or "").lower().strip()
    if not lowered:
        return None
    if lowered in PICKUPS:
        kind = PICKUPS[lowered]
        return Resonance(kind.f0_hz, kind.q, label=kind.name)
    # Longest alias first, so "les paul junior" is a P-90 before "les paul" is a PAF.
    pairs = sorted(((alias, kind) for kind in PICKUPS.values() for alias in kind.aliases), key=lambda pair: -len(pair[0]))
    for alias, kind in pairs:
        if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", lowered):
            return Resonance(kind.f0_hz, kind.q, label=kind.name)
    return None


def _spectrum(audio: np.ndarray, sample_rate: int) -> tuple[np.ndarray, np.ndarray]:
    """Long-term average spectrum in dB, on a 1/6-octave grid."""
    from scipy.signal import welch

    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    nperseg = min(8192, max(256, len(audio)))
    freqs, power = welch(audio, fs=sample_rate, nperseg=nperseg)
    grid = FIT_LOW_HZ * 2 ** (np.arange(0, np.log2(min(FIT_HIGH_HZ, sample_rate / 2.2) / FIT_LOW_HZ), 1 / 6))
    levels = []
    for centre in grid:
        band = (freqs >= centre * 2 ** (-1 / 12)) & (freqs < centre * 2 ** (1 / 12))
        levels.append(10 * np.log10(np.mean(power[band]) + 1e-20) if band.any() else np.nan)
    levels = np.asarray(levels)
    keep = np.isfinite(levels)
    return grid[keep], levels[keep]


def fit(freqs, levels_db, *, label: str = "") -> Resonance:
    """The low-pass (and string tilt) that best explains a spectrum.

    A grid over resonance and Q, with tilt and level solved exactly for each
    point - small enough to be exhaustive, so there is no local minimum to
    land in.
    """
    freqs = np.asarray(freqs, dtype=np.float64)
    levels = np.asarray(levels_db, dtype=np.float64)
    keep = (freqs >= FIT_LOW_HZ) & (freqs <= FIT_HIGH_HZ) & np.isfinite(levels)
    freqs, levels = freqs[keep], levels[keep]
    if len(freqs) < 6:
        return Resonance(FLAT.f0_hz, FLAT.q, fit_error_db=99.0, label=label)
    octaves = np.log2(freqs / 1000.0)
    design = np.column_stack([octaves, np.ones_like(octaves)])
    best = (np.inf, FLAT.f0_hz, FLAT.q, 0.0)
    for f0 in np.geomspace(1200, 11000, 72):
        for q in np.geomspace(0.5, 5.0, 40):
            residual = levels - response_db(freqs, f0, q)
            coef, *_ = np.linalg.lstsq(design, residual, rcond=None)
            error = float(np.sqrt(np.mean((residual - design @ coef) ** 2)))
            if error < best[0]:
                best = (error, f0, q, float(coef[0]))
    error, f0, q, tilt = best
    return Resonance(round(float(f0), 0), round(float(q), 2), round(tilt, 2), round(error, 2), label)


def estimate(audio: np.ndarray, sample_rate: int) -> Resonance:
    """Where the pickup that recorded ``audio`` resonates."""
    return fit(*_spectrum(audio, sample_rate))


def estimate_from_bands(bands_hz, bands_db) -> Resonance:
    """The same, from a profile's third-octave fingerprint - no audio needed."""
    return fit(bands_hz, bands_db)


def morph(source: Resonance, target: Resonance, amount: float) -> Resonance:
    """Part of the way from one resonance to another, in log terms - which is
    how both the ear and the circuit (a pot, a cable length) move them."""
    amount = float(np.clip(amount, 0.0, 1.0))
    f0 = float(np.exp(np.log(source.f0_hz) * (1 - amount) + np.log(target.f0_hz) * amount))
    q = float(np.exp(np.log(source.q) * (1 - amount) + np.log(target.q) * amount))
    return Resonance(round(f0, 0), round(q, 2), label=f"{amount:.0%} of the way to {target.label or target.reads_as}")


def correction_db(freqs, source: Resonance, target: Resonance, *, limit_db: float = MAX_CORRECTION_DB) -> np.ndarray:
    """What to multiply a DI by, per frequency, to swap ``source`` for ``target``."""
    delta = response_db(freqs, target.f0_hz, target.q) - response_db(freqs, source.f0_hz, source.q)
    return np.clip(delta, -limit_db, limit_db)


def swap(
    audio: np.ndarray, sample_rate: int, source: Resonance, target: Resonance, *,
    amount: float = 1.0, taps: int = 2047, limit_db: float = MAX_CORRECTION_DB,
) -> np.ndarray:
    """``audio`` as if played through ``target``'s pickup instead of ``source``'s.

    A linear-phase FIR with its delay removed, so the result stays sample-
    aligned with the input - which matters, because performance-mode scoring
    compares it frame by frame against the record.
    """
    from scipy.signal import fftconvolve, firwin2

    goal = morph(source, target, amount) if amount < 1.0 else target
    nyquist = sample_rate / 2
    grid = np.concatenate([[0.0], np.geomspace(20, nyquist * 0.999, 256), [nyquist]])
    gain = 10 ** (correction_db(np.maximum(grid, 1.0), source, goal, limit_db=limit_db) / 20)
    # Above the fit range the model is extrapolating; hold the gain there.
    hold = grid > FIT_HIGH_HZ * 1.3
    if hold.any() and (~hold).any():
        gain[hold] = gain[~hold][-1]
    kernel = firwin2(taps, grid / nyquist, gain)
    audio = np.asarray(audio, dtype=np.float64)
    delay = (taps - 1) // 2
    if audio.ndim == 1:
        return fftconvolve(audio, kernel)[delay : delay + len(audio)]
    return np.stack([fftconvolve(audio[:, c], kernel)[delay : delay + len(audio)] for c in range(audio.shape[1])], axis=1)


def apply_resonance(audio: np.ndarray, sample_rate: int, resonance: Resonance) -> np.ndarray:
    """Put a resonance onto a flat source - what a synthetic DI is missing."""
    return swap(audio, sample_rate, FLAT, resonance, limit_db=40.0)

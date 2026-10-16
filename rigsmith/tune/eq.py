"""Closing the last of the gap with an EQ the Core actually has.

After the amp and its knobs are as close as they get, what is left is mostly
a frequency-response difference - the record's mic, its mix EQ, a cab IR
that is nearly right. That is what an EQ block is for, and the Core has two
shapes worth fitting to it:

* the **G EQ** block - eight graphic bands at 60 Hz to 6.4 kHz, +-15 dB;
* the **slot EQ** every block carries (``SltEQF/G/Q`` plus a high- and a
  low-pass) - one parametric band, no extra block, no extra CPU.

The difference curve is measured between long-term spectra over the frames
that hold playing, smoothed to a third of an octave, and fitted by least
squares to the real bands' shapes - graphic bands overlap, so setting each
to "the difference at its centre" double-counts wherever two neighbours
both reach. The fit is then applied offline to the candidate and the
remaining difference re-measured, so a suggestion arrives with evidence of
what it buys.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import numpy as np

GEQ_BANDS = (60, 100, 200, 400, 800, 1600, 3200, 6400)
GEQ_NAMES = ("Gain60Hz", "Gain100Hz", "Gain200Hz", "Gain400Hz", "Gain800Hz", "Gain1_6kHz", "Gain3_2kHz", "Gain6_4kHz")
GEQ_RANGE = 15.0
GEQ_Q = 1.41

#: Log-spaced analysis grid; what the fit is judged on.
GRID = np.geomspace(50, 10000, 96)


def _long_term_db(audio, sample_rate: int) -> np.ndarray:
    from ..match.metrics import _stft_magnitude, to_mono

    n_fft = 8192
    magnitude = _stft_magnitude(to_mono(audio), n_fft, n_fft // 4)
    if not len(magnitude):
        return np.full(len(GRID), -120.0)
    energy = (magnitude ** 2).sum(axis=1)
    if energy.max() <= 0:
        # Digital silence: no frame is loud, and an empty mean is NaN - which
        # became a NaN EQ "suggestion" for a candidate that rendered silent.
        return np.full(len(GRID), -120.0)
    loud = energy > energy.max() * 10 ** (-35 / 10)
    power = (magnitude[loud] ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(n_fft, 1 / sample_rate)
    smoothed = []
    for centre in GRID:
        lo, hi = centre / 2 ** (1 / 6), centre * 2 ** (1 / 6)
        mask = (freqs >= lo) & (freqs < hi)
        smoothed.append(float(power[mask].mean()) if mask.any() else float(np.interp(centre, freqs, power)))
    return 10 * np.log10(np.maximum(smoothed, 1e-20))


def difference(target, candidate, sample_rate: int) -> np.ndarray:
    """Target minus candidate in dB on :data:`GRID`, loudness-matched."""
    t = _long_term_db(target, sample_rate)
    c = _long_term_db(candidate, sample_rate)
    diff = t - c
    weights = _weights()
    return diff - float(np.average(diff, weights=weights))


def _weights() -> np.ndarray:
    # The guitar's range counts most; the extremes are mostly recording.
    return np.where((GRID >= 80) & (GRID <= 6000), 1.0, 0.3)


def _peak_db(f: np.ndarray, centre: float, gain_db: float, q: float, sample_rate: int = 48000) -> np.ndarray:
    """Magnitude response of an RBJ peaking filter, in dB, at ``f``."""
    from ..loop.virtual import _biquad

    b, a = _biquad("peak", centre, sample_rate, q=q, gain_db=gain_db)
    w = 2 * np.pi * f / sample_rate
    z = np.exp(-1j * w)
    response = (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)
    return 20 * np.log10(np.maximum(np.abs(response), 1e-12))


def fit_graphic(diff: np.ndarray) -> np.ndarray:
    """Least-squares G EQ gains for a difference curve, clipped to +-15 dB.

    The bands overlap, so this is solved jointly. The peaking response is
    close enough to linear in its gain (in dB) for a 1 dB basis to be used,
    then refined twice against the true response.
    """
    weights = np.sqrt(_weights())
    basis = np.stack([_peak_db(GRID, c, 1.0, GEQ_Q) for c in GEQ_BANDS], axis=1)
    gains, *_ = np.linalg.lstsq(basis * weights[:, None], diff * weights, rcond=None)
    gains = np.clip(gains, -GEQ_RANGE, GEQ_RANGE)
    for _ in range(2):
        achieved = sum(_peak_db(GRID, c, g, GEQ_Q) for c, g in zip(GEQ_BANDS, gains))
        residual = diff - achieved
        step, *_ = np.linalg.lstsq(basis * weights[:, None], residual * weights, rcond=None)
        gains = np.clip(gains + step, -GEQ_RANGE, GEQ_RANGE)
    return gains


def fit_slot(diff: np.ndarray) -> dict:
    """The single parametric band (and filters) closest to the difference."""
    weights = _weights()
    index = int(np.argmax(np.abs(diff) * weights))
    centre = float(GRID[index])
    gain = float(np.clip(diff[index], -18, 18))
    half = abs(gain) / 2
    # Width where the difference falls to half its peak, as a Q.
    lo = index
    while lo > 0 and abs(diff[lo]) > half and np.sign(diff[lo]) == np.sign(gain):
        lo -= 1
    hi = index
    while hi < len(GRID) - 1 and abs(diff[hi]) > half and np.sign(diff[hi]) == np.sign(gain):
        hi += 1
    octaves = max(math.log2(GRID[hi] / GRID[lo]), 0.2)
    q = float(np.clip(1.0 / (2 * math.sinh(math.log(2) / 2 * octaves)), 0.3, 6.0))
    plan = {"SltEQOn": True, "SltEQF": round(centre, 1), "SltEQG": round(gain, 1), "SltEQQ": round(q, 2)}
    low = diff[GRID < 90]
    if len(low) and float(np.mean(low)) < -6:
        plan["SltEQHP"] = 90.0
    high = diff[GRID > 7000]
    if len(high) and float(np.mean(high)) < -6:
        plan["SltEQLP"] = 7000.0
    return plan


def apply_graphic(audio, sample_rate: int, gains) -> np.ndarray:
    from ..loop.virtual import _biquad, _filter

    out = np.asarray(audio, dtype=np.float64)
    for centre, gain in zip(GEQ_BANDS, gains):
        if abs(gain) >= 0.05:
            out = _filter(out, *_biquad("peak", centre, sample_rate, q=GEQ_Q, gain_db=float(gain)))
    return out


@dataclass
class EQPlan:
    graphic_db: dict[str, float] = field(default_factory=dict)
    """G EQ band name -> dB."""
    graphic_wire: dict[str, float] = field(default_factory=dict)
    """The same, as 0-1 wire values for the Core."""
    slot: dict = field(default_factory=dict)
    before_db: float = 0.0
    """RMS difference before, dB, over the guitar's range."""
    after_db: float = 0.0
    """RMS difference after applying the graphic fit offline."""
    curve: list[float] = field(default_factory=list)
    grid: list[float] = field(default_factory=list)

    def describe(self) -> str:
        bands = ", ".join(f"{name[4:]} {db:+.1f}" for name, db in self.graphic_db.items() if abs(db) >= 0.5)
        lines = [
            f"G EQ: {bands or 'flat - nothing worth fixing'}",
            f"  difference {self.before_db:.1f} dB -> {self.after_db:.1f} dB with it",
            f"  or one slot band: {self.slot.get('SltEQG', 0):+.1f} dB at {self.slot.get('SltEQF', 0):.0f} Hz, Q {self.slot.get('SltEQQ', 1):.1f}"
            + (f", high-pass {self.slot['SltEQHP']:.0f} Hz" if "SltEQHP" in self.slot else "")
            + (f", low-pass {self.slot['SltEQLP']:.0f} Hz" if "SltEQLP" in self.slot else ""),
        ]
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["describe"] = self.describe()
        return data


def match(target, candidate, sample_rate: int) -> EQPlan:
    """What EQ would close the gap between a candidate and the target.

    When either is silent there is no gap an EQ could close, and the plan
    is flat rather than a fit to 100 dB of nothing.
    """
    if not np.any(np.asarray(target)) or not np.any(np.asarray(candidate)):
        flat = {name: 0.0 for name in GEQ_NAMES}
        return EQPlan(graphic_db=flat, graphic_wire={name: 0.5 for name in GEQ_NAMES}, grid=[float(f) for f in GRID])
    diff = difference(target, candidate, sample_rate)
    weights = _weights()
    gains = fit_graphic(diff)
    corrected = apply_graphic(candidate, sample_rate, gains)
    after = difference(target, corrected, sample_rate)
    rms = lambda d: float(np.sqrt(np.average(d ** 2, weights=weights)))  # noqa: E731
    return EQPlan(
        graphic_db={name: round(float(g), 1) for name, g in zip(GEQ_NAMES, gains)},
        graphic_wire={name: round((float(g) + GEQ_RANGE) / (2 * GEQ_RANGE), 4) for name, g in zip(GEQ_NAMES, gains)},
        slot=fit_slot(diff),
        before_db=round(rms(diff), 2),
        after_db=round(rms(after), 2),
        curve=[round(float(v), 2) for v in diff],
        grid=[round(float(f), 1) for f in GRID],
    )


#: How each amp knob is moved by what the record has more (or less) of, as
#: (band weights, dB per unit of knob travel). A tone stack's bass shelf
#: moves the low end; mids the 400-1600 band; treble the bite; presence the
#: top. The scale is the simulated amp's (24 dB per full turn, 18 for
#: presence) - on a real amp it is only a direction and a rough size, which is
#: all a starting point needs.
KNOB_BANDS = {
    "Bass": ({"low end": 0.6, "body": 0.4}, 24.0),
    "Mid": ({"mids": 0.7, "mud": 0.3}, 24.0),
    "Treble": ({"bite": 1.0}, 24.0),
    "Presence": ({"presence": 0.5, "fizz": 0.5}, 18.0),
}

#: The furthest a starting point moves any knob from where it was.
MAX_START_MOVE = 0.3


def informed_start(space, differences, *, current: dict | None = None) -> dict:
    """Where to start turning knobs, from where the candidate is off.

    ``differences`` is :func:`rigsmith.match.metrics.band_differences` of the
    untuned candidate against the target: too much low end starts the search
    with the bass down, and so on. Without this the search starts every knob
    at noon and a small budget can end before it gets to the obvious move.
    """
    delta = {d.name: d.delta_db for d in differences}
    start = dict(current or space.start())
    for knob in space.knobs:
        rule = KNOB_BANDS.get(knob.param) or KNOB_BANDS.get({"Pres": "Presence"}.get(knob.param, ""))
        if rule is None:
            continue
        weights, scale = rule
        excess = sum(delta.get(band, 0.0) * weight for band, weight in weights.items())
        move = max(-MAX_START_MOVE, min(MAX_START_MOVE, -excess / scale))
        start[knob.key] = start.get(knob.key, (knob.low + knob.high) / 2) + move * (knob.high - knob.low)
    return space.clip(start)


#: What to reach for when a band is off, (too much, not enough). Only a
#: starting point - the same band can be the amp, the cab or the record.
BAND_MOVES = {
    "sub": ("a high-pass around 80 Hz", "a cab with more low end"),
    "low end": ("Bass down", "Bass up, or a 4x12"),
    "body": ("Bass or Mid down a little", "Mid up a little"),
    "mud": ("Mid down, or a cut around 500 Hz", "Mid up"),
    "mids": ("Mid down", "Mid up"),
    "presence": ("Presence down", "Presence up"),
    "bite": ("Treble down", "Treble up"),
    "fizz": ("a low-pass around 7 kHz, or a darker cab", "Treble or Presence up, or a brighter cab"),
    "air": ("nothing - usually the record's own top end", "nothing - usually the record's own top end"),
}


def remaining(target, audio, sample_rate: int) -> list[dict]:
    """Band by band, where ``audio`` still differs from ``target``, and what to try.

    ``{band, low, high, delta_db, audible, meaning, try}`` per guitar band -
    what a run hands over so the next move is a knob, not a number.
    """
    from ..match.metrics import band_differences

    rows = []
    for diff in band_differences(target, audio, sample_rate):
        too_much, too_little = BAND_MOVES.get(diff.name, ("", ""))
        rows.append({
            "band": diff.name, "low": diff.low, "high": diff.high, "delta_db": round(diff.delta_db, 1),
            "audible": diff.audible, "meaning": diff.meaning, "try": too_much if diff.delta_db > 0 else too_little,
        })
    return rows


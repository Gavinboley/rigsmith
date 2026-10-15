"""Making the loop measure what it claims to.

Three things are calibrated, in order of how badly each one lies when left
alone:

**Level.** An amp model's gain is only meaningful relative to what arrives
at its input. An interface's line output is far hotter than a pickup, so a DI
sent at unity drives the Core like a guitar with its volume at eleven - every
capture comes back with more gain than the same rig gives the actual guitar,
and the comparison ranks tones you will never hear. The send gain is set so
the Core receives the DI at the level the guitar itself would give it:

* *reference* - the route can hear what the Core received (its USB channel
  5). Compare that against the guitar played straight into the Core. Exact.
* *guitar-direct* - no reference channel. Load a clean rig, play the guitar
  straight in and record the return, then reamp a DI of similar playing and
  record that; a clean rig is linear, so the difference in the returns is the
  difference at the input. Close.
* *assumed* - nothing measured. The send stays where it is and every capture
  carries a note saying its gain staging was not checked.

**Latency and polarity** are measured on every capture anyway (see
:mod:`.run`); the sweep here records what they were, so a change - a driver
update, a different buffer size - is visible.

**The loop itself.** A sweep through a clean rig gives the frequency response
of everything that is not the rig - interface converters, cable loading, the
Core's input and output stages - and a noise floor. A response that is not
flat to within a dB or so is worth knowing about before blaming a capture.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .routes import LoopRoute
from .run import reamp
from .signals import analyse_sweep, exponential_sweep

#: Window for the level statistic: long enough to hold a pick attack.
LEVEL_WINDOW_S = 0.05

#: The send gain is never set outside this range - a calibration asking for
#: more is measuring a fault (an unplugged cable), not a level.
SEND_GAIN_LIMITS = (-40.0, 24.0)


def level_statistic(audio: np.ndarray, sample_rate: int) -> float | None:
    """How hard a recording of playing hits, in dBFS.

    The 90th percentile of 50 ms window peaks over the windows that hold
    playing. Not the single loudest peak - one accidental thump would set the
    whole calibration - and not an RMS, which a held chord and a staccato riff
    disagree about.
    """
    audio = np.abs(np.asarray(audio, dtype=np.float64))
    window = max(1, int(LEVEL_WINDOW_S * sample_rate))
    usable = (len(audio) // window) * window
    if usable < window * 4:
        return None
    peaks = audio[:usable].reshape(-1, window).max(axis=1)
    loudest = float(peaks.max())
    if loudest <= 1e-6:
        return None
    playing = peaks[peaks > loudest * 10 ** (-30 / 20)]
    if len(playing) < 4:
        return None
    return 20 * math.log10(float(np.percentile(playing, 90)))


@dataclass
class Calibration:
    route_id: str
    measured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    latency_samples: float | None = None
    polarity: int | None = None
    drift_ppm: float | None = None
    noise_floor_db: float | None = None
    response_db: list[float] = field(default_factory=list)
    bands_hz: list[int] = field(default_factory=list)
    thd_percent: float | None = None
    loop_gain_db: float | None = None
    send_gain_db: float = 0.0
    level_method: str = "assumed"
    """reference, guitar-direct, or assumed."""
    core_input_peak_db: float | None = None
    """Where a typical DI lands at the Core's input after calibration."""
    notes: list[str] = field(default_factory=list)

    @property
    def flat_within_db(self) -> float | None:
        if not self.response_db:
            return None
        # 100 Hz - 6.3 kHz is the guitar's range; outside it a converter's
        # own roll-off is normal and not worth flagging.
        usable = [v for b, v in zip(self.bands_hz, self.response_db) if 100 <= b <= 6300]
        return round(max(usable) - min(usable), 2) if usable else None

    def describe(self) -> str:
        lines = [f"calibration of {self.route_id} ({self.measured_at})"]
        if self.latency_samples is not None:
            lines.append(f"  latency {self.latency_samples:.1f} samples, polarity {'inverted' if self.polarity and self.polarity < 0 else 'normal'}")
        if self.drift_ppm:
            lines.append(f"  clock drift {self.drift_ppm:+.1f} ppm between output and input")
        if self.noise_floor_db is not None:
            lines.append(f"  return noise floor {self.noise_floor_db:.1f} dBFS")
        flat = self.flat_within_db
        if flat is not None:
            lines.append(f"  loop response flat within {flat:.1f} dB from 100 Hz to 6.3 kHz")
        if self.thd_percent is not None:
            lines.append(f"  distortion {self.thd_percent:.1f}%")
        lines.append(f"  send gain {self.send_gain_db:+.1f} dB ({self.level_method})")
        if self.core_input_peak_db is not None:
            lines.append(f"  a typical DI reaches the Core at {self.core_input_peak_db:.1f} dBFS")
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["flat_within_db"] = self.flat_within_db
        data["describe"] = self.describe()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Calibration":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"calibration-{self.route_id}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, directory: Path, route_id: str) -> "Calibration | None":
        path = directory / f"calibration-{route_id}.json"
        if not path.is_file():
            return None
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None


def measure_noise(route: LoopRoute, *, backend, seconds: float = 1.0) -> float:
    recorded = backend.record(
        seconds,
        input_device=route.input_device,
        input_channels=route.input_channels,
        sample_rate=route.sample_rate,
        hostapi=route.hostapi,
    )
    recorded = np.asarray(recorded, dtype=np.float64)
    signal = recorded.mean(axis=1) if recorded.ndim > 1 else recorded
    rms = float(np.sqrt(np.mean(signal ** 2))) if len(signal) else 0.0
    return round(20 * math.log10(max(rms, 1e-9)), 1)


def measure_sweep(route: LoopRoute, *, backend, level_db: float = -24.0, seconds: float = 3.0):
    """Sweep the loop; return the capture and its analysis."""
    sweep = exponential_sweep(seconds, route.sample_rate, level_db=level_db)
    capture = reamp(route, sweep, backend=backend, send_gain_db=0.0)
    analysis = analyse_sweep(capture.wet[: len(sweep) + route.sample_rate // 2], sweep, route.sample_rate)
    return capture, analysis


def send_gain_from_levels(direct_db: float, reamped_db: float, current_gain_db: float) -> float:
    """The send gain that makes a reamp hit as hard as the guitar did."""
    proposed = current_gain_db + (direct_db - reamped_db)
    return round(float(np.clip(proposed, *SEND_GAIN_LIMITS)), 2)


def calibrate(
    route: LoopRoute,
    *,
    backend,
    di: np.ndarray | None = None,
    direct: np.ndarray | None = None,
    direct_is_reference: bool = False,
    progress=None,
) -> Calibration:
    """Measure a route and propose its send gain.

    Args:
        di: A DI of your playing - a profile's samples or a take - to reamp
            for the level comparison.
        direct: The guitar played straight into the Core, recorded on the
            route's reference channel (``direct_is_reference``) or on its
            return through a clean rig.
    """
    def step(fraction, message):
        if progress:
            progress.check()
            progress.update(fraction, message)

    result = Calibration(route_id=route.route_id, send_gain_db=route.send_gain_db)
    step(0.05, "listening to the noise floor")
    result.noise_floor_db = measure_noise(route, backend=backend)

    step(0.25, "sweeping the loop")
    capture, analysis = measure_sweep(route, backend=backend)
    result.latency_samples = round(capture.latency_samples, 2)
    result.polarity = capture.polarity
    result.drift_ppm = round(capture.drift_ppm, 2) if not route.same_clock else 0.0
    result.response_db = [round(v, 2) for v in analysis.response_db]
    from .signals import ANALYSIS_BANDS

    result.bands_hz = list(ANALYSIS_BANDS)
    result.thd_percent = round(analysis.thd_percent, 2)
    result.loop_gain_db = round(analysis.gain_db, 2)
    if not capture.aligned:
        result.notes.append("the sweep could not be found on the return - check the cabling before trusting anything else here")
    if analysis.thd_percent > 10:
        result.notes.append(
            f"the loaded rig distorts the sweep ({analysis.thd_percent:.0f}% THD), so the "
            "response above is the rig's, not the loop's. Load a clean rig for a loop response; "
            "latency and polarity are good either way."
        )
    flat = result.flat_within_db
    if flat is not None and flat > 3 and analysis.thd_percent <= 10:
        result.notes.append(
            f"the response varies by {flat:.1f} dB across the guitar's range. That is the "
            "loop *and* the loaded rig together - a cab or EQ accounts for most of it. Load "
            "a rig with every block bypassed to see the loop on its own."
        )

    if di is not None and len(di):
        step(0.6, "reamping your DI to set the level")
        reamped = reamp(route, di, backend=backend, send_gain_db=route.send_gain_db)
        if direct is not None and reamped.dry_received is not None and direct_is_reference:
            direct_db = level_statistic(direct, route.sample_rate)
            reamp_db = level_statistic(reamped.dry_received, route.sample_rate)
            if direct_db is not None and reamp_db is not None:
                result.send_gain_db = send_gain_from_levels(direct_db, reamp_db, route.send_gain_db)
                result.level_method = "reference"
                result.core_input_peak_db = round(direct_db, 1)
        elif direct is not None:
            direct_db = level_statistic(direct, route.sample_rate)
            reamp_db = level_statistic(reamped.wet, route.sample_rate)
            if direct_db is not None and reamp_db is not None:
                result.send_gain_db = send_gain_from_levels(direct_db, reamp_db, route.send_gain_db)
                result.level_method = "guitar-direct"
                result.notes.append(
                    "levels matched through the return - accurate only if the rig "
                    "loaded for both recordings was clean"
                )
        elif reamped.input_peak_db is not None:
            result.core_input_peak_db = round(reamped.input_peak_db, 1)
            result.notes.append(
                f"the Core receives this DI peaking at {reamped.input_peak_db:.1f} dBFS. "
                "Record the guitar played straight in to match it exactly."
            )
    if result.level_method == "assumed":
        result.notes.append(
            "the send level has not been matched to your guitar, so captures may "
            "hit the amp harder or softer than you do"
        )
    step(1.0, "calibrated")
    return result

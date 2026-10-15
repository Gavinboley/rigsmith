"""One pass through the loop, aligned to the sample.

The playback is laid out so the recording can always be found again:

    [silence][sync A][gap][ DI ...... ][tail][sync B][silence]

Both marks are located in the recording by GCC-PHAT. Mark A gives the
latency (and the polarity, from the sign of the peak); the distance between
A and B, compared with the distance that was played, gives the clock drift
when the output and input are different devices. The recording is resampled
to take the drift out, shifted by the fractional latency, flipped if it came
back inverted, and cut to the DI's own timeline - so sample *n* of the
returned audio is the rig's response to sample *n* of the DI, to within the
rig's own group delay.

That last clause is not a rounding error. The marks travel through the rig,
so what they measure is the loop's latency *plus* the few samples the rig's
filters delay things by - and the DI is delayed by the same filters, so
taking those samples out would be removing part of the rig. The aligned
comparison in :mod:`.accuracy` fine-aligns against its target instead, and
reports what it moved. A capture whose marks could not be found confidently
says so, and is not offered for aligned scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..core.errors import Invalid
from .routes import LoopRoute
from .signals import estimate_drift, fractional_shift, gcc_phat, resample_by, sync_mark

PRE_S = 0.3
GAP_S = 0.3
TAIL_S = 1.0
POST_S = 0.6

#: Below this the GCC-PHAT peak is not distinguishable from the floor.
MIN_SHARPNESS = 8.0


@dataclass
class LoopCapture:
    """The rig's response to a DI, on the DI's own timeline."""

    wet: np.ndarray
    sample_rate: int
    dry_received: np.ndarray | None = None
    """The DI as the Core received it, when the route has a reference channel."""
    latency_samples: float = 0.0
    drift_ppm: float = 0.0
    polarity: int = 1
    sharpness: float = 0.0
    aligned: bool = True
    send_gain_db: float = 0.0
    peak_db: float = -120.0
    rms_db: float = -120.0
    input_peak_db: float | None = None
    """Peak of the DI as the Core received it, from the reference channel."""
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return len(self.wet) / self.sample_rate

    def describe(self) -> str:
        lines = [
            f"{self.duration:.2f}s @ {self.sample_rate} Hz, latency "
            f"{self.latency_samples:.1f} samples ({1000 * self.latency_samples / self.sample_rate:.2f} ms)"
            + (", polarity inverted (corrected)" if self.polarity < 0 else "")
            + (f", clock drift {self.drift_ppm:+.1f} ppm (corrected)" if abs(self.drift_ppm) >= 0.5 else ""),
            f"  return peaks at {self.peak_db:.1f} dBFS, {self.rms_db:.1f} dBFS RMS",
        ]
        if self.input_peak_db is not None:
            lines.append(f"  the Core received the DI peaking at {self.input_peak_db:.1f} dBFS")
        lines.append(
            "  aligned to the DI - safe for aligned scoring"
            if self.aligned
            else "  NOT aligned - score in distribution or performance mode only"
        )
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "sample_rate": self.sample_rate,
            "latency_samples": round(self.latency_samples, 2),
            "latency_ms": round(1000 * self.latency_samples / self.sample_rate, 3),
            "drift_ppm": round(self.drift_ppm, 2),
            "polarity": self.polarity,
            "sharpness": round(self.sharpness, 1),
            "aligned": self.aligned,
            "send_gain_db": self.send_gain_db,
            "peak_db": round(self.peak_db, 2),
            "rms_db": round(self.rms_db, 2),
            "input_peak_db": None if self.input_peak_db is None else round(self.input_peak_db, 2),
            "notes": self.notes,
            "describe": self.describe(),
        }


def _db(value: float) -> float:
    return 20 * np.log10(max(value, 1e-9))


def layout(di: np.ndarray, sample_rate: int, *, send_gain_db: float = 0.0) -> tuple[np.ndarray, dict]:
    """The playback buffer and where everything in it sits."""
    mark = sync_mark(sample_rate)
    gain = 10 ** (send_gain_db / 20)
    sent = np.asarray(di, dtype=np.float64) * gain
    pieces = [
        np.zeros(int(PRE_S * sample_rate)),
        mark,
        np.zeros(int(GAP_S * sample_rate)),
        sent,
        np.zeros(int(TAIL_S * sample_rate)),
        mark,
        np.zeros(int(POST_S * sample_rate)),
    ]
    positions = {}
    cursor = 0
    for name, piece in zip(("pre", "mark_a", "gap", "di", "tail", "mark_b", "post"), pieces):
        positions[name] = cursor
        cursor += len(piece)
    positions["total"] = cursor
    positions["mark_len"] = len(mark)
    positions["di_len"] = len(sent)
    peak = float(np.max(np.abs(sent))) if len(sent) else 0.0
    if peak > 1.0:
        raise Invalid(
            f"the DI would leave the interface at {_db(peak):+.1f} dBFS and clip",
            hint="Lower the route's send gain, or use a quieter DI.",
        )
    return np.concatenate(pieces), positions


def _locate(recorded: np.ndarray, mark: np.ndarray, expected: int, search: int) -> tuple[float, float, float]:
    """Where ``mark`` sits in ``recorded``, searching around ``expected``."""
    lo = max(0, expected - search)
    hi = min(len(recorded), expected + len(mark) + search)
    window = recorded[lo:hi]
    if len(window) < len(mark):
        return float(expected), 0.0, 0.0
    delay, peak, sharpness = gcc_phat(window, mark, max_lag=len(window))
    return lo + delay, peak, sharpness


def align_capture(
    recorded: np.ndarray,
    positions: dict,
    sample_rate: int,
    *,
    two_clocks: bool,
    reference: np.ndarray | None = None,
    max_latency_s: float = 0.4,
) -> LoopCapture:
    """Find the marks, undo drift, latency and polarity; cut to the DI."""
    mark = sync_mark(sample_rate)
    search = int(max_latency_s * sample_rate)
    recorded = np.asarray(recorded, dtype=np.float64)

    pos_a, peak_a, sharp_a = _locate(recorded, mark, positions["mark_a"], search)
    pos_b, peak_b, sharp_b = _locate(recorded, mark, positions["mark_b"], search)
    notes: list[str] = []
    played_span = positions["mark_b"] - positions["mark_a"]
    drift = estimate_drift(pos_a - positions["mark_a"], pos_b - positions["mark_b"], played_span)

    confident = min(sharp_a, sharp_b) >= MIN_SHARPNESS and np.sign(peak_a) == np.sign(peak_b)
    if two_clocks and confident and abs(drift) >= 0.5:
        # Stretch the recording back onto the playback clock, then find A again.
        recorded = resample_by(recorded, 1.0 / (1.0 + drift * 1e-6))
        if reference is not None:
            reference = resample_by(reference, 1.0 / (1.0 + drift * 1e-6))
        pos_a, peak_a, sharp_a = _locate(recorded, mark, positions["mark_a"], search)
    elif not two_clocks and abs(drift) > 20 and confident:
        notes.append(
            f"the marks are {drift:+.0f} ppm apart on what should be one clock - "
            "check the sample rate the device is actually running at"
        )

    latency = pos_a - positions["mark_a"]
    polarity = 1 if peak_a >= 0 else -1
    start = positions["di"] + latency
    length = positions["di_len"] + int(TAIL_S * sample_rate)

    def cut(signal: np.ndarray) -> np.ndarray:
        whole = int(np.floor(start))
        fraction = start - whole
        piece = signal[whole : whole + length + 2]
        if fraction > 1e-6:
            piece = fractional_shift(piece, -fraction)
        piece = piece[:length]
        if len(piece) < length:
            piece = np.pad(piece, (0, length - len(piece)))
        return piece

    wet = cut(recorded) * polarity
    capture = LoopCapture(
        wet=wet,
        sample_rate=sample_rate,
        latency_samples=float(latency),
        drift_ppm=float(drift) if two_clocks else 0.0,
        polarity=polarity,
        sharpness=float(min(sharp_a, sharp_b)),
        aligned=bool(confident),
        notes=notes,
    )
    if reference is not None:
        # The reference channel has its own (smaller) latency - the Core's
        # converter rather than the whole loop - so it gets its own marks.
        ref_a, ref_peak, ref_sharp = _locate(reference, mark, positions["mark_a"], search)
        if ref_sharp >= MIN_SHARPNESS:
            ref_start = positions["di"] + (ref_a - positions["mark_a"])
            whole = int(np.floor(ref_start))
            dry = reference[whole : whole + positions["di_len"]]
            capture.dry_received = dry * (1 if ref_peak >= 0 else -1)
            if len(dry):
                capture.input_peak_db = _db(float(np.max(np.abs(dry))))
    if not confident:
        capture.notes.append(
            "the sync marks could not be found clearly - check the return is "
            "connected and the rig is not muted or gated shut"
        )
    body = wet[: positions["di_len"]]
    if len(body):
        capture.peak_db = _db(float(np.max(np.abs(body))))
        capture.rms_db = _db(float(np.sqrt(np.mean(body * body))))
    if capture.peak_db > -0.5:
        capture.notes.append("the return is clipping at the interface input - turn its gain down")
    elif capture.peak_db < -50:
        capture.notes.append("the return is nearly silent - is the Core's output connected and the rig unmuted?")
    return capture


def reamp(
    route: LoopRoute,
    di: np.ndarray,
    *,
    backend=None,
    send_gain_db: float | None = None,
) -> LoopCapture:
    """Send a DI around the loop and bring it back on the DI's timeline."""
    from .backend import get_backend

    backend = backend or get_backend(route.backend if route.backend == "virtual" else None)
    gain = route.send_gain_db if send_gain_db is None else send_gain_db
    playback, positions = layout(di, route.sample_rate, send_gain_db=gain)

    # Same device: record the reference channel in the same stream. Different
    # devices: the reference lives with the input device (a hybrid route's
    # Core), so it rides along with the wet channels.
    in_channels = list(route.input_channels)
    has_reference = bool(route.reference_channel) and (route.reference_device or route.input_device) == route.input_device
    if has_reference:
        in_channels = in_channels + [int(route.reference_channel)]

    columns = np.repeat(playback[:, None], len(route.output_channels), axis=1)
    recorded = backend.playrec(
        columns,
        output_device=route.output_device,
        input_device=route.input_device,
        output_channels=route.output_channels,
        input_channels=in_channels,
        sample_rate=route.sample_rate,
        hostapi=route.hostapi,
    )
    recorded = np.asarray(recorded, dtype=np.float64)
    wet_count = len(route.input_channels)
    wet = recorded[:, :wet_count].mean(axis=1) if wet_count > 1 else recorded[:, 0]
    reference = recorded[:, wet_count] if has_reference else None

    capture = align_capture(
        wet,
        positions,
        route.sample_rate,
        # same_clock, not same_device: an interface Windows lists as a
        # separate input and output is still one box with one clock.
        two_clocks=not route.same_clock,
        reference=reference,
    )
    capture.send_gain_db = gain
    return capture

"""The loop with nothing plugged in.

A simulated audio interface, cable, and HeadRush Core, wired the way the
manual says the real ones are, with the inconveniences that make the real
loop hard to get right:

* each stream starts with a slightly different latency, as USB audio does;
* the Core and the interface have separate clocks that drift apart by a few
  parts per million;
* an interface line output is much hotter than a guitar, so the Core's input
  sees the wrong level until someone calibrates the send;
* the analog return has a noise floor;
* the Core's USB channels 1-2 go straight to its outputs, bypassing the rig,
  exactly as on the hardware - so sending a reamp on the wrong channel
  produces a dry return here too, and the mistake is catchable in a test.

The rig is :class:`SimAmp` - a small amp model with the Core's own knob
names. It is not any real amp and does not claim to be; it is a deterministic
nonlinear system with tone controls, which is what a loop, a calibration and
an optimiser need to be exercised against.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

import numpy as np

SAMPLE_RATE = 48000


# --------------------------------------------------------------------------
# the amp
# --------------------------------------------------------------------------


def _biquad(kind: str, freq: float, sample_rate: int, *, q: float = 0.707, gain_db: float = 0.0):
    """RBJ cookbook coefficients ``(b, a)``."""
    freq = min(max(freq, 10.0), 0.45 * sample_rate)
    w0 = 2 * math.pi * freq / sample_rate
    cos, sin = math.cos(w0), math.sin(w0)
    alpha = sin / (2 * q)
    a_gain = 10 ** (gain_db / 40)
    if kind == "lowpass":
        b = [(1 - cos) / 2, 1 - cos, (1 - cos) / 2]
        a = [1 + alpha, -2 * cos, 1 - alpha]
    elif kind == "highpass":
        b = [(1 + cos) / 2, -(1 + cos), (1 + cos) / 2]
        a = [1 + alpha, -2 * cos, 1 - alpha]
    elif kind == "peak":
        b = [1 + alpha * a_gain, -2 * cos, 1 - alpha * a_gain]
        a = [1 + alpha / a_gain, -2 * cos, 1 - alpha / a_gain]
    elif kind == "lowshelf":
        root = 2 * math.sqrt(a_gain) * alpha
        b = [a_gain * ((a_gain + 1) - (a_gain - 1) * cos + root), 2 * a_gain * ((a_gain - 1) - (a_gain + 1) * cos), a_gain * ((a_gain + 1) - (a_gain - 1) * cos - root)]
        a = [(a_gain + 1) + (a_gain - 1) * cos + root, -2 * ((a_gain - 1) + (a_gain + 1) * cos), (a_gain + 1) + (a_gain - 1) * cos - root]
    elif kind == "highshelf":
        root = 2 * math.sqrt(a_gain) * alpha
        b = [a_gain * ((a_gain + 1) + (a_gain - 1) * cos + root), -2 * a_gain * ((a_gain - 1) + (a_gain + 1) * cos), a_gain * ((a_gain + 1) + (a_gain - 1) * cos - root)]
        a = [(a_gain + 1) - (a_gain - 1) * cos + root, 2 * ((a_gain - 1) - (a_gain + 1) * cos), (a_gain + 1) - (a_gain - 1) * cos - root]
    else:  # pragma: no cover - programming error
        raise ValueError(kind)
    b, a = np.array(b), np.array(a)
    return b / a[0], a / a[0]


def _filter(audio, b, a):
    from scipy.signal import lfilter

    return lfilter(b, a, audio)


#: The SimAmp's controls, as the Core names them on its Amp block, with the
#: display range each one reads in. On the wire every one of these is 0-1.
CONTROLS = {
    "Gain": (0.0, 100.0),
    "Bass": (0.0, 100.0),
    "Mid": (0.0, 100.0),
    "Treble": (0.0, 100.0),
    "Pres": (0.0, 100.0),
    "Level": (0.0, 100.0),
}

#: Voicings the simulated Amp block can be switched between, by ``Type``.
VOICINGS = {
    0: ("Sim Clean", {"drive_max": 6.0, "mid_hz": 600.0, "cab_lp": 6500.0, "tight_hz": 60.0}),
    1: ("Sim Crunch", {"drive_max": 40.0, "mid_hz": 750.0, "cab_lp": 5200.0, "tight_hz": 90.0}),
    2: ("Sim Lead", {"drive_max": 160.0, "mid_hz": 800.0, "cab_lp": 4800.0, "tight_hz": 120.0}),
    3: ("Sim Modern", {"drive_max": 260.0, "mid_hz": 500.0, "cab_lp": 4400.0, "tight_hz": 160.0}),
}


#: Simulated cabs, by ``CabType``: (name, high-pass, low resonance Hz and dB,
#: a mid/presence peak Hz, dB and Q, top roll-off). Four shapes that differ
#: the way real ones do - an open-back 1x12 is light and bright, a Greenback
#: 4x12 humps in the mids and rolls off early, V30s bark at 2-3 kHz.
CAB_VOICINGS = {
    0: ("Sim 1x12 open back", 90.0, 130.0, 2.0, 2000.0, 1.0, 1.0, 6000.0),
    1: ("Sim 2x12 open back", 80.0, 110.0, 3.0, 2400.0, 2.0, 1.2, 5600.0),
    2: ("Sim 4x12 V30", 70.0, 100.0, 4.0, 2800.0, 4.0, 1.5, 5000.0),
    3: ("Sim 4x12 Greenback", 70.0, 95.0, 4.5, 1500.0, 3.0, 1.0, 4300.0),
}


@dataclass
class SimAmp:
    """A deterministic amp-and-cab with the Core's knob names, 0-1 each."""

    Type: int = 1
    Gain: float = 0.5
    Bass: float = 0.5
    Mid: float = 0.5
    Treble: float = 0.5
    Pres: float = 0.5
    Level: float = 0.5
    enabled: bool = True
    cab: bool = True
    CabType: int = -1
    """A simulated cab from :data:`CAB_VOICINGS`; -1 keeps the voicing's own."""

    #: The Core's Amp block calls these GainA, Presence and Master; the
    #: simulated amp answers to both, so a tuner written against the real
    #: block's parameter names drives this one unchanged.
    ALIASES = {"GainA": "Gain", "Presence": "Pres", "Master": "Level"}

    def set(self, **values) -> None:
        for name, value in values.items():
            name = self.ALIASES.get(name, name)
            if name == "Type":
                self.Type = int(value) if int(value) in VOICINGS else self.Type
            elif name == "CabType":
                self.CabType = int(value) if int(value) in CAB_VOICINGS else -1
            elif name in CONTROLS:
                setattr(self, name, float(np.clip(value, 0.0, 1.0)))
            elif name in ("enabled", "cab"):
                setattr(self, name, bool(value))

    def params(self) -> dict:
        return {"Type": self.Type, "CabType": self.CabType, **{name: getattr(self, name) for name in CONTROLS}}

    @property
    def voicing(self) -> str:
        return VOICINGS.get(self.Type, VOICINGS[1])[0]

    def process(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
        x = np.asarray(audio, dtype=np.float64)
        if not self.enabled:
            return x.copy()
        voice = VOICINGS.get(self.Type, VOICINGS[1])[1]
        # Pre-drive tightening: a gain stage that sees less bass distorts
        # less muddily, which is what a "tight" knob does on real amps.
        x = _filter(x, *_biquad("highpass", voice["tight_hz"], sample_rate, q=0.6))
        drive = 1.0 + (voice["drive_max"] - 1.0) * (self.Gain ** 2)
        # Slight asymmetry puts even harmonics in, as a triode does.
        y = np.tanh(drive * x + 0.08 * drive * x * x) - np.tanh(0.0)
        y /= math.tanh(max(1.0, drive * 0.3))
        y = _filter(y, *_biquad("lowshelf", 110.0, sample_rate, gain_db=24 * (self.Bass - 0.5)))
        y = _filter(y, *_biquad("peak", voice["mid_hz"], sample_rate, q=0.8, gain_db=24 * (self.Mid - 0.5)))
        y = _filter(y, *_biquad("highshelf", 2800.0, sample_rate, gain_db=24 * (self.Treble - 0.5)))
        y = _filter(y, *_biquad("highshelf", 5500.0, sample_rate, gain_db=18 * (self.Pres - 0.5)))
        if self.cab and self.CabType in CAB_VOICINGS:
            _, hp, res_hz, res_db, peak_hz, peak_db, peak_q, lp = CAB_VOICINGS[self.CabType]
            y = _filter(y, *_biquad("highpass", hp, sample_rate, q=0.8))
            y = _filter(y, *_biquad("peak", res_hz, sample_rate, q=1.4, gain_db=res_db))
            y = _filter(y, *_biquad("peak", peak_hz, sample_rate, q=peak_q, gain_db=peak_db))
            y = _filter(y, *_biquad("lowpass", lp, sample_rate, q=0.9))
            y = _filter(y, *_biquad("lowpass", lp * 1.2, sample_rate, q=0.6))
        elif self.cab:
            # A 4x12 in three filters: no sub, a low resonance, and the steep
            # top roll-off that is most of why a cab is a cab.
            y = _filter(y, *_biquad("highpass", 75.0, sample_rate, q=0.8))
            y = _filter(y, *_biquad("peak", 115.0, sample_rate, q=1.4, gain_db=4.0))
            y = _filter(y, *_biquad("lowpass", voice["cab_lp"], sample_rate, q=0.9))
            y = _filter(y, *_biquad("lowpass", voice["cab_lp"] * 1.2, sample_rate, q=0.6))
        level = 10 ** ((self.Level - 0.5) * 36 / 20) * 0.25
        return y * level


# --------------------------------------------------------------------------
# the devices
# --------------------------------------------------------------------------


@dataclass
class VirtualDeviceInfo:
    name: str
    inputs: int
    outputs: int
    sample_rate: float = SAMPLE_RATE
    hostapi: str = "Virtual"


INTERFACE = VirtualDeviceInfo("Virtual Interface (2i2)", 2, 2)
CORE = VirtualDeviceInfo("Virtual HeadRush Core", 6, 4)
DEVICES = (INTERFACE, CORE)


@dataclass
class VirtualRig:
    """What the virtual Core is running: an amp, and its input gain."""

    amp: SimAmp = field(default_factory=SimAmp)
    input_gain_db: float = 0.0
    """The In block's gain, -60..+12 dB on the real Core."""
    usb_in_level_db: float = 0.0
    """Global USB In Level, -60..+12 dB."""
    gate_db: float | None = None

    def process(self, audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
        x = audio * 10 ** (self.input_gain_db / 20)
        if self.gate_db is not None:
            x = np.where(np.abs(x) < 10 ** (self.gate_db / 20), 0.0, x)
        return self.amp.process(x, sample_rate)


@dataclass
class Wiring:
    """How the virtual cables run, and how imperfect they are."""

    cable_gain_db: float = 9.0
    """Interface line out into the Core's instrument input: hot. A guitar
    DI peaking at -12 dBFS arrives at the Core around -3 dBFS - the reason a
    reamp has to be calibrated before its gain staging means anything."""
    return_gain_db: float = -4.0
    noise_db: float = -88.0
    base_latency: int = 312
    """Interface out -> Core -> interface in, in samples, before jitter."""
    jitter: int = 96
    """Each stream start adds up to this many samples - USB does that."""
    core_drift_ppm: float = 35.0
    """The Core's clock against the interface's."""
    polarity: int = 1
    seed: int | None = None


class VirtualWorld:
    """The simulated bench: one interface, one Core, the cables between."""

    def __init__(self, *, rig: VirtualRig | None = None, wiring: Wiring | None = None) -> None:
        self.rig = rig or VirtualRig()
        self.wiring = wiring or Wiring()
        self._rng = np.random.default_rng(self.wiring.seed)
        self._lock = threading.Lock()
        self.last_core_input: np.ndarray | None = None
        """What arrived at the Core's guitar input on the last pass - the
        thing its USB channel 5 would carry."""

    def _latency(self) -> int:
        return self.wiring.base_latency + int(self._rng.integers(0, self.wiring.jitter + 1))

    def _noise(self, length: int) -> np.ndarray:
        return self._rng.normal(0.0, 10 ** (self.wiring.noise_db / 20), length)

    def playrec(
        self,
        playback: np.ndarray,
        *,
        output_device: str,
        input_device: str,
        output_channels: list[int],
        input_channels: list[int],
        sample_rate: int = SAMPLE_RATE,
    ) -> np.ndarray:
        """Play columns of ``playback`` on 1-based output channels; record inputs.

        Returns ``(frames, len(input_channels))``.
        """
        with self._lock:
            frames = len(playback)
            playback = np.asarray(playback, dtype=np.float64)
            if playback.ndim == 1:
                playback = playback[:, None]
            sent = {ch: playback[:, i] for i, ch in enumerate(output_channels)}
            silent = np.zeros(frames)

            # What reaches the Core's guitar jack: interface output 1, hot.
            guitar_in = silent
            if output_device == INTERFACE.name and 1 in sent:
                guitar_in = sent[1] * 10 ** (self.wiring.cable_gain_db / 20)
            core_in_delay = self._latency() // 3
            guitar_in = _delay(guitar_in, core_in_delay)

            # USB from the computer, per the manual: 1-2 straight to the
            # outputs, 3 into the rig.
            usb_direct = silent
            usb_reamp = silent
            if output_device == CORE.name:
                usb_direct = (sent.get(1, silent) + sent.get(2, silent)) * 0.5
                usb_reamp = sent.get(3, silent) * 10 ** (self.rig.usb_in_level_db / 20)
                usb_delay = self._latency() // 2
                usb_direct = _delay(usb_direct, usb_delay)
                usb_reamp = _delay(usb_reamp, usb_delay)

            self.last_core_input = guitar_in.copy()
            wet = self.rig.process(guitar_in + usb_reamp, sample_rate) + usb_direct
            wet = wet * self.wiring.polarity

            columns = []
            for channel in input_channels:
                if input_device == INTERFACE.name:
                    if channel == 1:
                        back = _delay(wet, self._latency() - core_in_delay) * 10 ** (self.wiring.return_gain_db / 20)
                        columns.append(back + self._noise(frames))
                    else:
                        columns.append(self._noise(frames))
                elif input_device == CORE.name:
                    if channel in (1, 2, 3, 4):
                        source = wet
                    elif channel == 5:
                        source = guitar_in
                    else:
                        source = silent
                    recorded = _delay(source, 8) + self._noise(frames) * 0.3
                    columns.append(recorded)
                else:
                    columns.append(self._noise(frames))

            out = np.stack(columns, axis=1) if columns else np.zeros((frames, 0))
            # Recording on the Core while playing on the interface: two clocks.
            if input_device == CORE.name and output_device != CORE.name and self.wiring.core_drift_ppm:
                from .signals import resample_by

                ratio = 1.0 + self.wiring.core_drift_ppm * 1e-6
                stretched = np.stack([resample_by(out[:, i], ratio)[:frames] for i in range(out.shape[1])], axis=1)
                if len(stretched) < frames:
                    stretched = np.pad(stretched, ((0, frames - len(stretched)), (0, 0)))
                out = stretched
            return np.clip(out, -1.0, 1.0)


def _delay(audio: np.ndarray, samples: int) -> np.ndarray:
    if samples <= 0:
        return audio
    out = np.zeros_like(audio)
    if samples < len(audio):
        out[samples:] = audio[: len(audio) - samples]
    return out


_world: VirtualWorld | None = None


def world() -> VirtualWorld:
    """The process-wide simulated bench, created on first use."""
    global _world
    if _world is None:
        _world = VirtualWorld()
    return _world


def set_world(value: VirtualWorld | None) -> None:
    global _world
    _world = value

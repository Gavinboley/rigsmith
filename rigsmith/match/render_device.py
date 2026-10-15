"""Layer 2: reamp through the actual HeadRush over USB.

The Core presents a 6-in / 4-out USB audio interface, and the input channel
layout is what makes automated tone testing possible:

===========  =========================================
Channel      Content
===========  =========================================
1-2          stereo wet, XLR main outputs
3-4          stereo wet, 1/4" main outputs
5            **mono dry DI from the guitar input**
6            mono dry from the microphone input
===========  =========================================

From the computer the other way, the manual's layout matters as much and is
easier to get wrong: channels 1-2 go **straight to the Core's outputs**,
bypassing the rig; channel 3 is the mono **reamp input** that runs through
whatever rig is loaded (with USB Mode set to Reamp). A reamp sent on channel 1
comes back unprocessed. This module used to do exactly that.

For anything beyond the Core's own USB - an interface feeding the Core's
guitar input through a cable - see :mod:`rigsmith.loop`, which also aligns
every capture from sync marks rather than one latency measurement.

Channel 5 is the important one. Recording it alongside the wet channels means
one take gives you both a reference tone *and* the dry DI that produced it -
so a single performance can be reamped through unlimited candidate rigs, each
compared against the same reference, with no further playing.

Round-trip latency is measured rather than assumed. Without that compensation
the returned audio is offset by a few milliseconds, and a few milliseconds is
enough to make ESR report a perfect match as a catastrophic one.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .metrics import to_mono
from ..core.errors import Unavailable

#: Input channel layout of the Core's USB interface, 1-based as the manual
#: numbers them.
WET_XLR_CHANNELS = (1, 2)
WET_QUARTER_INCH_CHANNELS = (3, 4)
DRY_GUITAR_CHANNEL = 5
DRY_MIC_CHANNEL = 6

#: Computer -> Core: 1-2 go straight to the outputs, 3 into the rig.
REAMP_OUTPUT_CHANNEL = 3

DEFAULT_SAMPLE_RATE = 48000


class AudioUnavailable(Unavailable):
    """The audio backend or the device is not available."""


def _sounddevice():
    try:
        import sounddevice as sd

        return sd
    except (ImportError, OSError) as exc:
        raise AudioUnavailable(
            "sounddevice is required for hardware rendering. Install with:\n"
            "  pip install 'rigsmith[audio]'\n"
            "On Windows, ASIO support needs a PortAudio build with ASIO; see "
            "the python-sounddevice docs."
        ) from exc


@dataclass
class AudioDevice:
    """An audio interface as the host sees it."""

    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float
    hostapi: str = ""

    @property
    def looks_like_headrush(self) -> bool:
        return "headrush" in self.name.lower()

    @property
    def has_full_channel_layout(self) -> bool:
        """Whether the dry DI channel is reachable on this device."""
        return self.max_input_channels >= DRY_GUITAR_CHANNEL

    def describe(self) -> str:
        return (
            f"[{self.index}] {self.name} "
            f"({self.max_input_channels} in / {self.max_output_channels} out, "
            f"{self.default_sample_rate:.0f} Hz, {self.hostapi})"
        )


def list_devices() -> list[AudioDevice]:
    """Every audio device the host exposes."""
    sd = _sounddevice()
    hostapis = sd.query_hostapis()
    devices = []
    for index, raw in enumerate(sd.query_devices()):
        devices.append(
            AudioDevice(
                index=index,
                name=raw["name"],
                max_input_channels=raw["max_input_channels"],
                max_output_channels=raw["max_output_channels"],
                default_sample_rate=raw["default_samplerate"],
                hostapi=hostapis[raw["hostapi"]]["name"] if hostapis else "",
            )
        )
    return devices


def find_headrush_device() -> AudioDevice:
    """Locate the Core's USB audio interface.

    Prefers a device exposing all six input channels, since the dry DI on
    channel 5 is the reason to use it at all.
    """
    devices = [d for d in list_devices() if d.looks_like_headrush]
    if not devices:
        available = "\n".join(f"  {d.describe()}" for d in list_devices())
        raise AudioUnavailable(
            "No HeadRush audio device found. Connect the Core to this computer "
            "over USB-B and check it is selected as an audio device.\n\n"
            f"Devices visible now:\n{available}"
        )

    full = [d for d in devices if d.has_full_channel_layout and d.max_input_channels]
    if full:
        return max(full, key=lambda d: d.max_input_channels)

    best = max(devices, key=lambda d: d.max_input_channels)
    return best


@dataclass
class Capture:
    """One recorded take: the wet tone and the dry DI that produced it."""

    wet: np.ndarray
    dry: np.ndarray | None
    sample_rate: int
    latency_samples: int = 0
    latency_measured: bool = True
    """False when round-trip latency could not be measured.

    An uncompensated offset of a few milliseconds is enough to make ESR
    report a perfect match as a catastrophic one, so a capture with this
    unset must not be scored in aligned mode. See docs/TONE_MATCHING.md.
    """
    notes: list[str] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return len(self.wet) / self.sample_rate

    @property
    def safe_for_aligned_scoring(self) -> bool:
        """Whether sample-wise metrics can be trusted against this capture."""
        return self.latency_measured

    def describe(self) -> str:
        lines = [f"{self.duration:.2f}s @ {self.sample_rate} Hz"]
        lines.append("dry DI captured" if self.dry is not None else "no dry DI captured")
        if not self.latency_measured:
            lines.append(
                "LATENCY NOT MEASURED - do not score this capture in aligned "
                "mode. A few milliseconds of uncompensated offset makes ESR "
                "report a perfect match as a catastrophic one. Use "
                "distribution mode, or fix the audio device and re-record."
            )
        elif self.latency_samples:
            lines.append(
                f"round-trip latency {self.latency_samples} samples "
                f"({1000 * self.latency_samples / self.sample_rate:.1f} ms), compensated"
            )
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


def measure_latency(
    device: AudioDevice | None = None,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    output_channel: int = REAMP_OUTPUT_CHANNEL,
    input_channel: int = WET_XLR_CHANNELS[0],
    timeout_s: float = 2.0,
) -> int:
    """Measure USB round-trip latency in samples, by sending an impulse.

    Returns the offset to trim from recorded audio so it lines up with what
    was played. Assuming a fixed latency instead of measuring it is the
    quickest way to make every aligned comparison wrong.
    """
    sd = _sounddevice()
    device = device or find_headrush_device()

    length = int(sample_rate * timeout_s)
    probe = np.zeros(length, dtype=np.float32)
    # A short burst survives a noise gate better than a single sample.
    probe[sample_rate // 10 : sample_rate // 10 + 32] = 0.7

    out_channels = max(output_channel, 1)
    in_channels = max(input_channel, 1)

    playback = np.zeros((length, out_channels), dtype=np.float32)
    playback[:, output_channel - 1] = probe

    recorded = sd.playrec(
        playback,
        samplerate=sample_rate,
        channels=in_channels,
        device=(device.index, device.index),
        blocking=True,
    )
    captured = np.asarray(recorded)[:, input_channel - 1]

    correlation = np.correlate(captured, probe, mode="full")
    peak = int(np.argmax(np.abs(correlation))) - (len(probe) - 1)
    return max(0, peak)


def capture_performance(
    duration_s: float,
    device: AudioDevice | None = None,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    wet_channels: tuple[int, ...] = WET_XLR_CHANNELS,
    dry_channel: int | None = DRY_GUITAR_CHANNEL,
) -> Capture:
    """Record a performance, capturing the wet tone and the dry DI together.

    Play the part once. The dry DI on channel 5 can then be reamped through
    any number of candidate rigs, all compared against this same wet
    reference - so candidate count costs compute, not playing time.
    """
    sd = _sounddevice()
    device = device or find_headrush_device()

    needed = max(list(wet_channels) + ([dry_channel] if dry_channel else []))
    if device.max_input_channels < needed:
        raise AudioUnavailable(
            f"{device.name} exposes {device.max_input_channels} input channels, "
            f"but channel {needed} is required. On the Core the dry DI is "
            f"channel {DRY_GUITAR_CHANNEL}; check the USB audio mode."
        )

    frames = int(duration_s * sample_rate)
    recorded = sd.rec(
        frames,
        samplerate=sample_rate,
        channels=needed,
        device=device.index,
        blocking=True,
    )
    recorded = np.asarray(recorded, dtype=np.float64)

    wet = np.stack([recorded[:, c - 1] for c in wet_channels], axis=-1)
    dry = recorded[:, dry_channel - 1] if dry_channel else None

    capture = Capture(wet=to_mono(wet), dry=dry, sample_rate=sample_rate)
    if dry is not None and float(np.max(np.abs(dry))) < 1e-4:
        capture.notes.append(
            f"channel {dry_channel} (dry DI) is silent - check that the guitar "
            "is plugged into the instrument input"
        )
    return capture


def reamp(
    di: np.ndarray,
    device: AudioDevice | None = None,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    output_channels: tuple[int, ...] = (REAMP_OUTPUT_CHANNEL,),
    wet_channels: tuple[int, ...] = WET_XLR_CHANNELS,
    latency_samples: int | None = None,
    tail_s: float = 0.5,
) -> Capture:
    """Play a DI through the device and record the processed result.

    Args:
        di: Dry signal to send.
        latency_samples: Round-trip offset to compensate. Measured
            automatically when omitted.
        tail_s: Extra recording time so reverb and delay tails are not cut off.

    Returns the wet capture, already latency-compensated and trimmed to the
    length of the input plus the tail.
    """
    sd = _sounddevice()
    device = device or find_headrush_device()
    di = to_mono(np.asarray(di, dtype=np.float64))

    # Latency is measured, not assumed. If measurement fails we record that
    # loudly rather than quietly falling back to zero - a silent zero is
    # indistinguishable from a real measurement downstream, and would let
    # aligned scoring report contaminated numbers with full confidence.
    # Only an audio-subsystem failure is tolerated here; anything else is a
    # bug in measure_latency and is allowed to propagate.
    latency_measured = True
    if latency_samples is None:
        try:
            latency_samples = measure_latency(device, sample_rate=sample_rate)
        except AudioUnavailable as exc:
            latency_samples = 0
            latency_measured = False
            latency_error = str(exc).splitlines()[0]
        else:
            latency_error = None
    else:
        latency_error = None

    tail = int(tail_s * sample_rate)
    padded = np.concatenate([di, np.zeros(tail + latency_samples)])

    out_count = max(output_channels)
    playback = np.zeros((len(padded), out_count), dtype=np.float32)
    for channel in output_channels:
        playback[:, channel - 1] = padded

    recorded = sd.playrec(
        playback,
        samplerate=sample_rate,
        channels=max(wet_channels),
        device=(device.index, device.index),
        blocking=True,
    )
    recorded = np.asarray(recorded, dtype=np.float64)

    wet = to_mono(np.stack([recorded[:, c - 1] for c in wet_channels], axis=-1))
    if latency_samples:
        wet = wet[latency_samples:]
    wet = wet[: len(di) + tail]

    capture = Capture(
        wet=wet,
        dry=di,
        sample_rate=sample_rate,
        latency_samples=latency_samples,
        latency_measured=latency_measured,
    )
    if latency_error:
        capture.notes.append(
            f"round-trip latency could not be measured ({latency_error}); "
            "the capture is NOT time-aligned"
        )
    if float(np.max(np.abs(wet))) < 1e-4:
        capture.notes.append(
            "the returned signal is silent - check the Core's USB audio input "
            "routing, and that the rig is not fully bypassed"
        )
    elif float(np.max(np.abs(wet))) > 0.99:
        capture.notes.append(
            "the returned signal is clipping - reduce the DI level or the rig's "
            "output gain, since a clipped capture will score badly for the "
            "wrong reason"
        )
    return capture

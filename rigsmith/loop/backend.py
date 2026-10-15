"""Where audio actually goes: real devices, or the virtual bench.

One small interface - list devices, play and record at once, record alone,
play alone - with two implementations. :class:`SoundDeviceBackend` is
PortAudio through python-sounddevice; :class:`VirtualBackend` is
:mod:`.virtual`. Everything above this module is written against the
interface, which is why the loop's calibration and alignment code is tested
against the same kinds of failure the hardware produces.

Two devices in one PortAudio stream is not something Windows host APIs
reliably allow, so when the output and input are different devices the
real backend opens two streams and starts them as close together as it can.
The offset between them is unknown - and does not matter, because every
capture carries sync marks that measure it.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import numpy as np

from ..core.errors import Invalid, Unavailable


@dataclass
class DeviceInfo:
    name: str
    index: int
    inputs: int
    outputs: int
    sample_rate: float
    hostapi: str = ""

    @property
    def looks_like_headrush(self) -> bool:
        return "headrush" in self.name.lower()

    def describe(self) -> str:
        return f"[{self.index}] {self.name} ({self.inputs} in / {self.outputs} out, {self.sample_rate:.0f} Hz, {self.hostapi})"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "index": self.index,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "sample_rate": self.sample_rate,
            "hostapi": self.hostapi,
            "headrush": self.looks_like_headrush,
            "describe": self.describe(),
        }


class AudioBackend:
    name = "abstract"

    def devices(self) -> list[DeviceInfo]:  # pragma: no cover - interface
        raise NotImplementedError

    def find(self, name: str, *, hostapi: str | None = None, need_inputs: int = 0, need_outputs: int = 0) -> DeviceInfo:
        """A device by name (exact, then substring), preferring a host API."""
        candidates = [d for d in self.devices() if d.name == name] or [
            d for d in self.devices() if name.lower() in d.name.lower()
        ]
        candidates = [d for d in candidates if d.inputs >= need_inputs and d.outputs >= need_outputs]
        if hostapi:
            preferred = [d for d in candidates if d.hostapi == hostapi]
            candidates = preferred or candidates
        if not candidates:
            known = "\n".join(f"  {d.describe()}" for d in self.devices())
            raise Unavailable(
                f"no audio device matching '{name}' with {need_inputs} input(s) "
                f"and {need_outputs} output(s)",
                hint=f"Devices visible now:\n{known or '  (none)'}",
            )
        return candidates[0]

    def playrec(self, playback, *, output_device, input_device, output_channels, input_channels, sample_rate, hostapi=None) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def record(self, seconds, *, input_device, input_channels, sample_rate, hostapi=None) -> np.ndarray:  # pragma: no cover
        raise NotImplementedError

    def play(self, audio, *, output_device, output_channels, sample_rate, hostapi=None) -> None:  # pragma: no cover
        raise NotImplementedError


class VirtualBackend(AudioBackend):
    """The simulated bench of :mod:`.virtual`."""

    name = "virtual"

    def __init__(self, world=None) -> None:
        from . import virtual

        self.world = world or virtual.world()

    def devices(self) -> list[DeviceInfo]:
        from .virtual import DEVICES

        return [
            DeviceInfo(d.name, i, d.inputs, d.outputs, d.sample_rate, d.hostapi)
            for i, d in enumerate(DEVICES)
        ]

    def playrec(self, playback, *, output_device, input_device, output_channels, input_channels, sample_rate, hostapi=None):
        return self.world.playrec(
            playback,
            output_device=self.find(output_device).name,
            input_device=self.find(input_device).name,
            output_channels=list(output_channels),
            input_channels=list(input_channels),
            sample_rate=sample_rate,
        )

    def record(self, seconds, *, input_device, input_channels, sample_rate, hostapi=None):
        frames = int(seconds * sample_rate)
        silence = np.zeros((frames, 1))
        device = self.find(input_device)
        return self.world.playrec(
            silence,
            output_device=device.name,
            input_device=device.name,
            output_channels=[4] if device.outputs >= 4 else [device.outputs or 1],
            input_channels=list(input_channels),
            sample_rate=sample_rate,
        )

    def play(self, audio, *, output_device, output_channels, sample_rate, hostapi=None) -> None:
        return None


class SoundDeviceBackend(AudioBackend):
    """PortAudio via python-sounddevice."""

    name = "sounddevice"

    def __init__(self) -> None:
        # ASIO is opt-in in sounddevice 0.5.1+, and has to be chosen before
        # the module is imported for the first time.
        if os.environ.get("RIGSMITH_ASIO"):
            os.environ.setdefault("SD_ENABLE_ASIO", "1")
        try:
            import sounddevice as sd
        except (ImportError, OSError) as exc:
            raise Unavailable(
                "sounddevice is needed to talk to audio hardware",
                hint="Install it with:  pip install 'rigsmith[audio]'",
            ) from exc
        self.sd = sd

    def devices(self) -> list[DeviceInfo]:
        hostapis = self.sd.query_hostapis()
        found = []
        for index, raw in enumerate(self.sd.query_devices()):
            found.append(
                DeviceInfo(
                    name=raw["name"],
                    index=index,
                    inputs=raw["max_input_channels"],
                    outputs=raw["max_output_channels"],
                    sample_rate=raw["default_samplerate"],
                    hostapi=hostapis[raw["hostapi"]]["name"] if hostapis else "",
                )
            )
        return found

    def _mapping(self, channels) -> list[int]:
        mapping = [int(c) for c in channels]
        if not mapping or min(mapping) < 1:
            raise Invalid("audio channels are numbered from 1")
        return mapping

    def playrec(self, playback, *, output_device, input_device, output_channels, input_channels, sample_rate, hostapi=None):
        playback = np.asarray(playback, dtype=np.float32)
        if playback.ndim == 1:
            playback = playback[:, None]
        out_map = self._mapping(output_channels)
        in_map = self._mapping(input_channels)
        out_dev = self.find(output_device, hostapi=hostapi, need_outputs=max(out_map))
        in_dev = self.find(input_device, hostapi=hostapi, need_inputs=max(in_map))
        if out_dev.index == in_dev.index:
            recorded = self.sd.playrec(
                playback,
                samplerate=sample_rate,
                device=out_dev.index,
                input_mapping=in_map,
                output_mapping=out_map,
                blocking=True,
            )
            return np.asarray(recorded, dtype=np.float64)
        return self._two_streams(playback, out_dev, in_dev, out_map, in_map, sample_rate)

    def _two_streams(self, playback, out_dev, in_dev, out_map, in_map, sample_rate):
        """Separate devices: start recording, then playing, as close as possible.

        The gap between the two starts is unknown and differs every time,
        which is fine - the sync marks in the playback are found again in the
        recording, and that is what the alignment uses.
        """
        sd = self.sd
        frames = len(playback)
        recorded: dict[str, np.ndarray] = {}
        errors: list[BaseException] = []

        def capture():
            try:
                data = sd.rec(frames + sample_rate // 2, samplerate=sample_rate, device=in_dev.index, mapping=in_map, blocking=True)
                recorded["data"] = np.asarray(data, dtype=np.float64)
            except BaseException as exc:  # surfaced below
                errors.append(exc)

        thread = threading.Thread(target=capture, daemon=True)
        thread.start()
        time.sleep(0.05)
        sd.play(playback, samplerate=sample_rate, device=out_dev.index, mapping=out_map, blocking=True)
        thread.join(timeout=len(playback) / sample_rate + 5)
        if errors:
            raise Unavailable(f"recording from {in_dev.name} failed: {errors[0]}")
        if "data" not in recorded:
            raise Unavailable(f"recording from {in_dev.name} did not finish")
        return recorded["data"]

    def record(self, seconds, *, input_device, input_channels, sample_rate, hostapi=None):
        in_map = self._mapping(input_channels)
        device = self.find(input_device, hostapi=hostapi, need_inputs=max(in_map))
        data = self.sd.rec(int(seconds * sample_rate), samplerate=sample_rate, device=device.index, mapping=in_map, blocking=True)
        return np.asarray(data, dtype=np.float64)

    def play(self, audio, *, output_device, output_channels, sample_rate, hostapi=None) -> None:
        out_map = self._mapping(output_channels)
        device = self.find(output_device, hostapi=hostapi, need_outputs=max(out_map))
        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim == 1:
            audio = np.repeat(audio[:, None], len(out_map), axis=1)
        self.sd.play(audio, samplerate=sample_rate, device=device.index, mapping=out_map, blocking=True)


_override: AudioBackend | None = None


def get_backend(name: str | None = None) -> AudioBackend:
    """The backend to use: explicit, then ``RIGSMITH_AUDIO``, then the real one."""
    if _override is not None and name is None:
        return _override
    choice = (name or os.environ.get("RIGSMITH_AUDIO") or "sounddevice").lower()
    if choice == "virtual":
        return VirtualBackend()
    return SoundDeviceBackend()


def set_backend(backend: AudioBackend | None) -> None:
    """Pin a backend for this process - tests, and the app's demo mode."""
    global _override
    _override = backend

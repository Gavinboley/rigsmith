"""Everything that talks to the HeadRush: network, storage, and MIDI.

Four transports, and this module is where the choice between them is made so
no surface has to reason about it:

* **network** - discovery and Remote Editor protocol probing
* **USB storage** - backup, restore, file transfer
* **MIDI** - rig and scene switching, and post-write verification

MIDI is the one fully documented interface, which makes it the verification
channel: confirming a write over the same channel that performed it proves
very little.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import get_settings, require_writable
from .errors import Invalid, NotFound, Unavailable
from .jobs import Progress


# --------------------------------------------------------------------------
# network
# --------------------------------------------------------------------------


async def discover(*, include_sweep: bool = True, timeout: float = 2.0):
    """Find HeadRush devices on the LAN, best candidate first."""
    from ..device.discovery import discover as _discover

    return await _discover(include_sweep=include_sweep, timeout=timeout)


async def find(*, include_sweep: bool = True):
    """The single best device, or :class:`NotFound` with what to check."""
    from ..device.discovery import find_device

    device = await find_device(include_sweep=include_sweep)
    if device is None:
        raise NotFound(
            "no HeadRush device answered on the local network",
            hint=(
                "Check: powered on; same Wi-Fi network and band as this "
                "machine; Global Settings > Wi-Fi > HeadRush Remote = On; no "
                "VPN active. You can also double-tap 'HeadRush Remote' on the "
                "device to read its IP, then set RIGSMITH_DEVICE to it."
            ),
        )
    return device


def render_discovery(devices) -> str:
    """Format a discovery result for a human or an agent."""
    if not devices:
        return (
            "No devices answered.\n\n"
            "Check: powered on; same Wi-Fi network and band; "
            "Global Settings > Wi-Fi > HeadRush Remote = On; no VPN active.\n"
            "You can also double-tap 'HeadRush Remote' on the device to read "
            "its IP, then set RIGSMITH_DEVICE to that address."
        )
    lines = [f"{len(devices)} host(s) answered:"]
    for info in devices:
        mark = "CONFIRMED" if info.confirmed else "unconfirmed"
        lines.append(f"  [{mark}] {info.describe()}")
    if not any(d.confirmed for d in devices):
        lines.append(
            "\nNone fingerprinted as HeadRush. If one of these is the Core, "
            "set RIGSMITH_DEVICE to its address."
        )
    return "\n".join(lines)


@dataclass
class ProbeResult:
    """A captured Remote Editor bundle and what was mined from it."""

    inventory: Any
    output_dir: Path
    base_url: str

    def render(self) -> str:
        inv = self.inventory
        lines = [f"Probed {self.base_url}", inv.summary()]
        if inv.firmware:
            lines.append(f"firmware: {inv.firmware}")
        if inv.endpoints:
            lines.append("\nEndpoints:")
            for endpoint in inv.endpoints[:30]:
                methods = ",".join(endpoint.methods) or "-"
                lines.append(f"  {endpoint.evidence:8} {methods:12} {endpoint.path}")
            if len(inv.endpoints) > 30:
                lines.append(f"  ... {len(inv.endpoints) - 30} more in inventory.json")
        if inv.websockets:
            lines.append("\nWebSockets: " + ", ".join(inv.websockets))
        if inv.message_types:
            lines.append("\nMessage types: " + ", ".join(inv.message_types[:30]))
        for note in inv.notes:
            lines.append(f"\nNote: {note}")
        lines.append(f"\nSaved to {self.output_dir}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "base_url": self.base_url,
            "output_dir": str(self.output_dir),
            **self.inventory.to_dict(),
        }


async def probe(
    *, max_assets: int = 200, save_assets: bool = True, out_dir: Path | None = None
) -> ProbeResult:
    """Fetch the Remote Editor bundle and extract its API surface.

    Read-only: the same requests a browser makes when you open the editor.
    """
    from ..protocol.capture import capture

    device = await find()
    settings = get_settings()
    settings.ensure_workspace()
    destination = out_dir or settings.captures_dir / datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )
    inventory = await capture(
        device.base_url,
        out_dir=destination,
        max_assets=max_assets,
        save_assets=save_assets,
    )
    return ProbeResult(
        inventory=inventory, output_dir=destination, base_url=device.base_url
    )


# --------------------------------------------------------------------------
# USB storage
# --------------------------------------------------------------------------


def storage_status(path: str | Path | None = None):
    """The mounted device volume, or :class:`Unavailable` explaining how."""
    from ..device.usbmsc import find_device_volume

    return find_device_volume(path)


def backup(path: str | Path | None = None, destination: Path | None = None):
    """Copy everything off the device into a timestamped local backup."""
    from ..device.usbmsc import find_device_volume, timestamped_backup

    require_writable("write a device backup")
    settings = get_settings()
    settings.ensure_workspace()
    volume = find_device_volume(path)
    return timestamped_backup(destination or settings.backups_dir, volume)


def install_capture(
    nam_path: str | Path,
    *,
    device_path: str | Path | None = None,
    overwrite: bool = False,
) -> str:
    """Copy a NAM capture onto the device over USB."""
    from ..device.usbmsc import find_device_volume, install_nam

    require_writable("copy a capture to the device")
    volume = find_device_volume(device_path)
    return install_nam(nam_path, volume, overwrite=overwrite)


# --------------------------------------------------------------------------
# MIDI
# --------------------------------------------------------------------------


def midi_ports() -> tuple[list[str], list[str]]:
    """``(outputs, inputs)`` visible to the host."""
    from ..device.midi import list_ports

    return list_ports()


def render_midi_ports() -> str:
    from ..device.midi import find_headrush_port

    outputs, inputs = midi_ports()
    lines = ["MIDI outputs:"]
    lines.extend(f"  {p}" for p in outputs) if outputs else lines.append("  (none)")
    lines.append("MIDI inputs:")
    lines.extend(f"  {p}" for p in inputs) if inputs else lines.append("  (none)")
    match = find_headrush_port(outputs)
    lines.append(
        f"\nHeadRush output port: {match}" if match else
        "\nNo HeadRush MIDI port found. Connect over USB-MIDI or a MIDI interface."
    )
    return "\n".join(lines)


def _controller(port: str | None, channel: int):
    from ..device.midi import MidiController

    return MidiController(port_name=port, channel=channel).open()


def select_rig(program: int, *, port: str | None = None, channel: int = 0) -> str:
    """Load a rig on the device by MIDI Program Change."""
    require_writable("send MIDI to the device")
    if not 0 <= program <= 127:
        raise Invalid("program must be 0-127")
    with _controller(port, channel) as midi:
        midi.select_rig(program)
    return (
        f"Sent Program Change {program}.\n"
        "The device's PC-to-rig numbering is not published, so confirm which "
        "rig actually loaded before relying on the mapping."
    )


def press_footswitch(
    footswitch: int, *, port: str | None = None, channel: int = 0
) -> str:
    """Simulate a footswitch press - also how scenes are activated."""
    require_writable("send MIDI to the device")
    with _controller(port, channel) as midi:
        midi.press_footswitch(footswitch)
    return f"Pressed footswitch {footswitch}."


def set_block(
    slot: int, enabled: bool, *, port: str | None = None, channel: int = 0
) -> str:
    """Turn a block on or off over MIDI."""
    require_writable("send MIDI to the device")
    with _controller(port, channel) as midi:
        midi.set_block(slot, enabled)
    return f"Block in slot {slot} {'on' if enabled else 'off'}."


def tap_tempo(
    bpm: float, *, taps: int = 4, port: str | None = None, channel: int = 0
) -> str:
    """Set the device's tempo by tapping it, the way a person would.

    Delay and modulation times on a record are the song's tempo, and the Core
    has no "set BPM to N" message - CC64 is a tap, and the device averages the
    intervals. So this taps at the interval you want, which is the same thing
    a foot does, and is why the result is close rather than exact.

    Four taps is the usual minimum for a stable average; more is steadier and
    takes longer. At 120 BPM, four taps is a second and a half.
    """
    import time

    require_writable("send MIDI to the device")
    if not 20 <= bpm <= 400:
        raise Invalid(
            f"{bpm} BPM is outside what a tap tempo can express",
            hint="The Core's tap range is roughly 20-400 BPM.",
        )
    if taps < 2:
        raise Invalid("a tap tempo needs at least two taps to have an interval")

    interval = 60.0 / bpm
    with _controller(port, channel) as midi:
        for index in range(taps):
            if index:
                time.sleep(interval)
            midi.tap_tempo()

    return (
        f"Tapped {taps} times at {bpm:.1f} BPM "
        f"({interval * 1000:.0f} ms apart).\n"
        "The device averages the intervals, so the result is close rather "
        "than exact - check the tempo on screen if it matters."
    )


@dataclass
class VerifyResult:
    """What a MIDI verification pass exercised."""

    draft_id: str
    steps: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Exercised rig from draft '{self.draft_id}' over MIDI:"]
        lines.extend(f"  {s}" for s in self.steps)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {"draft_id": self.draft_id, "steps": self.steps}


def verify_rig(
    draft_id: str,
    *,
    program: int | None = None,
    port: str | None = None,
    channel: int = 0,
) -> VerifyResult:
    """Drive a draft's rig over MIDI to confirm the device responds.

    This is a liveness check on a channel independent of whatever performed
    the write. It is **not** a read-back: the device does not report block
    state over MIDI, so it proves the rig loads and responds, not that its
    contents match the draft.
    """
    from ..device.midi import verify_rig as _verify

    require_writable("send MIDI to the device")
    from . import rigs

    draft = rigs.get(draft_id)
    with _controller(port, channel) as midi:
        steps = _verify(draft.rig, midi, program=program)
    return VerifyResult(draft_id=draft_id, steps=steps)

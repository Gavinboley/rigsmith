"""MIDI control of the HeadRush, and the write-verification channel.

MIDI is the one interface here that is fully documented, which makes it
valuable for two different reasons:

1. **Control.** Program Change selects rigs; Control Change toggles blocks
   and simulates footswitch presses. That is enough to drive a rig through
   its scenes during automated tone testing.
2. **Verification.** It is a completely separate path from the Remote Editor.
   After writing a rig over the network, switching to it and toggling its
   blocks over MIDI confirms the write actually landed, rather than trusting
   the same channel that performed it to also report on it.

Note there is no "select scene N" control. Scenes are reached by simulating
the footswitch they are assigned to, which is why scene-to-footswitch
assignment is part of the rig model rather than an afterthought.

MIDI reaches the device over the 5-pin DIN ports or a class-compliant
USB-MIDI device on the USB-A port.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from ..rig.model import FOOTSWITCH_COUNT, MAX_SLOTS

#: Control Change numbers, from the device's MIDI implementation.
CC_EXPRESSION_PEDAL = 1
CC_FOOTSWITCH_BASE = 49
"""Footswitch 1-5 are CC 49-53."""
CC_TAP_TEMPO = 64
CC_LOOPER_HALF_SPEED = 65
CC_LOOPER_DOUBLE_SPEED = 66
CC_LOOPER_HALF_LOOP = 67
CC_LOOPER_DOUBLE_LOOP = 68
CC_LOOPER_START_STOP = 69
CC_LOOPER_RECORD = 70
CC_LOOPER_INSERT = 71
CC_LOOPER_PEEL = 72
CC_LOOPER_MUTE = 73
CC_LOOPER_REVERSE = 74
CC_BLOCK_TOGGLE_BASE = 75
"""Blocks 1-14 are CC 75-88."""
CC_MIC_DRY_OUT = 89
CC_METRONOME_ON = 115

#: Values above this are read as "on" by convention.
ON_VALUE = 127
OFF_VALUE = 0


class MidiUnavailable(RuntimeError):
    """No MIDI backend, or no matching port."""


def _mido():
    try:
        import mido

        return mido
    except ImportError as exc:
        raise MidiUnavailable(
            "MIDI needs mido and a backend. Install with:\n"
            "  pip install 'rigsmith[midi]'"
        ) from exc


def block_toggle_cc(slot: int) -> int:
    """CC number that toggles the block in ``slot``."""
    if not 1 <= slot <= MAX_SLOTS:
        raise ValueError(f"slot {slot} is outside 1..{MAX_SLOTS}")
    return CC_BLOCK_TOGGLE_BASE + slot - 1


def footswitch_cc(footswitch: int) -> int:
    """CC number that simulates pressing ``footswitch``."""
    if not 1 <= footswitch <= FOOTSWITCH_COUNT:
        raise ValueError(f"footswitch {footswitch} is outside 1..{FOOTSWITCH_COUNT}")
    return CC_FOOTSWITCH_BASE + footswitch - 1


def list_ports() -> tuple[list[str], list[str]]:
    """Return ``(output_ports, input_ports)`` visible to the host."""
    mido = _mido()
    return list(mido.get_output_names()), list(mido.get_input_names())


def find_headrush_port(ports: Iterable[str] | None = None) -> str | None:
    """Pick the port that looks like a HeadRush, if any."""
    if ports is None:
        ports, _ = list_ports()
    for name in ports:
        if "headrush" in name.lower():
            return name
    return None


@dataclass
class MidiController:
    """Send MIDI to the device.

    Args:
        port_name: Output port. Auto-detected when omitted.
        channel: 0-based MIDI channel. The device can be set to Omni, in
            which case any channel works.
    """

    port_name: str | None = None
    channel: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.channel <= 15:
            raise ValueError("MIDI channel must be 0-15")
        self._port = None

    # -- connection -------------------------------------------------------

    def open(self) -> "MidiController":
        mido = _mido()
        name = self.port_name or find_headrush_port()
        if name is None:
            outputs, _ = list_ports()
            available = "\n".join(f"  {p}" for p in outputs) or "  (none)"
            raise MidiUnavailable(
                "No HeadRush MIDI port found. Connect the device over USB-MIDI "
                "or a MIDI interface.\n\nOutput ports visible now:\n" + available
            )
        try:
            self._port = mido.open_output(name)
        except (OSError, IOError) as exc:
            raise MidiUnavailable(f"could not open MIDI port '{name}': {exc}") from exc
        self.port_name = name
        return self

    def close(self) -> None:
        if self._port is not None:
            self._port.close()
            self._port = None

    def __enter__(self) -> "MidiController":
        return self.open()

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _require_open(self) -> None:
        if self._port is None:
            raise MidiUnavailable("MIDI port is not open; call open() first")

    def _send(self, message) -> None:
        self._require_open()
        self._port.send(message)

    # -- control ----------------------------------------------------------

    def select_rig(self, program: int) -> None:
        """Send a Program Change to load a rig.

        The device's Program-Change-to-rig numbering is not published, so
        confirm the mapping against the hardware before relying on it.
        """
        if not 0 <= program <= 127:
            raise ValueError("program must be 0-127")
        self._require_open()
        mido = _mido()
        self._send(mido.Message("program_change", channel=self.channel, program=program))

    def control(self, cc: int, value: int = ON_VALUE) -> None:
        """Send a raw Control Change."""
        if not 0 <= cc <= 127:
            raise ValueError("control number must be 0-127")
        if not 0 <= value <= 127:
            raise ValueError("control value must be 0-127")
        self._require_open()
        mido = _mido()
        self._send(
            mido.Message("control_change", channel=self.channel, control=cc, value=value)
        )

    def set_block(self, slot: int, on: bool) -> None:
        """Turn the block in ``slot`` on or off."""
        self.control(block_toggle_cc(slot), ON_VALUE if on else OFF_VALUE)

    def press_footswitch(self, footswitch: int) -> None:
        """Simulate a footswitch press.

        This is also how scenes are activated - there is no direct scene
        selection control.
        """
        self.control(footswitch_cc(footswitch), ON_VALUE)

    def activate_scene(self, scene) -> None:
        """Activate a :class:`~rigsmith.rig.model.Scene` via its footswitch.

        Raises:
            ValueError: if the scene has no footswitch assigned, since there
                is no other way to reach it over MIDI.
        """
        if scene.footswitch is None:
            raise ValueError(
                f"scene '{scene.name}' has no footswitch assigned, and MIDI has "
                "no direct scene-select control - assign a footswitch first"
            )
        self.press_footswitch(scene.footswitch)

    def tap_tempo(self) -> None:
        self.control(CC_TAP_TEMPO)

    def expression(self, value: int) -> None:
        """Move the expression pedal, 0-127."""
        self.control(CC_EXPRESSION_PEDAL, value)


def verify_rig(
    rig,
    controller: MidiController,
    *,
    program: int | None = None,
    settle_s: float = 0.3,
) -> list[str]:
    """Drive a rig over MIDI after writing it, and report what was exercised.

    This is a *liveness* check on a channel independent of the one that
    performed the write: select the rig, toggle each block to its intended
    state, and step through every scene. It confirms the device accepts the
    commands and does not lock up.

    It is not a read-back. The device does not report block state over MIDI,
    so this cannot prove the rig's contents match the draft - only that the
    rig loads and responds. Anything stronger needs the Remote Editor path.
    """
    import time

    performed: list[str] = []

    if program is not None:
        controller.select_rig(program)
        performed.append(f"program change {program}")
        time.sleep(settle_s)

    for block in sorted(rig.blocks, key=lambda b: b.slot):
        controller.set_block(block.slot, block.enabled)
        performed.append(
            f"slot {block.slot} ({block.model_id}) -> "
            f"{'on' if block.enabled else 'off'}"
        )

    for scene in rig.scenes:
        if scene.footswitch is None:
            performed.append(f"scene '{scene.name}' skipped - no footswitch assigned")
            continue
        controller.activate_scene(scene)
        performed.append(f"scene '{scene.name}' via footswitch {scene.footswitch}")
        time.sleep(settle_s)

    performed.append(
        "NOTE: MIDI is write-only here - the device does not report block "
        "state back, so this confirms the rig responds, not that its contents "
        "match the draft"
    )
    return performed

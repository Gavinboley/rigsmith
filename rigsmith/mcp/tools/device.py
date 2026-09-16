"""Device tools: discovery, protocol probing, USB storage, and MIDI."""

from __future__ import annotations

from ...core import devices
from .._shared import tool


@tool
async def discover_device(include_sweep: bool = True) -> str:
    """Find the HeadRush on the local network.

    Tries the documented `.local` hostname, then mDNS service browsing, then
    a sweep of the local subnet. The device must be powered on, joined to the
    same Wi-Fi network, with Global Settings > Wi-Fi > HeadRush Remote = On.

    Args:
        include_sweep: Also sweep the local subnet if nothing else answers.
            Slower, but works when mDNS is blocked.
    """
    return devices.render_discovery(
        await devices.discover(include_sweep=include_sweep)
    )


@tool
async def probe_protocol(max_assets: int = 200) -> str:
    """Fetch the Remote Editor's web bundle and extract its API surface.

    Read-only: requests the same files a browser would when opening the
    editor, then mines them for endpoints, WebSocket URLs, and message types.
    This is how rigsmith learns the device's undocumented protocol.
    """
    return (await devices.probe(max_assets=max_assets)).render()


@tool
def device_storage_status(path: str | None = None) -> str:
    """Check whether the device's USB storage is mounted, and what is on it.

    Put the device into Menu > USB Transfer with a USB-B cable attached. It
    mounts as a drive named 'HeadRush' holding Rigs, Setlists, Blocks,
    Impulse Responses, Amp Clones, and NAM folders.
    """
    return devices.storage_status(path).describe()


@tool
def device_backup(path: str | None = None) -> str:
    """Copy everything off the device into a timestamped local backup.

    Reads only - the device is not modified. Worth doing before a session of
    experimenting; write_rig also does it automatically.
    """
    return devices.backup(path).describe()


@tool
def install_capture(
    nam_path: str, device_path: str | None = None, overwrite: bool = False
) -> str:
    """Copy a NAM capture onto the device over USB.

    The capture is checked first: the Core loads A2 Full/Lite and A1
    Standard/Lite/Nano/Feather, and anything outside that is refused rather
    than copied and left to fail silently on the device.

    The on-screen TONE3000 browser is usually easier - it downloads straight
    to the device over Wi-Fi with no computer involved. Use this for captures
    you already have locally.
    """
    return devices.install_capture(
        nam_path, device_path=device_path, overwrite=overwrite
    )


# --------------------------------------------------------------------------
# MIDI
# --------------------------------------------------------------------------


@tool
def midi_ports() -> str:
    """List MIDI ports, flagging the HeadRush if it is connected.

    MIDI is the one fully documented interface on the device, which makes it
    useful both for switching rigs during testing and as a verification
    channel independent of whatever performed a write.
    """
    return devices.render_midi_ports()


@tool
def midi_select_rig(program: int, port: str | None = None, channel: int = 0) -> str:
    """Load a rig on the device by MIDI Program Change.

    This is how you put the device on the rig you want to reamp through,
    rather than walking over and doing it by hand between candidates.

    The device's Program-Change-to-rig numbering is not published, so confirm
    what actually loaded before relying on the mapping.
    """
    return devices.select_rig(program, port=port, channel=channel)


@tool
def midi_press_footswitch(
    footswitch: int, port: str | None = None, channel: int = 0
) -> str:
    """Press a footswitch (1-5) over MIDI.

    This is also the only way to activate a scene remotely - the device has
    no direct scene-select control.
    """
    return devices.press_footswitch(footswitch, port=port, channel=channel)


@tool
def midi_tap_tempo(
    bpm: float, taps: int = 4, port: str | None = None, channel: int = 0
) -> str:
    """Set the device's tempo by tapping it at a given BPM.

    Delay and modulation times on a record are the song's tempo, so this is
    worth setting whenever the recipe names one. There is no "set BPM"
    message on this device - CC64 is a tap and the device averages the
    intervals - so the result is close rather than exact.

    Args:
        bpm: Beats per minute, roughly 20-400.
        taps: How many taps to send. Four is the usual minimum for a stable
            average; more is steadier and takes proportionally longer.
    """
    return devices.tap_tempo(bpm, taps=taps, port=port, channel=channel)


@tool
def midi_set_block(
    slot: int, enabled: bool, port: str | None = None, channel: int = 0
) -> str:
    """Turn the block in a slot on or off over MIDI (blocks 1-14).

    Useful for A/B-ing one pedal in the chain without rebuilding the rig,
    and for stepping a rig through states during automated testing.
    """
    return devices.set_block(slot, enabled, port=port, channel=channel)


@tool
def midi_verify_rig(
    draft_id: str,
    program: int | None = None,
    port: str | None = None,
    channel: int = 0,
) -> str:
    """Drive a draft's rig over MIDI to confirm the device responds.

    A liveness check on a channel independent of whatever performed the
    write. It is **not** a read-back: the device does not report block state
    over MIDI, so this proves the rig loads and responds, not that its
    contents match the draft.
    """
    return devices.verify_rig(
        draft_id, program=program, port=port, channel=channel
    ).render()

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
from .errors import Conflict, Invalid, NotFound


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
# Pairing and read-only snapshots
# --------------------------------------------------------------------------


def sessions() -> list[dict]:
    """Every device this workspace is paired with - never the cookie itself."""
    import json

    from ..device.session import Session

    directory = get_settings().workspace / "sessions"
    found = []
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        try:
            session = Session.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, Invalid):
            continue
        found.append(
            {
                "host": session.host,
                "hostname": session.hostname,
                "server": session.server,
                "firmware": session.firmware,
                "paired_at": datetime.fromtimestamp(session.paired_at).isoformat(timespec="seconds") if session.paired_at else None,
            }
        )
    return found


def render_sessions(found: list[dict]) -> str:
    if not found:
        return "Not paired with any device. Pair from the Device panel, or:  rigsmith device pair"
    lines = ["Paired with:"]
    for item in found:
        lines.append(f"  {item['host']}  {item.get('server') or ''}  since {item.get('paired_at') or '?'}".rstrip())
    return "\n".join(lines)


async def pair(host: str | None = None, *, timeout: float = 120.0, on_pin=None):
    """Pair with a Core; ``on_pin`` gets the PIN to show the person at it.

    The device authorises a session cookie, not a user, and only once somebody
    standing at it accepts the PIN on its screen - so this waits for a person.
    """
    from ..device.client import DeviceClient
    from ..protocol.map import get as get_map

    require_writable("save a device session")
    if not host:
        host = (await find()).host
    async with DeviceClient(f"http://{host}", get_map("headrush-core"), session=None) as client:
        return await client.pair(timeout_s=timeout, on_pin=on_pin)


def forget(host: str) -> bool:
    from ..device.session import forget_session

    return forget_session(host)


async def snapshot(host: str | None = None):
    """What is on the device, read over the network: every rig's name and id,
    and the loaded rig in full. Writes nothing to the device."""
    from ..device import snapshot as snap
    from ..device.client import DeviceClient
    from ..protocol.map import get as get_map

    if not host:
        host = (await find()).host
    async with DeviceClient(f"http://{host}", get_map("headrush-core")) as client:
        taken = await snap.take(client)
    return taken, snap.save(taken)


#: The footswitch modes the Core lists for ModeNew1..10, in order.
FOOTSWITCH_MODES = ("Toggle", "Hold", "Scene")


def parse_scenes(values: dict) -> list[dict]:
    """The scene table out of /Evil/Engine/FootSwitch's values.

    Per footswitch: its mode (Toggle, Hold, Scene), label and, for every slot
    that holds a block, the raw SceneN_S_Mode number. Raw on purpose - which
    number means on and which off is what this is for finding out.
    """
    rows = []
    for switch in range(1, 11):
        mode = values.get(f"ModeNew{switch}")
        slots = []
        for slot in range(1, 15):
            effect = values.get(f"Scene{switch}_{slot}_Effect")
            if effect and effect != "Empty Slot":
                slots.append({"slot": slot, "block": effect, "mode": values.get(f"Scene{switch}_{slot}_Mode")})
        rows.append({
            "footswitch": switch,
            "mode": FOOTSWITCH_MODES[mode] if isinstance(mode, int) and 0 <= mode < len(FOOTSWITCH_MODES) else mode,
            "label": values.get(f"UserFootSwitchText{switch}") or values.get(f"FootSwitchText{switch}") or "",
            "slots": slots,
        })
    return rows


def render_scenes(rows: list[dict]) -> str:
    lines = []
    for row in rows:
        if row["mode"] != "Scene" and not any(s["mode"] for s in row["slots"]):
            continue
        settings = ", ".join(f"{s['slot']}:{s['block']}={s['mode']}" for s in row["slots"]) or "no blocks"
        lines.append(f"  FS{row['footswitch']} [{row['mode']}] '{row['label']}': {settings}")
    if not lines:
        return "No scenes are set on the loaded rig."
    return "Scenes on the loaded rig (slot:block=raw mode; 0 is believed to be No Change):\n" + "\n".join(lines)


def learn_scene_modes(rows: list[dict]) -> dict:
    """Which scene mode number means on and which off, from footswitch 1.

    The person sets FS1, by hand, to a scene that turns the first block in
    the chain on and the second off; their two numbers are then the
    answer. Anything ambiguous is refused rather than guessed - a wrong
    mapping would invert every scene written afterwards.
    """
    first = next((row for row in rows if row["footswitch"] == 1), None)
    hint = (
        "On the Core: make footswitch 1 a scene, set the first block in the chain "
        "to on and the second to off, save, then run this again with that rig loaded."
    )
    if first is None or first["mode"] != "Scene":
        raise Invalid("footswitch 1 is not a scene on the loaded rig", hint=hint)
    slots = sorted(first["slots"], key=lambda s: s["slot"])
    if len(slots) < 2:
        raise Invalid("footswitch 1's scene needs at least two blocks to read", hint=hint)
    on, off = slots[0]["mode"], slots[1]["mode"]
    if not isinstance(on, int) or not isinstance(off, int) or on == off or 0 in (on, off):
        raise Invalid(f"footswitch 1 reads {on} and {off} for its first two blocks, which does not tell on from off", hint=hint)
    return {"on": on, "off": off, "no_change": 0}


async def send_live(rig, slot: int, values: dict, *, host: str | None = None) -> dict:
    """Set a few of one block's values on the rig the Core has loaded.

    Only when that rig is this one - the loaded rig's name is the draft's -
    since otherwise the knobs would move on whatever else was loaded. The
    values are read back, and any the device kept its own way are listed.
    Nothing is saved on the device: it is the loaded patch, as when tuning.
    """
    from ..device.client import DeviceClient
    from ..device.writer import plan_rig
    from ..protocol.map import get as get_map
    from ..rig.catalog import get_catalog

    require_writable("turn knobs on the Core")
    placement = next((p for p in plan_rig(rig, get_catalog()) if p.slot == slot), None)
    if placement is None:
        raise NotFound(f"slot {slot} is empty in '{rig.name}'")
    if not host:
        host = (await find()).host
    protocol = get_map("headrush-core")
    async with DeviceClient(f"http://{host}", protocol) as client:
        loaded = await client.call("loaded_rig")
        name = loaded.get(protocol.field("rig_name")) if isinstance(loaded, dict) else None
        if (name or "").strip().lower() != rig.name.strip().lower():
            raise Conflict(
                f"the Core has '{name or 'another rig'}' loaded, not '{rig.name}'",
                hint="Write the draft to the device, or load it there, then nudge live.",
            )
        await client.call("set_block", block=placement.node, properties=values)
        back = await client.call("get_block", block=placement.node)
    back = back if isinstance(back, dict) else {}
    kept = [n for n, v in values.items() if not DeviceClient._same(v, back.get(n))]
    return {"node": placement.node, "sent": values, "refused": kept}


async def read_scenes(host: str | None = None) -> list[dict]:
    """Read the loaded rig's scene table - read only."""
    from ..device.client import DeviceClient
    from ..protocol.map import get as get_map

    if not host:
        host = (await find()).host
    async with DeviceClient(f"http://{host}", get_map("headrush-core")) as client:
        values = await client.call("get_properties", node="/Evil/Engine/FootSwitch")
    return parse_scenes(values if isinstance(values, dict) else {})


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


# --------------------------------------------------------------------------
# pulling audio off the device
# --------------------------------------------------------------------------


def list_device_audio(path: str | Path | None = None):
    """Audio files sitting on the device's USB storage."""
    from ..device.usbmsc import find_audio, find_device_volume

    volume = find_device_volume(path)
    return volume, find_audio(volume)


def render_device_audio(volume, paths) -> str:
    from ..device.usbmsc import render_audio_listing

    return render_audio_listing(paths, volume)


def import_device_audio(
    *,
    path: str | Path | None = None,
    kind: str = "loop",
    guitar: str | None = None,
    pickup: str | None = None,
    note: str | None = None,
    only: list[str] | None = None,
):
    """Copy recordings off the device into the takes library.

    Defaults to ``loop`` rather than ``di``, because whatever the pedal
    recorded has already been through the pedal. Calling that a DI and
    reamping through it stacks two amps.
    """
    from . import takes

    require_writable("import audio from the device")
    volume, found = list_device_audio(path)
    if only:
        wanted = {name.lower() for name in only}
        found = [p for p in found if p.name.lower() in wanted or p.stem.lower() in wanted]

    imported = []
    skipped = []
    for source in found:
        try:
            imported.append(
                takes.add(
                    source,
                    kind=kind,
                    origin="looper",
                    guitar=guitar,
                    pickup=pickup,
                    note=note,
                )
            )
        except Conflict as exc:
            skipped.append(f"{source.name}: {exc.message}")
        except (Invalid, NotFound) as exc:
            skipped.append(f"{source.name}: {exc.message}")
    return imported, skipped


def render_import(imported, skipped) -> str:
    if not imported and not skipped:
        return "Nothing to import."
    lines = []
    if imported:
        lines.append(f"Imported {len(imported)} take(s):")
        lines.extend(f"  {take.describe()}" for take in imported)
        lines.append(
            "\nThese came off the pedal, so they have already been through "
            "whatever rig was loaded. Good as a reference to match; not "
            "something to reamp through - that would stack two amps."
        )
    if skipped:
        lines.append("\nskipped:")
        lines.extend(f"  {item}" for item in skipped)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the catalog
# --------------------------------------------------------------------------


def catalog_status() -> dict:
    """How much rigsmith knows about the Core's models and controls.

    ``measured`` counts the continuous controls whose taper (the curve from
    the wire's 0-1 to what the knob reads) has been measured; every other
    reading is worked out as if the control were linear.
    """
    from ..rig.catalog import get_catalog

    cat = get_catalog(reload=True)
    continuous = [p for spec in cat.models.values() for p in spec.params if p.normalised]
    measured = sum(1 for p in continuous if p.taper in ("linear", "log"))
    return {
        "source": cat.source,
        "models": len(cat.models),
        "verified": cat.is_verified,
        "captured_at": cat.captured_at,
        "controls": len(continuous),
        "measured": measured,
        "completeness": cat.completeness_report(),
        "warnings": list(cat.warnings[:5]),
    }


async def calibrate_catalog(host: str | None = None, blocks: list[str] | None = None) -> dict:
    """Measure how each control's wire value maps to what the device shows.

    Reads, for every block in the loaded rig (or the ones named), the wire
    values and the device's own serialised readings side by side, and works
    out which curve fits; the measured tapers are saved into the workspace
    catalog. Nothing is written to the device. A control sitting at an end of
    its range cannot be told apart - move it on the device and run again.
    """
    import json

    from ..device.client import DeviceClient
    from ..protocol.calibrate import calibrate_model, samples_from
    from ..protocol.map import get as get_map
    from ..rig.catalog import get_catalog

    cat = get_catalog(reload=True)
    if not cat.is_verified:
        raise Invalid(
            "the catalog is not extracted yet",
            hint="Run 'rigsmith catalog extract --subtree DIR' first.",
        )
    if not host:
        host = (await find()).host
    lines: list[str] = []
    measured = 0
    async with DeviceClient(f"http://{host}", get_map("headrush-core")) as client:
        wanted = list(blocks or [])
        if not wanted:
            chain = await client.call("get_chain")
            table = (await client.call("module_table"))["ModuleTypes"]
            for slot in range(1, 15):
                index = chain.get(f"ModuleType{slot}")
                if isinstance(index, int) and 0 < index < len(table):
                    name = table[index]
                    if name not in wanted:
                        wanted.append(name)
        if not wanted:
            return {"lines": ["Nothing in the loaded rig to calibrate against."], "measured": 0, "saved": None, "blocks": []}
        for name in wanted:
            spec = cat.models.get(name)
            if spec is None:
                lines.append(f"  {name}: not in the catalog, skipped")
                continue
            try:
                wire = await client.call("get_block", block=name.replace(" ", "_"))
                raw = await client.call("serialize_block", block=name, instance=0)
            except Exception as exc:
                lines.append(f"  {name}: {exc}")
                continue
            try:
                serialised = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                lines.append(f"  {name}: the device sent something unreadable")
                continue
            result = calibrate_model(spec, samples_from(wire, serialised))
            measured += result.measured
            lines.append(f"{name}: {result.render()}")
            shared = cat.share_tapers(name)
            if shared:
                lines.append(f"  and the same curves for {shared} model(s) selected on {name}")
    saved = None
    if measured:
        require_writable("save the calibrated catalog")
        saved = str(cat.save(get_settings().workspace / "catalog.json"))
    return {"lines": lines, "measured": measured, "saved": saved, "blocks": wanted}

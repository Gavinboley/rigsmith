"""How the loop is wired, written down once.

A route names the devices and channels a reamp goes out on and comes back
in on. It is stored rather than asked for each time because getting it right
takes a minute with the manual open, and getting it wrong is silent: the
Core's USB channels 1-2 bypass the rig entirely, so a reamp sent there comes
back perfectly clean and scores as a terrible match for reasons that have
nothing to do with tone.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.errors import Invalid, NotFound

KINDS = {
    "analog": "interface output -> cable -> Core guitar input; Core output -> interface input",
    "usb-reamp": "the Core's USB audio: computer channel 3 into the rig, channels 1-2 back",
    "hybrid": "interface output -> Core guitar input; recorded from the Core's USB (channel 5 shows what arrived)",
    "custom": "anything else - the channels are as written",
}

#: The Core's USB layout, from the v5.2 manual. Computer -> Core channels 1-2
#: go straight to the outputs; 3 is the reamp input through the rig.
CORE_REAMP_OUTPUT = 3
CORE_WET_INPUTS = (1, 2)
CORE_DRY_INPUT = 5


@dataclass
class LoopRoute:
    route_id: str
    kind: str = "analog"
    output_device: str = ""
    output_channels: list[int] = field(default_factory=lambda: [1])
    input_device: str = ""
    input_channels: list[int] = field(default_factory=lambda: [1])
    """Where the processed signal comes back. Two channels are summed."""
    reference_device: str | None = None
    reference_channel: int | None = None
    """Where the DI can be heard *as the Core received it* - its USB channel
    5 on a hybrid route. Optional; makes level calibration exact."""
    sample_rate: int = 48000
    hostapi: str | None = None
    send_gain_db: float = 0.0
    """Digital gain on the DI before it leaves, set by calibration."""
    note: str | None = None
    backend: str = "sounddevice"

    @property
    def same_device(self) -> bool:
        return self.output_device == self.input_device

    @property
    def same_clock(self) -> bool:
        """One box, even when Windows lists its input and output separately."""
        if self.same_device:
            return True
        tag = hardware_tag(self.output_device)
        return tag is not None and tag == hardware_tag(self.input_device)

    def check(self) -> list[str]:
        """What is wrong or worth knowing about this wiring."""
        problems: list[str] = []
        if self.kind not in KINDS:
            problems.append(f"unknown route kind '{self.kind}'")
        if not self.output_device or not self.input_device:
            problems.append("an output and an input device are both needed")
        if self.kind == "usb-reamp":
            if CORE_REAMP_OUTPUT not in self.output_channels:
                problems.append(
                    "a USB reamp has to go out on channel 3 - channels 1-2 go "
                    "straight to the Core's outputs and never reach the rig"
                )
            if any(c in (1, 2) for c in self.output_channels):
                problems.append(
                    "channels 1-2 to the Core bypass the rig; sending the DI there "
                    "mixes an unprocessed copy into the result"
                )
        if not self.same_clock:
            problems.append(
                "output and input are different devices, so their clocks drift; "
                "each capture is resampled from its sync marks to undo it"
            )
        return problems

    def describe(self) -> str:
        out = ",".join(str(c) for c in self.output_channels)
        back = ",".join(str(c) for c in self.input_channels)
        lines = [
            f"{self.route_id} [{self.kind}]  {self.output_device} out {out} -> "
            f"{self.input_device} in {back}  @ {self.sample_rate} Hz"
        ]
        if self.reference_channel:
            lines.append(f"  reference: {self.reference_device} in {self.reference_channel}")
        if self.send_gain_db:
            lines.append(f"  send gain {self.send_gain_db:+.1f} dB")
        lines.extend(f"  note: {p}" for p in self.check())
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["problems"] = self.check()
        data["describe"] = self.describe()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "LoopRoute":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


class RouteStore:
    """Routes on disk, plus which one is the default."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / "routes.json"

    def _load(self) -> dict:
        if not self.path.is_file():
            return {"default": None, "routes": {}}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"default": None, "routes": {}}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list(self) -> list[LoopRoute]:
        return [LoopRoute.from_dict(r) for r in self._load()["routes"].values()]

    def default_id(self) -> str | None:
        return self._load().get("default")

    def get(self, route_id: str | None = None) -> LoopRoute:
        data = self._load()
        wanted = route_id or data.get("default")
        if not wanted:
            raise NotFound(
                "no loop route set up yet",
                hint="Create one with:  rigsmith loop route new <name> --kind analog ...\n"
                "Or see suggestions with:  rigsmith loop suggest",
            )
        if wanted not in data["routes"]:
            raise NotFound(f"no loop route '{wanted}'", hint="Routes: " + ", ".join(data["routes"]) or "none")
        return LoopRoute.from_dict(data["routes"][wanted])

    def put(self, route: LoopRoute, *, make_default: bool = False) -> LoopRoute:
        if route.kind not in KINDS:
            raise Invalid(f"unknown route kind '{route.kind}'", hint="Kinds: " + ", ".join(KINDS))
        data = self._load()
        data["routes"][route.route_id] = asdict(route)
        if make_default or not data.get("default"):
            data["default"] = route.route_id
        self._save(data)
        return route

    def delete(self, route_id: str) -> None:
        data = self._load()
        if route_id not in data["routes"]:
            raise NotFound(f"no loop route '{route_id}'")
        del data["routes"][route_id]
        if data.get("default") == route_id:
            data["default"] = next(iter(data["routes"]), None)
        self._save(data)

    def set_default(self, route_id: str) -> None:
        data = self._load()
        if route_id not in data["routes"]:
            raise NotFound(f"no loop route '{route_id}'")
        data["default"] = route_id
        self._save(data)


#: Host APIs in the order they are worth using for a loop. Windows lists
#: every device once per API; WASAPI is the one that neither resamples
#: silently (MME, DirectSound) nor drops the box's name (WDM-KS).
HOSTAPI_PREFERENCE = ("ASIO", "Core Audio", "Windows WASAPI", "ALSA", "MME", "Windows DirectSound", "Windows WDM-KS")

#: Names that are the operating system's mixer, not a box with jacks.
GENERIC = re.compile(r"sound mapper|primary sound|stereo mix|what u hear|^default$|^sysdefault|^pulse|^pipewire", re.I)
#: Ports that carry the computer's own audio back, not a cable.
LOOPBACK = re.compile(r"loopback|stereo mix|what u hear|monitor of", re.I)
INPUT_PORT = re.compile(r"analog|analogue|input|line|mic|inst|\bin\b", re.I)
OUTPUT_PORT = re.compile(r"playback 1|output 1|analog|analogue|line out|main|speakers|monitor|out", re.I)


def hardware_tag(name: str) -> str | None:
    """The box a Windows device name belongs to: ``Analog 1/2 (E2x2)`` -> ``E2x2``.

    WDM-KS repeats the port instead (``Analog 1/2 (Analog 1/2)``), which says
    nothing about the box, so that is no tag.
    """
    match = re.match(r"^(.*?)\s*\(([^()]+)\)\s*$", name or "")
    if not match or match.group(1).strip().lower() == match.group(2).strip().lower():
        return None
    return match.group(2).strip()


def _rank(hostapi: str | None) -> int:
    return HOSTAPI_PREFERENCE.index(hostapi) if hostapi in HOSTAPI_PREFERENCE else len(HOSTAPI_PREFERENCE)


def hardware(devices) -> list[dict]:
    """The boxes behind the device list, one entry each.

    Windows shows a two-in, six-out interface as seven devices four times
    over; this is the view a person thinks in - the box, its ports, and the
    host APIs it can be reached through.
    """
    boxes: dict[str, dict] = {}
    for device in devices:
        if GENERIC.search(device.name):
            continue
        tag = hardware_tag(device.name)
        if tag is None and device.hostapi == "Windows WDM-KS":
            continue  # the same ports again, without the box's name
        key = tag or device.name
        box = boxes.setdefault(key, {"name": key, "inputs": [], "outputs": [], "hostapis": [], "headrush": False})
        port = device.name if tag is None else device.name[: device.name.rfind("(")].strip()
        if device.inputs and port not in box["inputs"]:
            box["inputs"].append(port)
        if device.outputs and port not in box["outputs"]:
            box["outputs"].append(port)
        if device.hostapi and device.hostapi not in box["hostapis"]:
            box["hostapis"].append(device.hostapi)
        box["headrush"] = box["headrush"] or "headrush" in device.name.lower()
    for box in boxes.values():
        box["hostapis"].sort(key=_rank)
    return sorted(boxes.values(), key=lambda b: (not b["headrush"], not (b["inputs"] and b["outputs"]), b["name"].lower()))


def _pairs(devices) -> list[tuple[str, object, object]]:
    """(box, input device, output device) for boxes Windows splits in two.

    One host API per box - the best one that has both directions - and the
    ports a cable would be in: the first analog input, the first main output,
    never a loopback.
    """
    found = []
    by_tag: dict[str, list] = {}
    for device in devices:
        tag = hardware_tag(device.name)
        if tag and not GENERIC.search(device.name):
            by_tag.setdefault(tag, []).append(device)
    for tag, members in by_tag.items():
        for hostapi in sorted({d.hostapi for d in members}, key=_rank):
            here = [d for d in members if d.hostapi == hostapi]
            ins = [d for d in here if d.inputs and not LOOPBACK.search(d.name)]
            outs = [d for d in here if d.outputs and not LOOPBACK.search(d.name)]
            if not ins or not outs:
                continue
            ins.sort(key=lambda d: (not INPUT_PORT.search(d.name), d.name))
            outs.sort(key=lambda d: (not OUTPUT_PORT.search(d.name), d.name))
            found.append((tag, ins[0], outs[0]))
            break
    return found


def _rate(device) -> int:
    """The box's own rate when it is one the Core speaks - so nothing resamples."""
    rate = int(round(getattr(device, "sample_rate", 0) or 0))
    return rate if rate in (44100, 48000, 96000) else 48000


def _slug(name: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-")[:24]


def suggest(devices) -> list[LoopRoute]:
    """Routes that make sense for the devices that are actually connected."""
    devices = [d for d in devices if not GENERIC.search(d.name)]
    devices.sort(key=lambda d: _rank(d.hostapi))
    suggestions: list[LoopRoute] = []
    backend = "virtual" if any(d.hostapi == "Virtual" for d in devices) else "sounddevice"

    # A box is (name, input device, output device, host API); a box Windows
    # lists as one device has the same device on both sides.
    boxes = []
    seen: set[str] = set()
    for device in devices:
        if device.inputs >= 1 and device.outputs >= 1 and device.name not in seen:
            seen.add(device.name)
            boxes.append((device.name, device, device))
    covered = {hardware_tag(name) or name for name, _, _ in boxes}
    for tag, inp, out in _pairs(devices):
        if tag not in covered:
            covered.add(tag)
            boxes.append((tag, inp, out))

    def is_core(box) -> bool:
        name, inp, out = box
        return "headrush" in f"{name} {inp.name} {out.name}".lower()

    cores = [b for b in boxes if is_core(b) and b[1].inputs >= 2 and b[2].outputs >= CORE_REAMP_OUTPUT]
    interfaces = [b for b in boxes if not is_core(b)]

    for name, inp, out in cores[:1]:
        suggestions.append(
            LoopRoute(
                route_id="core-usb",
                kind="usb-reamp",
                output_device=out.name,
                output_channels=[CORE_REAMP_OUTPUT],
                input_device=inp.name,
                input_channels=list(CORE_WET_INPUTS),
                hostapi=out.hostapi,
                sample_rate=_rate(out),
                note="Set the Core's USB Mode to Reamp. All digital, one clock.",
                backend=backend,
            )
        )
    for name, inp, out in interfaces[:2]:
        slug = _slug(name)
        suggestions.append(
            LoopRoute(
                route_id=f"analog-{slug}",
                kind="analog",
                output_device=out.name,
                output_channels=[1],
                input_device=inp.name,
                input_channels=[1],
                hostapi=out.hostapi,
                sample_rate=_rate(out),
                note=f"{name} output 1 to the Core's guitar input (a reamp box if you have one); "
                "the Core's 1/4\" output L back to input 1.",
                backend=backend,
            )
        )
        for core_name, core_in, _ in cores[:1]:
            if core_in.hostapi != out.hostapi and backend != "virtual":
                continue  # two streams on two host APIs is asking for trouble
            note = (
                "Output 1 to the Core's guitar input, recorded over the Core's USB. "
                "Channel 5 shows the level the Core actually received."
            )
            if _rate(out) != _rate(core_in):
                note += (
                    f" {name} runs at {_rate(out)} Hz and the Core at {_rate(core_in)} Hz - "
                    "set both to the same rate in the system's sound settings first."
                )
            suggestions.append(
                LoopRoute(
                    route_id=f"hybrid-{slug}",
                    kind="hybrid",
                    output_device=out.name,
                    output_channels=[1],
                    input_device=core_in.name,
                    input_channels=list(CORE_WET_INPUTS),
                    reference_device=core_in.name,
                    reference_channel=CORE_DRY_INPUT,
                    hostapi=out.hostapi,
                    sample_rate=_rate(core_in),
                    note=note,
                    backend=backend,
                )
            )
    return suggestions

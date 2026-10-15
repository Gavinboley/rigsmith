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
        if not self.same_device:
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


def suggest(devices) -> list[LoopRoute]:
    """Routes that make sense for the devices that are actually connected."""
    suggestions: list[LoopRoute] = []
    cores = [d for d in devices if d.looks_like_headrush and d.inputs >= 2 and d.outputs >= CORE_REAMP_OUTPUT]
    interfaces = [d for d in devices if not d.looks_like_headrush and d.inputs >= 1 and d.outputs >= 1]
    backend = "virtual" if any(d.hostapi == "Virtual" for d in devices) else "sounddevice"

    # One entry per device name - Windows lists each device once per host API.
    def unique(items):
        seen, out = set(), []
        for item in items:
            if item.name not in seen:
                seen.add(item.name)
                out.append(item)
        return out

    for core in unique(cores):
        suggestions.append(
            LoopRoute(
                route_id="core-usb",
                kind="usb-reamp",
                output_device=core.name,
                output_channels=[CORE_REAMP_OUTPUT],
                input_device=core.name,
                input_channels=list(CORE_WET_INPUTS),
                hostapi=core.hostapi,
                note="Set the Core's USB Mode to Reamp. All digital, one clock.",
                backend=backend,
            )
        )
    for interface in unique(interfaces)[:2]:
        slug = "".join(ch if ch.isalnum() else "-" for ch in interface.name.lower()).strip("-")[:24]
        suggestions.append(
            LoopRoute(
                route_id=f"analog-{slug}",
                kind="analog",
                output_device=interface.name,
                output_channels=[1],
                input_device=interface.name,
                input_channels=[1],
                hostapi=interface.hostapi,
                note="Output 1 to the Core's guitar input (a reamp box if you have one); "
                "the Core's 1/4\" output L back to input 1.",
                backend=backend,
            )
        )
        for core in unique(cores)[:1]:
            suggestions.append(
                LoopRoute(
                    route_id=f"hybrid-{slug}",
                    kind="hybrid",
                    output_device=interface.name,
                    output_channels=[1],
                    input_device=core.name,
                    input_channels=list(CORE_WET_INPUTS),
                    reference_device=core.name,
                    reference_channel=CORE_DRY_INPUT,
                    hostapi=interface.hostapi,
                    note="Output 1 to the Core's guitar input, recorded over the Core's USB. "
                    "Channel 5 shows the level the Core actually received.",
                    backend=backend,
                )
            )
    return suggestions

"""Protocol maps: the device's API expressed as data rather than code.

The client that talks to a HeadRush knows *what* it wants to do - create a
rig, add a block, set a parameter. A map says how that translates into HTTP
for one particular device and firmware: which method, which path, what shape
the body takes, what the fields are called.

Keeping it as data rather than hardcoding it matters because the real
protocol is still unknown. The client can be written, tested and finished
today against the simulator's map, and when the real one is captured it drops
in as another JSON file. Nothing about the client changes.

Maps carry ``verified``: false means invented or inferred, and the client
says so rather than letting a guess pass for knowledge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.errors import Invalid, NotFound

MAPS_DIR = Path(__file__).resolve().parent / "maps"


@dataclass
class Operation:
    """One thing the device can be asked to do."""

    name: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    unwrap: str | None = None
    """Key to pull the payload out of, when the device wraps its responses."""

    def render_path(self, values: dict[str, Any]) -> str:
        try:
            return self.path.format(**values)
        except KeyError as exc:
            raise Invalid(
                f"operation '{self.name}' needs {exc} to build its path",
                hint=f"Path template: {self.path}",
            ) from exc

    def render_body(self, values: dict[str, Any]) -> dict[str, Any] | None:
        """Fill the body template, dropping anything the caller left out.

        Placeholders are whole values rather than string interpolation, so a
        dict or a list survives intact instead of being stringified.
        """
        if self.body is None:
            return None

        rendered: dict[str, Any] = {}
        for key, template in self.body.items():
            if (
                isinstance(template, str)
                and template.startswith("{")
                and template.endswith("}")
            ):
                source = template[1:-1]
                if source in values and values[source] is not None:
                    rendered[key] = values[source]
            else:
                rendered[key] = template
        return rendered


@dataclass
class ProtocolMap:
    """How to speak to one device and firmware."""

    name: str
    base: str = ""
    firmware: str | None = None
    description: str = ""
    verified: bool = False
    field_names: dict[str, str] = field(default_factory=dict)
    operations: dict[str, Operation] = field(default_factory=dict)
    source_path: Path | None = None

    def operation(self, name: str) -> Operation:
        operation = self.operations.get(name)
        if operation is None:
            known = ", ".join(sorted(self.operations)) or "none"
            raise NotFound(
                f"protocol map '{self.name}' has no operation '{name}'",
                hint=f"It supports: {known}",
            )
        return operation

    def supports(self, name: str) -> bool:
        return name in self.operations

    def field(self, logical: str) -> str:
        """The device's name for one of our field names."""
        return self.field_names.get(logical, logical)

    def url_for(self, name: str, values: dict[str, Any]) -> str:
        return self.base.rstrip("/") + self.operation(name).render_path(values)

    def caveat(self) -> str:
        """What to say about this map's trustworthiness."""
        if self.verified:
            return ""
        return (
            f"Protocol map '{self.name}' is UNVERIFIED - it was invented or "
            "inferred, not captured from a real device. Anything it does on "
            "hardware is a guess."
        )

    def render(self) -> str:
        lines = [
            f"{self.name}  (firmware {self.firmware or 'unknown'})",
            f"  base: {self.base or '/'}",
            f"  verified: {self.verified}",
        ]
        if self.description:
            lines.append(f"  {self.description}")
        lines.append(f"  {len(self.operations)} operation(s):")
        for name, operation in sorted(self.operations.items()):
            lines.append(f"    {operation.method:6} {self.base}{operation.path}   ({name})")
        caveat = self.caveat()
        if caveat:
            lines.append(f"\n  {caveat}")
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, data: dict, *, source: Path | None = None) -> "ProtocolMap":
        if "operations" not in data:
            raise Invalid("a protocol map needs an 'operations' object")

        operations = {}
        for name, spec in data["operations"].items():
            if "method" not in spec or "path" not in spec:
                raise Invalid(f"operation '{name}' needs a method and a path")
            operations[name] = Operation(
                name=name,
                method=spec["method"].upper(),
                path=spec["path"],
                body=spec.get("body"),
                unwrap=spec.get("unwrap"),
            )

        return cls(
            name=data.get("name", source.stem if source else "unnamed"),
            base=data.get("base", ""),
            firmware=data.get("firmware"),
            description=data.get("description", ""),
            verified=bool(data.get("verified", False)),
            field_names=dict(data.get("field_names") or {}),
            operations=operations,
            source_path=source,
        )

    @classmethod
    def load(cls, path: str | Path) -> "ProtocolMap":
        path = Path(path)
        if not path.exists():
            raise NotFound(f"no protocol map at {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise Invalid(f"{path.name} is not valid JSON: {exc}") from exc
        return cls.from_dict(data, source=path)


def available() -> list[ProtocolMap]:
    """Every map shipped with rigsmith, verified ones first."""
    maps = []
    for path in sorted(MAPS_DIR.glob("*.json")):
        try:
            maps.append(ProtocolMap.load(path))
        except (Invalid, NotFound):
            continue
    maps.sort(key=lambda m: (not m.verified, m.name))
    return maps


def get(name: str) -> ProtocolMap:
    """Load a map by name."""
    for candidate in available():
        if candidate.name == name:
            return candidate
    known = ", ".join(m.name for m in available()) or "none"
    raise NotFound(f"no protocol map '{name}'", hint=f"Available: {known}")


def for_firmware(firmware: str | None) -> ProtocolMap:
    """Pick the map matching a firmware version.

    Fails loudly on an unknown version rather than falling back to a map
    written for a different one: issuing requests shaped for firmware 5.1 at
    a device running something else is how a rig gets corrupted.
    """
    maps = available()
    if not maps:
        raise NotFound(
            "no protocol maps are installed",
            hint="Capture one with:  rigsmith protocol record",
        )

    if firmware:
        for candidate in maps:
            if candidate.firmware == firmware:
                return candidate
        # A major.minor match is good enough; a patch release is unlikely to
        # move endpoints, and refusing one would be needlessly brittle.
        wanted = ".".join(firmware.split(".")[:2])
        for candidate in maps:
            if candidate.firmware and candidate.firmware.startswith(wanted):
                return candidate

        known = ", ".join(m.firmware or m.name for m in maps)
        raise NotFound(
            f"no protocol map for firmware {firmware}",
            hint=(
                f"Maps available for: {known}. Capture this firmware's "
                "protocol with:  rigsmith protocol record"
            ),
        )

    verified = [m for m in maps if m.verified]
    if verified:
        return verified[0]
    raise NotFound(
        "the device did not report a firmware version, and no verified map "
        "is installed",
        hint=(
            "Pass a map explicitly, or capture one with: "
            " rigsmith protocol record"
        ),
    )

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
class Step:
    """One call in a procedure, and what to remember from it."""

    operation: str
    with_values: dict[str, Any] = field(default_factory=dict)
    """Extra values for this step, templated against the running context."""
    bind: str | None = None
    """Name to store this step's result under, for later steps to use."""
    optional: bool = False
    """Skip rather than fail when the map has no such operation."""


@dataclass
class Procedure:
    """An ordered sequence of calls, with state carried between them.

    Some devices do not model a rig as a resource you POST. The Core does not:
    you ask it to make a new rig, then set properties on the rig that is now
    loaded, then ask it to save. Nothing in that carries an id in a body - the
    state is *which rig is open*, and it lives on the device between calls.

    A single :class:`Operation` cannot describe that, and writing it as Python
    puts the shape of one firmware's API back into code, which is the thing
    maps exist to avoid. So a procedure names the steps in order, and each
    step can bind its result for later ones to read.
    """

    name: str
    steps: list[Step] = field(default_factory=list)
    description: str = ""


class _Missing:
    """Distinguishes 'absent' from 'present and None'."""


_MISSING = _Missing()


def resolve_template(template: Any, values: dict[str, Any]) -> Any:
    """Substitute ``{name}`` placeholders, keeping non-string values whole.

    A placeholder is the entire string or it is not a placeholder at all:
    ``"{params}"`` yields the dict, where ``"slot {slot}"`` would yield a
    string. That is deliberate - a body field that needs to be a dict must
    survive as one, and there is no case here for building a string out of
    several values.
    """
    if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
        key = template[1:-1]
        return values.get(key, _MISSING) if key in values else _MISSING
    if isinstance(template, list):
        return [resolve_template(item, values) for item in template]
    if isinstance(template, dict):
        resolved = {}
        for key, item in template.items():
            value = resolve_template(item, values)
            if value is not _MISSING:
                resolved[key] = value
        return resolved
    return template


@dataclass
class Operation:
    """One thing the device can be asked to do."""

    name: str
    method: str
    path: str
    body: dict[str, Any] | str | None = None
    """A template for the body. Usually an object whose fields are filled in;
    a bare ``"{name}"`` when the body *is* one caller-supplied value, which is
    how the object tree's property writes work - the body is the property
    object itself, with no envelope around it."""
    args: list[Any] | None = None
    """Positional arguments, for a device that calls methods rather than
    exposing resources. Rendered into ``{arg_key: [...]}``."""
    arg_key: str = "arguments"
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
        if self.args is not None:
            return {self.arg_key: self.render_args(values)}
        if self.body is None:
            return None

        if isinstance(self.body, str):
            whole = resolve_template(self.body, values)
            if whole is _MISSING:
                raise Invalid(
                    f"operation '{self.name}' needs {self.body} for its body",
                    hint="The whole body is that one value; there is nothing to send without it.",
                )
            return whole

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

    def render_args(self, values: dict[str, Any]) -> list[Any]:
        """Fill the positional-argument template.

        Unlike a body, a missing value is fatal rather than dropped. Omitting
        a named field sends a smaller object; omitting a positional argument
        shifts every argument after it, so ``saveRigAs(name, colour)`` quietly
        becomes ``saveRigAs(colour)`` and the device is asked to do something
        nobody intended.
        """
        rendered: list[Any] = []
        for position, template in enumerate(self.args or []):
            value = resolve_template(template, values)
            if value is _MISSING:
                raise Invalid(
                    f"operation '{self.name}' needs {template} for argument "
                    f"{position + 1}",
                    hint=(
                        "Positional arguments cannot be left out - dropping "
                        "one shifts the rest and calls a different method "
                        "than intended."
                    ),
                )
            rendered.append(value)
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
    procedures: dict[str, Procedure] = field(default_factory=dict)
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

    def procedure(self, name: str) -> Procedure:
        found = self.procedures.get(name)
        if found is None:
            known = ", ".join(sorted(self.procedures)) or "none"
            raise NotFound(
                f"protocol map '{self.name}' has no procedure '{name}'",
                hint=f"It defines: {known}",
            )
        return found

    def has_procedure(self, name: str) -> bool:
        return name in self.procedures

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
        if self.procedures:
            lines.append(f"  {len(self.procedures)} procedure(s):")
            for name, proc in sorted(self.procedures.items()):
                chain = " -> ".join(step.operation for step in proc.steps)
                lines.append(f"    {name}: {chain}")
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
                args=spec.get("args"),
                arg_key=spec.get("arg_key", "arguments"),
                unwrap=spec.get("unwrap"),
            )

        procedures = {}
        for name, spec in (data.get("procedures") or {}).items():
            steps = []
            for raw in spec.get("steps") or []:
                if "operation" not in raw:
                    raise Invalid(f"procedure '{name}' has a step with no operation")
                steps.append(
                    Step(
                        operation=raw["operation"],
                        with_values=dict(raw.get("with") or {}),
                        bind=raw.get("bind"),
                        optional=bool(raw.get("optional", False)),
                    )
                )
            if not steps:
                raise Invalid(f"procedure '{name}' has no steps")
            procedures[name] = Procedure(
                name=name, steps=steps, description=spec.get("description", "")
            )

        return cls(
            name=data.get("name", source.stem if source else "unnamed"),
            base=data.get("base", ""),
            firmware=data.get("firmware"),
            description=data.get("description", ""),
            verified=bool(data.get("verified", False)),
            field_names=dict(data.get("field_names") or {}),
            operations=operations,
            procedures=procedures,
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

"""Layer A: the native client that talks to a HeadRush over HTTP.

Driven entirely by a :mod:`rigsmith.protocol.map`, so the same code works
against the simulator today and the real device the moment its protocol is
captured. Nothing about the device is hardcoded here - only the *operations*
rigsmith wants to perform.

The write path is deliberately conservative:

* the map's ``verified`` flag is surfaced, so an unverified map cannot
  quietly pass for knowledge;
* a write builds the rig block by block and stops at the first refusal,
  rather than pressing on and leaving something half-built;
* what actually happened is reported step by step, because "it failed" after
  nine successful writes is not enough to know what state the device is in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.errors import Invalid, NotFound, Unavailable
from ..protocol.map import ProtocolMap
from ..rig.model import Rig, SceneBlockState


@dataclass
class Step:
    """One operation performed against the device."""

    operation: str
    ok: bool
    detail: str = ""
    status: int | None = None

    def render(self) -> str:
        mark = "ok  " if self.ok else "FAIL"
        line = f"  {mark} {self.operation}"
        if self.detail:
            line += f"  {self.detail}"
        return line


@dataclass
class WriteReport:
    """What a write to the device actually did."""

    rig_name: str
    rig_id: Any = None
    steps: list[Step] = field(default_factory=list)
    verified_map: bool = False
    map_name: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(step.ok for step in self.steps)

    @property
    def completed(self) -> int:
        return sum(1 for step in self.steps if step.ok)

    def render(self) -> str:
        header = (
            f"Wrote '{self.rig_name}' to the device"
            if self.ok
            else f"Write of '{self.rig_name}' stopped after {self.completed} step(s)"
        )
        lines = [header]
        if self.rig_id is not None:
            lines.append(f"  device rig id: {self.rig_id}")
        lines.extend(step.render() for step in self.steps)

        if not self.verified_map:
            lines.append(
                f"\n  The protocol map '{self.map_name}' is UNVERIFIED - it was "
                "invented or inferred rather than captured from a real device. "
                "Check the device before trusting this."
            )
        if not self.ok:
            lines.append(
                "\n  Stopped at the first refusal rather than pressing on, so "
                "the device holds a partial rig. Review it before retrying."
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "rig_name": self.rig_name,
            "rig_id": self.rig_id,
            "ok": self.ok,
            "completed": self.completed,
            "verified_map": self.verified_map,
            "map_name": self.map_name,
            "steps": [
                {"operation": s.operation, "ok": s.ok, "detail": s.detail}
                for s in self.steps
            ],
        }


class DeviceClient:
    """An async HTTP client for a HeadRush, driven by a protocol map."""

    def __init__(
        self,
        base_url: str,
        protocol: ProtocolMap,
        *,
        timeout: float = 15.0,
        transport: Any = None,
    ) -> None:
        """
        Args:
            transport: An httpx transport to use instead of the network.
                Lets tests drive a simulator app in-process, so the client is
                covered without binding a port.
        """
        self.base_url = base_url.rstrip("/")
        self.protocol = protocol
        self.timeout = timeout
        self.transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "DeviceClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- plumbing ---------------------------------------------------------

    async def call(self, operation: str, **values: Any) -> Any:
        """Perform one mapped operation."""
        if self._client is None:
            raise Unavailable(
                "the device client is not open",
                hint="Use it as an async context manager.",
            )

        spec = self.protocol.operation(operation)
        url = self.protocol.url_for(operation, values)
        body = spec.render_body(values)

        try:
            response = await self._client.request(
                spec.method, url, json=body if body is not None else None
            )
        except httpx.HTTPError as exc:
            raise Unavailable(
                f"{operation}: could not reach the device ({exc})",
                # url is already absolute; prefixing base_url again produced
                # a hint nobody could paste anywhere.
                hint=f"Tried {spec.method} {url}",
            ) from exc

        if response.status_code == 404:
            raise NotFound(f"{operation}: the device returned 404 for {url}")
        if response.status_code >= 400:
            detail = response.text[:300]
            raise Invalid(
                f"{operation}: the device refused this "
                f"({response.status_code}) - {detail}"
            )

        if not response.content:
            return None
        try:
            payload = response.json()
        except ValueError:
            return response.text

        if spec.unwrap and isinstance(payload, dict):
            return payload.get(spec.unwrap, payload)
        return payload

    # -- reads ------------------------------------------------------------

    async def status(self) -> dict:
        return await self.call("status")

    async def list_rigs(self) -> list[dict]:
        result = await self.call("list_rigs")
        return result if isinstance(result, list) else []

    async def get_rig(self, rig_id: Any) -> dict:
        return await self.call("get_rig", rig_id=rig_id)

    async def catalog(self) -> list[dict]:
        if not self.protocol.supports("catalog"):
            return []
        result = await self.call("catalog")
        return result if isinstance(result, list) else []

    # -- writes -----------------------------------------------------------

    async def write_rig(self, rig: Rig, *, replace_id: Any = None) -> WriteReport:
        """Build a rig on the device, block by block.

        Stops at the first refusal. A device that rejects block seven is
        telling you something about blocks eight through fourteen too, and
        carrying on would bury that under later failures.
        """
        report = WriteReport(
            rig_name=rig.name,
            verified_map=self.protocol.verified,
            map_name=self.protocol.name,
        )

        def record(operation: str, detail: str = "") -> None:
            report.steps.append(Step(operation=operation, ok=True, detail=detail))

        try:
            if replace_id is not None and self.protocol.supports("delete_rig"):
                await self.call("delete_rig", rig_id=replace_id)
                record("delete_rig", f"replaced id {replace_id}")

            created = await self.call("create_rig", name=rig.name)
            rig_id = (
                created.get(self.protocol.field("rig_id"))
                if isinstance(created, dict)
                else None
            )
            if rig_id is None:
                raise Invalid(
                    "the device did not return an id for the new rig",
                    hint=f"Response was: {str(created)[:200]}",
                )
            report.rig_id = rig_id
            record("create_rig", f"'{rig.name}' -> id {rig_id}")

            for block in sorted(rig.blocks, key=lambda b: b.slot):
                await self.call(
                    "add_block",
                    rig_id=rig_id,
                    slot=block.slot,
                    model_id=block.model_id,
                    category=block.category.value,
                    enabled=block.enabled,
                    params=dict(block.params),
                )
                record("add_block", f"slot {block.slot}: {block.model_id}")

            for scene in rig.scenes:
                states = {
                    str(slot): state.value
                    for slot, state in scene.states.items()
                    if state is not SceneBlockState.NO_CHANGE
                }
                await self.call(
                    "set_scene",
                    rig_id=rig_id,
                    name=scene.name,
                    states=states,
                    footswitch=scene.footswitch,
                )
                record("set_scene", f"'{scene.name}'")

        except (Invalid, NotFound, Unavailable) as exc:
            report.steps.append(
                Step(
                    operation="write",
                    ok=False,
                    detail=str(exc).splitlines()[0],
                )
            )

        return report


async def connect(
    base_url: str | None = None,
    *,
    protocol: ProtocolMap | None = None,
    map_name: str | None = None,
) -> DeviceClient:
    """Open a client against a device, choosing a protocol map for it.

    The map is selected from the firmware the device reports, unless one is
    named explicitly. An unknown firmware is an error rather than a fallback:
    sending requests shaped for one firmware to a device running another is
    how a rig gets corrupted.
    """
    from ..protocol import map as protocol_maps
    from . import discovery

    if base_url is None:
        device = await discovery.find_device()
        if device is None:
            raise NotFound(
                "no device found",
                hint=(
                    "Run 'rigsmith discover', or start the simulator with "
                    "'rigsmith protocol simulate' and pass --device."
                ),
            )
        base_url = device.base_url

    if protocol is None and map_name:
        protocol = protocol_maps.get(map_name)

    if protocol is None:
        # Ask the device what it is before deciding how to speak to it.
        firmware = None
        async with httpx.AsyncClient(base_url=base_url, timeout=8.0) as probe:
            for candidate in protocol_maps.available():
                if not candidate.supports("status"):
                    continue
                spec = candidate.operation("status")
                try:
                    response = await probe.request(
                        spec.method,
                        candidate.base.rstrip("/") + spec.path,
                    )
                except httpx.HTTPError:
                    continue
                if response.status_code >= 400:
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    continue
                firmware = payload.get(candidate.field("firmware"))
                if firmware:
                    protocol = candidate
                    break

        if protocol is None:
            protocol = protocol_maps.for_firmware(firmware)

    return DeviceClient(base_url, protocol)

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

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..core.errors import Invalid, NotFound, Unavailable
from ..protocol.map import ProtocolMap
from .session import (
    COOKIE_NAME,
    DEFAULT_PAIR_TIMEOUT,
    POLL_SECONDS,
    PairRequest,
    Session,
    load_session,
    read_pair_state,
    save_session,
)
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
    notes: list[str] = field(default_factory=list)
    """Anything the write itself noticed, as opposed to the standing caveats."""
    refused: list[str] = field(default_factory=list)
    """Values the device kept its own way after being asked twice:
    ``Amp.Type``. The rig was saved, but it is not quite the draft."""

    @property
    def ok(self) -> bool:
        """Every step completed - the rig is on the device."""
        return bool(self.steps) and all(step.ok for step in self.steps)

    @property
    def exact(self) -> bool:
        """The rig on the device is the draft: every step, every value."""
        return self.ok and not self.refused

    @property
    def completed(self) -> int:
        return sum(1 for step in self.steps if step.ok)

    @property
    def caveats(self) -> list[str]:
        """Everything a reader has to be told about this write.

        A property rather than prose inside render(), because a JSON consumer
        needs the same warnings a terminal gets. ``verified_map: false`` is a
        boolean that is very easy to skip past; a sentence is not.
        """
        notes: list[str] = list(self.notes)
        if not self.verified_map:
            notes.append(
                f"The protocol map '{self.map_name}' is UNVERIFIED - it was "
                "invented or inferred rather than captured from a real device. "
                "Check the device before trusting this."
            )
        if not self.ok:
            notes.append(
                "Stopped at the first refusal rather than pressing on, so the "
                "device holds a partial rig. Review it before retrying."
            )
        if self.refused:
            notes.append(
                f"The device kept {len(self.refused)} value(s) of its own "
                f"({', '.join(self.refused[:6])}), so the rig on it is not "
                "exactly the draft. Check those on the device."
            )
        return notes

    def render(self) -> str:
        header = (
            f"Wrote '{self.rig_name}' to the device"
            + (f", but it kept {len(self.refused)} value(s) of its own" if self.refused else "")
            if self.ok
            else f"Write of '{self.rig_name}' stopped after {self.completed} step(s)"
        )
        lines = [header]
        if self.rig_id is not None:
            lines.append(f"  device rig id: {self.rig_id}")
        lines.extend(step.render() for step in self.steps)
        lines.extend(f"\n  {note}" for note in self.caveats)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "rig_name": self.rig_name,
            "rig_id": self.rig_id,
            "caveats": self.caveats,
            "ok": self.ok,
            "exact": self.exact,
            "refused": list(self.refused),
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
        session: Session | None = None,
    ) -> None:
        """
        Args:
            transport: An httpx transport to use instead of the network.
                Lets tests drive a simulator app in-process, so the client is
                covered without binding a port.
            session: A paired session. Without one the Core answers 401 to
                everything, including the calls that would tell you why, so
                the saved session for this host is loaded if none is given.
        """
        self.base_url = base_url.rstrip("/")
        self.protocol = protocol
        self.timeout = timeout
        self.transport = transport
        self.session = session if session is not None else load_session(self.host)
        self._client: httpx.AsyncClient | None = None

    @property
    def host(self) -> str:
        """Host and port, without the scheme or the api path."""
        remainder = self.base_url.split("://", 1)[-1]
        return remainder.split("/", 1)[0]

    async def __aenter__(self) -> "DeviceClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            follow_redirects=True,
            transport=self.transport,
            cookies=self.session.cookies if self.session else None,
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None



    #: How far a float may drift and still count as the value that was sent.
    #: The device stores single precision, so 0.74 comes back 0.7400000095.
    VALUE_TOLERANCE = 1e-3

    async def _reconcile(self, placement: Any) -> list[tuple[str, Any, Any]]:
        """Read a block back and re-apply anything that did not stick.

        Setting several properties at once is not always the same as setting
        them one at a time: some of them reset others. Writing a cab's mic
        alongside its ``Doubling`` left the mic on the wrong model, silently,
        and the rig saved that way.

        Rather than model which property disturbs which - that is firmware
        detail that will change - this asks the device what it ended up with
        and sends the differences again, individually. What still will not
        take is returned, so it can be reported rather than assumed.
        """
        try:
            actual = await self.call("get_block", block=placement.node)
        except (Invalid, NotFound, Unavailable):
            return []
        if not isinstance(actual, dict):
            return []

        drifted = [
            (name, wanted)
            for name, wanted in placement.properties.items()
            if not self._same(wanted, actual.get(name))
        ]
        for name, wanted in drifted:
            try:
                await self.call(
                    "set_block", block=placement.node, properties={name: wanted}
                )
            except (Invalid, NotFound, Unavailable):
                continue

        if not drifted:
            return []

        try:
            final = await self.call("get_block", block=placement.node)
        except (Invalid, NotFound, Unavailable):
            return []
        if not isinstance(final, dict):
            return []
        return [
            (name, wanted, final.get(name))
            for name, wanted in drifted
            if not self._same(wanted, final.get(name))
        ]

    @classmethod
    def _same(cls, wanted: Any, actual: Any) -> bool:
        if actual is None:
            return False
        if isinstance(wanted, bool) or isinstance(actual, bool):
            return bool(wanted) == bool(actual)
        if isinstance(wanted, (int, float)) and isinstance(actual, (int, float)):
            return abs(float(wanted) - float(actual)) <= cls.VALUE_TOLERANCE
        return wanted == actual


    #: How long to give makeNewRig before deciding it is not coming.
    NEW_RIG_TIMEOUT = 15.0

    async def _await_new_rig(self, previous_id: Any, *, poll_s: float = 0.25) -> bool:
        """Block until the device has actually loaded the new rig.

        ``makeNewRig`` returns true immediately and loads the rig some time
        afterwards. Writing into the patch before that lands looks like it
        works - the values read back correctly, because they went into the rig
        that is still open - and is then wiped when the blank one arrives. The
        rig that gets saved is empty, and nothing anywhere has reported a
        failure.

        So the return value is not the signal. The signal is the device saying
        it now has a different rig open, and that its chain is empty.
        """
        deadline = time.monotonic() + self.NEW_RIG_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            try:
                loaded = await self.call("loaded_rig")
                chain = await self.call("get_chain")
            except (Invalid, NotFound, Unavailable):
                continue

            now = (
                loaded.get(self.protocol.field("rig_id"))
                if isinstance(loaded, dict)
                else None
            )
            if previous_id is not None and now == previous_id:
                continue
            if isinstance(chain, dict) and self._chain_is_empty(chain):
                return True
        return False

    @staticmethod
    def _chain_is_empty(chain: dict, slots: int = 14) -> bool:
        return all(not chain.get(f"ModuleType{n}") for n in range(1, slots + 1))

    # -- pairing ----------------------------------------------------------

    async def _auth(self) -> tuple[int, Any, Any]:
        """Ask the device for a session. Returns (status, payload, response)."""
        if self._client is None:
            raise Unavailable(
                "the device client is not open",
                hint="Use it as an async context manager.",
            )
        # Not a mapped operation: pairing is how you earn the right to use
        # the map, so it cannot be described by one.
        url = self.base_url.rsplit("/api/", 1)[0] + "/api/auth"
        try:
            response = await self._client.post(url, json={})
        except httpx.HTTPError as exc:
            raise Unavailable(
                f"could not reach the device to pair ({exc})",
                hint=f"Tried POST {url}",
            ) from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        return response.status_code, payload, response

    async def begin_pairing(self) -> PairRequest:
        """Start pairing. Returns the PIN to accept on the device."""
        status, payload, response = await self._auth()

        if status < 400 and not payload.get("pairPin"):
            cookie = self._client.cookies.get(COOKIE_NAME) if self._client else None
            if cookie:
                return PairRequest(pin="", cookie=cookie, state="paired")

        request = read_pair_state(payload, response)
        if request is None:
            raise Invalid(
                f"the device answered {status} with nothing about pairing",
                hint=f"Response was: {str(payload)[:200]}",
            )
        if not request.cookie and self._client is not None:
            request.cookie = self._client.cookies.get(COOKIE_NAME) or ""
        return request

    async def pairing_state(self) -> str:
        """Where a pairing in progress has got to."""
        _status, payload, _response = await self._auth()
        if isinstance(payload, dict):
            return str(payload.get("pairState") or payload.get("state") or "")
        return ""

    async def pair(
        self,
        *,
        timeout_s: float = DEFAULT_PAIR_TIMEOUT,
        on_pin: Any = None,
        poll_s: float = POLL_SECONDS,
        save: bool = True,
    ) -> Session:
        """Pair with the device, waiting for somebody to accept the PIN.

        ``on_pin`` is called with the PIN as soon as the device offers one -
        there is no way to complete this without a person at the hardware, so
        the PIN has to reach them before the wait starts, not after it.
        """
        request = await self.begin_pairing()

        if request.accepted and request.cookie:
            session = self._session_for(request.cookie)
            if save:
                save_session(session)
            self.session = session
            return session

        if on_pin and request.pin:
            on_pin(request.pin)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(poll_s)
            status, payload, response = await self._auth()
            state = str(payload.get("pairState") or "").lower() if isinstance(payload, dict) else ""

            if status < 400 or state in ("paired", "accepted", "ok"):
                cookie = (
                    (self._client.cookies.get(COOKIE_NAME) if self._client else None)
                    or request.cookie
                )
                if not cookie:
                    raise Invalid("the device paired but issued no session cookie")
                session = self._session_for(cookie, response=response)
                if save:
                    save_session(session)
                self.session = session
                return session

            if state in ("rejected", "denied", "refused"):
                raise Invalid(
                    "the pairing was declined on the device",
                    hint="Run it again and tap accept when the PIN appears.",
                )

        raise Unavailable(
            f"nobody accepted the pairing within {timeout_s:.0f}s",
            hint=(
                "The device shows the PIN on its own screen and somebody has "
                "to tap accept. Check it is awake and on the same network."
            ),
        )

    def _session_for(self, cookie: str, *, response: Any = None) -> Session:
        server = None
        if response is not None:
            server = getattr(response, "headers", {}).get("server")
        return Session(
            host=self.host,
            cookie=cookie,
            base_url=self.base_url,
            server=server,
            firmware=self.protocol.firmware,
            paired_at=time.time(),
        )

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

        if response.status_code == 401:
            # The single most common failure against a real Core, and the
            # one whose bare status code explains least.
            raise Unavailable(
                f"{operation}: the device has not paired this session",
                hint=(
                    "Run 'rigsmith device pair' and accept the PIN on the "
                    "device. Pairing is bound to a session cookie, so a "
                    "client that did not load one is never paired."
                ),
            )
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
        """Build a rig on the device.

        Two device shapes, decided by what the map describes rather than by
        guessing at the firmware. A map with a ``write_rig`` procedure is an
        object tree: make a blank rig, set the live patch, save. A map without
        one is the REST shape the simulator speaks.
        """
        if self.protocol.has_procedure("write_rig"):
            return await self._write_rig_patch(rig, replace_id=replace_id)
        return await self._write_rig_rest(rig, replace_id=replace_id)

    async def _write_rig_patch(self, rig: Rig, *, replace_id: Any = None) -> WriteReport:
        """The object-tree write: blank rig, live patch, save.

        Everything here changes device state before anything is saved, which
        is why the chain goes in one PUT. A half-written chain is a rig with
        blocks nobody asked for, and the device would save that just as
        happily as the right one.
        """
        from ..rig.catalog import get_catalog
        from .writer import RIG_NODE, Placement, chain_properties, plan_rig, rig_properties, scene_properties

        report = WriteReport(
            rig_name=rig.name,
            verified_map=self.protocol.verified,
            map_name=self.protocol.name,
        )

        def record(operation: str, detail: str = "") -> None:
            report.steps.append(Step(operation=operation, ok=True, detail=detail))

        try:
            catalog = get_catalog()
            if not catalog.is_verified:
                report.notes.append(
                    "The catalog is not verified, so model names could not be "
                    "turned into the numbers the device actually wants. Run "
                    "'rigsmith catalog extract --subtree'."
                )
            # Resolve everything before touching the device. A name that does
            # not resolve should cost nothing, not leave a half-built rig
            # loaded over whatever the player had open.
            placements = plan_rig(rig, catalog)
            chain = chain_properties(placements)
            settings = rig_properties(rig)
            for placement in placements:
                report.notes.extend(placement.notes)
            record("plan", f"{len(placements)} block(s) resolved")

            colour = getattr(rig, "led_color_index", None)
            colour = colour if isinstance(colour, int) else 0

            before = await self.call("loaded_rig")
            was = before.get(self.protocol.field("rig_id")) if isinstance(before, dict) else None

            await self.call("new_rig", colour=colour)
            settled = await self._await_new_rig(was)
            record(
                "new_rig",
                "blank rig loaded" if settled else "blank rig requested",
            )
            if not settled:
                raise Unavailable(
                    "the device did not finish loading a new rig",
                    hint=(
                        "makeNewRig is asynchronous, and it may be waiting for "
                        "somebody to answer a prompt on the device about "
                        "discarding unsaved changes."
                    ),
                )

            await self.call("set_chain", properties=chain)
            record(
                "set_chain",
                ", ".join(f"{p.slot}:{p.module}" for p in placements) or "empty",
            )

            for placement in placements:
                if not placement.properties:
                    continue
                await self.call(
                    "set_block",
                    block=placement.node,
                    properties=placement.properties,
                )
                detail = (
                    f"slot {placement.slot} {placement.module}: "
                    f"{len(placement.properties)} value(s)"
                )

                stubborn = await self._reconcile(placement)
                if stubborn:
                    report.refused.extend(f"{placement.module}.{name}" for name, _, _ in stubborn)
                    report.notes.append(
                        f"{placement.module}: "
                        + ", ".join(
                            f"{name} stayed {was!r} instead of {wanted!r}"
                            for name, wanted, was in stubborn
                        )
                        + ". The device overrode these; they are not what the "
                        "recipe asked for."
                    )
                    detail += f" ({len(stubborn)} refused)"
                record("set_block", detail)

            if settings:
                # The rig's own settings are secondary to its blocks: a
                # refusal here is reported, and the rig is still saved.
                try:
                    await self.call("set_block", block=RIG_NODE, properties=settings)
                    stubborn = await self._reconcile(Placement(slot=0, module=RIG_NODE, module_index=0, node=RIG_NODE, properties=settings))
                except (Invalid, NotFound) as exc:
                    stubborn = [("Tempo", settings["Tempo"], str(exc).splitlines()[0])]
                if stubborn:
                    report.refused.append("Rig.Tempo")
                    report.notes.append(
                        f"the rig's tempo did not take ({stubborn[0][2]!r}) - set it to "
                        f"{rig.tempo:g} BPM, Tempo: Fixed, in Hardware Assign on the device"
                    )
                record("set_rig", f"tempo {rig.tempo:g} BPM, fixed" + (" (refused)" if stubborn else ""))

            modes = _scene_modes() if rig.scenes else None
            if rig.scenes and modes:
                await self._write_scenes(rig, scene_properties(rig, modes), modes, report, record)

            await self.call("save_rig_as", name=rig.name, colour=colour)
            report.rig_id = rig.name
            record("save_rig_as", f"saved as '{rig.name}'")
            # The rig being replaced goes only once its replacement is saved:
            # deleting first lost it whenever anything after failed.
            if replace_id is not None and self.protocol.supports("delete_rig"):
                await self.call("delete_rig", rig_id=replace_id)
                record("delete_rig", f"replaced {replace_id}")
            if rig.scenes and not modes:
                report.notes.append(scene_instructions(rig))

            loaded = await self.call("loaded_rig")
            if isinstance(loaded, dict):
                report.rig_id = loaded.get(self.protocol.field("rig_id")) or rig.name
                actual = loaded.get(self.protocol.field("rig_name"))
                if actual and actual != rig.name:
                    report.notes.append(
                        f"the device saved it as '{actual}' rather than "
                        f"'{rig.name}' - names are truncated or de-duplicated"
                    )

        except (Invalid, NotFound, Unavailable) as exc:
            report.steps.append(
                Step(operation="write", ok=False, detail=str(exc).splitlines()[0])
            )

        return report

    async def _write_scenes(self, rig: Rig, values: dict, modes: dict, report: WriteReport, record) -> None:
        """Put the scenes on the FootSwitch node, then read them back.

        Secondary to the blocks, like the tempo: a refusal is reported - with
        what to set by hand - and the rig is still saved.
        """
        from .writer import FOOTSWITCH_NODE

        if not values:
            report.notes.append("no scene has a footswitch, so none were written")
            return
        try:
            await self.call("set_properties", node=FOOTSWITCH_NODE, properties=values)
            back = await self.call("get_properties", node=FOOTSWITCH_NODE)
        except (Invalid, NotFound) as exc:
            report.refused.append("FootSwitch")
            report.notes.append(f"the scenes did not take ({str(exc).splitlines()[0]}). " + scene_instructions(rig))
            record("set_scenes", "refused")
            return
        back = back if isinstance(back, dict) else {}
        wrong = [name for name, wanted in values.items() if not self._same(wanted, back.get(name))]
        if wrong:
            report.refused.extend(f"FootSwitch.{name}" for name in wrong)
            report.notes.append(f"{len(wrong)} scene value(s) did not take - " + scene_instructions(rig))
        report.notes.append(
            f"scenes written with the numbers learned from this Core (on {modes['on']}, off {modes['off']}) - "
            "press each footswitch once to check"
        )
        record("set_scenes", f"{len(rig.scenes)} scene(s)" + (f", {len(wrong)} refused" if wrong else ""))

    async def _write_rig_rest(self, rig: Rig, *, replace_id: Any = None) -> WriteReport:
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
            if replace_id is not None and self.protocol.supports("delete_rig"):
                await self.call("delete_rig", rig_id=replace_id)
                record("delete_rig", f"replaced id {replace_id}")
            if rig.tempo:
                report.notes.append(
                    f"this device shape has no rig tempo - set {rig.tempo:g} BPM on the device"
                )

        except (Invalid, NotFound, Unavailable) as exc:
            report.steps.append(
                Step(
                    operation="write",
                    ok=False,
                    detail=str(exc).splitlines()[0],
                )
            )

        return report


def _scene_modes() -> dict | None:
    """This Core's scene mode numbers, once learned - else None."""
    try:
        from ..core import appsettings

        modes = appsettings.load().scene_modes or {}
    except Exception:
        return None
    return modes if isinstance(modes.get("on"), int) and isinstance(modes.get("off"), int) else None


def scene_instructions(rig: Rig) -> str:
    """What to set by hand for each scene - they are not written over the network.

    The Core keeps scenes on /Evil/Engine/FootSwitch (per switch ModeNew,
    UserFootSwitchText and SceneN_S_Mode per slot), and which Mode number
    means on and which means off has not been confirmed on hardware. Writing
    them on a guess could invert every scene, so until 'device scenes' has
    read one set by hand, the write says what to set instead.
    """
    lines = [
        f"{len(rig.scenes)} scene(s) were not written over the network - this Core's scene "
        "numbers are not learned yet ('rigsmith device scenes --learn'). Set them on the device:"
    ]
    names = {block.slot: block.model_id for block in rig.blocks}
    for scene in sorted(rig.scenes, key=lambda s: (s.footswitch or 99, s.name)):
        on = [f"{names.get(slot, slot)} ({slot})" for slot, state in sorted(scene.states.items()) if state is SceneBlockState.ON]
        off = [f"{names.get(slot, slot)} ({slot})" for slot, state in sorted(scene.states.items()) if state is SceneBlockState.OFF]
        switch = f"FS{scene.footswitch}" if scene.footswitch else "unassigned"
        lines.append(f"  {switch} '{scene.name}': on {', '.join(on) or '-'}; off {', '.join(off) or '-'}")
    return "\n".join(lines)


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
                    "Run 'rigsmith discover' to see everything that answered - "
                    "hosts it could not identify as a HeadRush are listed "
                    "there but never written to, so pass --device explicitly "
                    "if one of them is yours.\n"
                    "With no hardware, start the simulator with "
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

"""The hardware loop as a service: routes, calibration, reamps, recording.

Thin over :mod:`rigsmith.loop`, which holds the signal work. What lives here
is what the surfaces share: where routes and calibrations are stored, which
audio backend is in use, turning a take id into samples, and filing what
comes back so it is usable everywhere a take is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import get_settings, require_writable
from .errors import Invalid, NotFound
from .jobs import Progress


def _dir() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    return settings.loop_dir


def store():
    from ..loop.routes import RouteStore

    return RouteStore(_dir())


def backend_for(route=None, name: str | None = None):
    """The audio backend a route runs on."""
    from ..loop.backend import get_backend

    if name:
        return get_backend(name)
    if route is not None and route.backend == "virtual":
        return get_backend("virtual")
    return get_backend()


# --------------------------------------------------------------------------
# devices and routes
# --------------------------------------------------------------------------


def devices(backend: str | None = None):
    return backend_for(name=backend).devices()


def suggest_routes(backend: str | None = None):
    from ..loop.routes import suggest

    return suggest(devices(backend))


def list_routes():
    return store().list()


def get_route(route_id: str | None = None):
    return store().get(route_id)


def save_route(route, *, make_default: bool = False):
    require_writable("save a loop route")
    return store().put(route, make_default=make_default)


def create_route(
    route_id: str,
    *,
    kind: str = "analog",
    output_device: str,
    input_device: str | None = None,
    output_channels: list[int] | None = None,
    input_channels: list[int] | None = None,
    reference_device: str | None = None,
    reference_channel: int | None = None,
    sample_rate: int = 48000,
    hostapi: str | None = None,
    backend: str = "sounddevice",
    note: str | None = None,
    make_default: bool = False,
):
    from ..loop.routes import CORE_REAMP_OUTPUT, CORE_WET_INPUTS, LoopRoute

    if kind == "usb-reamp":
        output_channels = output_channels or [CORE_REAMP_OUTPUT]
        input_channels = input_channels or list(CORE_WET_INPUTS)
    route = LoopRoute(
        route_id=route_id,
        kind=kind,
        output_device=output_device,
        output_channels=output_channels or [1],
        input_device=input_device or output_device,
        input_channels=input_channels or [1],
        reference_device=reference_device,
        reference_channel=reference_channel,
        sample_rate=sample_rate,
        hostapi=hostapi,
        backend=backend,
        note=note,
    )
    return save_route(route, make_default=make_default)


def adopt_suggestion(route_id: str, *, backend: str | None = None, make_default: bool = True):
    for route in suggest_routes(backend):
        if route.route_id == route_id:
            return save_route(route, make_default=make_default)
    raise NotFound(f"no suggested route '{route_id}'", hint="See:  rigsmith loop suggest")


def delete_route(route_id: str) -> None:
    require_writable("delete a loop route")
    store().delete(route_id)


def set_default(route_id: str) -> None:
    require_writable("change the default loop route")
    store().set_default(route_id)


def demo_bench():
    """Set up the virtual bench as the default route, for trying everything.

    Creates a route on the simulated interface and Core, so the loop panel,
    the tuner and autopilot all have hardware to talk to. Every capture made
    on it says it came from the simulator.
    """
    from ..loop.routes import suggest
    from ..loop.backend import VirtualBackend

    require_writable("set up the virtual bench")
    created = []
    for route in suggest(VirtualBackend().devices()):
        route.route_id = f"virtual-{route.kind}"
        route.backend = "virtual"
        route.note = "simulated - " + (route.note or "")
        created.append(store().put(route, make_default=route.kind == "analog"))
    return created


# --------------------------------------------------------------------------
# calibration
# --------------------------------------------------------------------------


def calibration(route_id: str | None = None):
    from ..loop.calibrate import Calibration

    route = get_route(route_id)
    return Calibration.load(_dir(), route.route_id)


def _samples(reference, sample_rate: int) -> np.ndarray:
    from ..match.render_offline import load_audio, resample
    from .takes import resolve

    audio, rate = load_audio(resolve(reference))
    if rate != sample_rate:
        audio = resample(audio, rate, sample_rate)
    return audio


def _profile_di(profile_id: str, sample_rate: int, seconds: float = 12.0) -> np.ndarray:
    """A DI of the profile's own notes, strung together, for level matching."""
    from . import profiles

    profile = profiles.get(profile_id)
    directory = profiles.sample_dir(profile.profile_id)
    picks = [s for s in profile.samples if s.technique == "pick" and s.dynamic == "mf"] or profile.samples
    if not picks:
        raise Invalid(f"profile '{profile_id}' has no samples to play")
    pieces, total = [], 0.0
    from ..match.render_offline import load_audio, resample

    for sample in picks:
        audio, rate = load_audio(directory / sample.file)
        if rate != sample_rate:
            audio = resample(audio, rate, sample_rate)
        audio = audio[: int(1.2 * sample_rate)]
        pieces.append(audio)
        total += len(audio) / sample_rate
        if total >= seconds:
            break
    return np.concatenate(pieces)


def calibration_note(route) -> str | None:
    """What a capture should say about the gain staging it was made with."""
    from ..loop.calibrate import Calibration

    found = Calibration.load(_dir(), route.route_id)
    if found is None:
        return (
            "this route has never been calibrated, so the DI may reach the Core "
            "hotter or quieter than your guitar does - run: rigsmith loop calibrate"
        )
    if found.level_method == "assumed":
        return "the send level was never matched to your guitar; gain staging is a guess"
    return None


def calibrate(
    route_id: str | None = None,
    *,
    di=None,
    profile_id: str | None = None,
    direct=None,
    direct_is_reference: bool = False,
    apply: bool = True,
    progress: Progress | None = None,
):
    """Measure a route; optionally store its send gain on the route."""
    from ..loop.calibrate import calibrate as _calibrate

    require_writable("calibrate the loop")
    route = get_route(route_id)
    backend = backend_for(route)
    di_audio = None
    if di is not None:
        di_audio = _samples(di, route.sample_rate)
    elif profile_id:
        di_audio = _profile_di(profile_id, route.sample_rate)
    direct_audio = _samples(direct, route.sample_rate) if direct is not None else None

    result = _calibrate(
        route,
        backend=backend,
        di=di_audio,
        direct=direct_audio,
        direct_is_reference=direct_is_reference,
        progress=progress,
    )
    result.save(_dir())
    if apply and result.level_method != "assumed" and result.send_gain_db != route.send_gain_db:
        route.send_gain_db = result.send_gain_db
        store().put(route)
    return result


# --------------------------------------------------------------------------
# reamping
# --------------------------------------------------------------------------


@dataclass
class ReampResult:
    path: Path
    route_id: str
    capture: object
    label: str
    di: str
    take_id: str | None = None
    simulated: bool = False
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"{self.label}: {self.path}" + ("  [SIMULATED]" if self.simulated else ""),
            f"  through route {self.route_id}, from {self.di}",
        ]
        lines.extend("  " + line for line in self.capture.describe().splitlines())
        if self.take_id:
            lines.append(f"  filed as take '{self.take_id}'")
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "route_id": self.route_id,
            "label": self.label,
            "di": self.di,
            "take_id": self.take_id,
            "simulated": self.simulated,
            "capture": self.capture.to_dict(),
            "notes": self.notes,
            "render": self.render(),
        }


def reamp(
    di,
    *,
    route_id: str | None = None,
    label: str = "loop",
    file_as_take: bool = False,
    send_gain_db: float | None = None,
    progress: Progress | None = None,
) -> ReampResult:
    """Send a DI (take id or path) around the loop and keep what comes back."""
    from ..loop.run import reamp as _reamp
    from ..match.render_offline import save_audio
    from . import takes

    require_writable("record a reamp")
    route = get_route(route_id)
    backend = backend_for(route)
    if progress:
        progress.update(0.1, f"reamping {label} through {route.route_id}")
    audio = _samples(di, route.sample_rate)
    capture = _reamp(route, audio, backend=backend, send_gain_db=send_gain_db)

    directory = _dir() / "captures"
    slug = takes.slugify(label)
    path = save_audio(directory / f"{slug}.wav", capture.wet, capture.sample_rate)
    meta = {
        "route": route.to_dict(),
        "di": str(di),
        "label": label,
        "capture": capture.to_dict(),
        "simulated": route.backend == "virtual",
    }
    path.with_suffix(".json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    if capture.dry_received is not None:
        save_audio(directory / f"{slug}.received.wav", capture.dry_received, capture.sample_rate)

    result = ReampResult(
        path=path,
        route_id=route.route_id,
        capture=capture,
        label=label,
        di=str(di),
        simulated=route.backend == "virtual",
    )
    note = calibration_note(route)
    if note:
        result.notes.append(note)
    if file_as_take:
        take_id = f"{slug}-return"
        if take_id in {t.take_id for t in takes.list_takes()}:
            takes.delete(take_id)
        take = takes.add(
            path,
            take_id=take_id,
            kind="wet",
            origin="loop-return",
            note=f"{label}: {di} through {route.route_id}" + (" (simulated)" if result.simulated else ""),
            tags=["loop"] + (["simulated"] if result.simulated else []),
            copy=False,
        )
        result.take_id = take.take_id
    if progress:
        progress.update(1.0, "captured")
    return result


def assess(target, capture) -> object:
    """Sample-aligned comparison of two renditions of the same DI."""
    from ..loop.accuracy import assess as _assess
    from ..match.render_offline import load_audio, resample
    from .takes import resolve

    t_audio, t_rate = load_audio(resolve(target))
    c_audio, c_rate = load_audio(resolve(capture))
    if c_rate != t_rate:
        c_audio = resample(c_audio, c_rate, t_rate)
    return _assess(t_audio, c_audio, t_rate)


def model_agreement(
    di,
    *,
    nam_path=None,
    ir_path=None,
    route_id: str | None = None,
    progress: Progress | None = None,
):
    """How faithfully the offline renderer predicts the hardware, for one rig.

    Reamp a DI through the Core with the capture loaded, render the same DI
    offline through the same capture, and compare the two sample for sample.
    A close agreement means offline sweeps can be trusted for this rig; a
    poor one means the shortlist has to be confirmed on hardware.
    """
    from . import matching

    if progress:
        progress.update(0.1, "rendering offline")
    offline = matching.render_offline(di, nam_path=nam_path, ir_path=ir_path, label="agreement-offline")
    if progress:
        progress.update(0.4, "reamping through the Core")
    hardware = reamp(di, route_id=route_id, label="agreement-hardware")
    if progress:
        progress.update(0.9, "comparing")
    return assess(offline.path, hardware.path), hardware


# --------------------------------------------------------------------------
# recording and checking the wiring
# --------------------------------------------------------------------------


def record_input(
    seconds: float,
    *,
    device: str,
    channel: int = 1,
    sample_rate: int = 48000,
    hostapi: str | None = None,
    backend: str | None = None,
    take_id: str | None = None,
    kind: str = "di",
    guitar: str | None = None,
    pickup: str | None = None,
    tuning: str | None = None,
    note: str | None = None,
    progress: Progress | None = None,
):
    """Record one input - a guitar into an interface's Hi-Z input - as a take."""
    from ..match.render_offline import save_audio
    from . import takes

    require_writable("record a take")
    if seconds <= 0 or seconds > 900:
        raise Invalid("record between a moment and fifteen minutes")
    chosen = backend_for(name=backend)
    if progress:
        progress.update(0.05, f"recording {seconds:.0f}s - play now")
    data = chosen.record(seconds, input_device=device, input_channels=[channel], sample_rate=sample_rate, hostapi=hostapi)
    audio = np.asarray(data, dtype=np.float64)
    audio = audio[:, 0] if audio.ndim > 1 else audio
    stem = takes.slugify(take_id or "recording")
    path = save_audio(get_settings().audio_dir / "recordings" / f"{stem}.wav", audio, sample_rate)
    if progress:
        progress.update(0.9, "filing it")
    identifier = stem
    known = {t.take_id for t in takes.list_takes()}
    counter = 2
    while identifier in known:
        identifier = f"{stem}-{counter}"
        counter += 1
    take = takes.add(
        path, take_id=identifier, kind=kind, origin="di-box",
        guitar=guitar, pickup=pickup, tuning=tuning, note=note,
        tags=(["simulated"] if chosen.name == "virtual" else []),
    )
    return take


def meter(route_id: str | None = None, *, seconds: float = 0.3) -> dict:
    """Peak and RMS on the route's inputs right now, for a level meter."""
    route = get_route(route_id)
    chosen = backend_for(route)
    channels = list(route.input_channels)
    data = chosen.record(seconds, input_device=route.input_device, input_channels=channels, sample_rate=route.sample_rate, hostapi=route.hostapi)
    data = np.asarray(data, dtype=np.float64)
    if data.ndim == 1:
        data = data[:, None]
    levels = []
    for index, channel in enumerate(channels):
        column = data[:, index]
        peak = float(np.max(np.abs(column))) if len(column) else 0.0
        rms = float(np.sqrt(np.mean(column ** 2))) if len(column) else 0.0
        levels.append(
            {
                "channel": channel,
                "peak_db": round(20 * np.log10(max(peak, 1e-9)), 1),
                "rms_db": round(20 * np.log10(max(rms, 1e-9)), 1),
                "clipping": peak >= 0.99,
            }
        )
    return {"route_id": route.route_id, "levels": levels}


def test_tone(route_id: str | None = None, *, frequency: float = 440.0, level_db: float = -18.0, seconds: float = 2.0) -> dict:
    """Play a tone out of the route and report what came back - a wiring check."""
    route = get_route(route_id)
    chosen = backend_for(route)
    t = np.arange(int(seconds * route.sample_rate)) / route.sample_rate
    tone = np.sin(2 * np.pi * frequency * t) * 10 ** (level_db / 20)
    fade = int(0.01 * route.sample_rate)
    tone[:fade] *= np.linspace(0, 1, fade)
    tone[-fade:] *= np.linspace(1, 0, fade)
    columns = np.repeat(tone[:, None], len(route.output_channels), axis=1)
    data = chosen.playrec(
        columns,
        output_device=route.output_device,
        input_device=route.input_device,
        output_channels=route.output_channels,
        input_channels=route.input_channels,
        sample_rate=route.sample_rate,
        hostapi=route.hostapi,
    )
    data = np.asarray(data, dtype=np.float64)
    back = data.mean(axis=1) if data.ndim > 1 else data
    peak = float(np.max(np.abs(back))) if len(back) else 0.0
    peak_db = 20 * np.log10(max(peak, 1e-9))
    connected = peak_db > -60
    return {
        "route_id": route.route_id,
        "sent_db": level_db,
        "returned_peak_db": round(peak_db, 1),
        "connected": connected,
        "message": (
            f"the tone came back at {peak_db:.1f} dBFS - the loop is connected"
            if connected
            else "nothing came back - check the cables, the Core's output level, and the input channel"
        ),
    }


def render_routes(routes, default_id: str | None) -> str:
    if not routes:
        return (
            "No loop routes yet.\n\n"
            "A route says where a DI goes out and where the Core's answer comes back.\n"
            "  rigsmith loop suggest          # routes for what is plugged in\n"
            "  rigsmith loop demo             # a simulated bench, nothing needed"
        )
    lines = []
    for route in routes:
        marker = "*" if route.route_id == default_id else " "
        lines.append(marker + " " + route.describe().replace("\n", "\n  "))
    return "\n".join(lines)

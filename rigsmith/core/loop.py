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
    # The app's own setting, unless the environment pins one for this process.
    import os

    if not os.environ.get("RIGSMITH_AUDIO"):
        from . import appsettings

        if appsettings.load().audio_backend == "virtual":
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
    stop_monitor()
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
    stop_monitor()
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
    sample_rate: int | None = None,
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
    stop_monitor()
    if seconds <= 0 or seconds > 900:
        raise Invalid("record between a moment and fifteen minutes")
    chosen = backend_for(name=backend)
    if sample_rate is None:
        # The device's own rate, so nothing in between resamples the DI.
        native = int(round(chosen.find(device, hostapi=hostapi, need_inputs=channel).sample_rate))
        sample_rate = native if native in (44100, 48000, 88200, 96000) else 48000
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


#: A DI's healthy peak range: loud enough that the noise floor is far below
#: the playing, with headroom for the hardest pick attack.
DI_PEAK_LOW_DB, DI_PEAK_HIGH_DB = -30.0, -3.0
DI_SWEET_LOW_DB, DI_SWEET_HIGH_DB, DI_SWEET_TARGET_DB = -18.0, -6.0, -10.0


def judge_level(audio, sample_rate: int) -> dict:
    """Is this guitar input recordable? Peak, noise floor, and what to change."""
    x = np.asarray(audio, dtype=np.float64)
    x = x[:, 0] if x.ndim > 1 else x
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    peak_db = 20 * np.log10(max(peak, 1e-9))
    rms = float(np.sqrt(np.mean(x**2))) if len(x) else 0.0
    rms_overall_db = 20 * np.log10(max(rms, 1e-9))
    hop = max(1, int(0.02 * sample_rate))
    blocks = x[: (len(x) // hop) * hop].reshape(-1, hop) if len(x) >= hop else np.zeros((1, 1))
    rms_db = 20 * np.log10(np.maximum(np.sqrt((blocks**2).mean(axis=1)), 1e-9))
    floor_db = float(np.percentile(rms_db, 10))
    clipping = peak >= 0.99
    headroom_db = round(max(0.0, -float(peak_db)), 1)
    gain_delta_db = round(float(DI_SWEET_TARGET_DB - peak_db), 1) if peak_db > -55 else None
    in_sweet_spot = bool(DI_SWEET_LOW_DB <= peak_db <= DI_SWEET_HIGH_DB and not clipping)

    if peak_db < -55:
        verdict, advice = "silent", "nothing is coming in - check the cable, the input and the channel"
        coaching = "Play a strong note or chord to measure your level (or check cable / input channel)."
    elif clipping or peak_db > DI_PEAK_HIGH_DB:
        verdict, advice = "too hot", "turn the input gain down until the hardest notes peak around -6 dBFS"
        drop = max(1.0, abs(round(float(peak_db - (-8.0)), 1)))
        coaching = f"CLIPPING / TOO HOT ({peak_db:.1f} dBFS) - turn your audio interface gain knob DOWN by ~{drop:.0f} dB."
    elif peak_db < DI_PEAK_LOW_DB:
        verdict, advice = "too quiet", "turn the input gain up (a Hi-Z / instrument input if the interface has one)"
        boost = max(1.0, round(float(DI_SWEET_TARGET_DB - peak_db), 1))
        coaching = f"Too quiet ({peak_db:.1f} dBFS) - turn your interface gain knob UP by ~{boost:.0f} dB (use Hi-Z / Inst input)."
    elif peak_db - floor_db < 30:
        verdict, advice = "noisy", "the hum and hiss are close to the playing - check the cable, the guitar's volume, and move away from screens"
        coaching = f"Noisy floor ({floor_db:.0f} dBFS vs {peak_db:.1f} dBFS peak) - turn instrument volume full up, check cable shielding, and step away from monitors."
    else:
        verdict, advice = "good", "ready to record"
        if peak_db < DI_SWEET_LOW_DB:
            boost = max(1.0, round(float(DI_SWEET_TARGET_DB - peak_db), 1))
            coaching = f"Usable ({peak_db:.1f} dBFS), or turn interface gain UP by ~{boost:.0f} dB to hit the optimal -18 to -6 dBFS sweet spot."
        elif peak_db > DI_SWEET_HIGH_DB:
            drop = max(1.0, round(float(peak_db - (-8.0)), 1))
            coaching = f"Hot but unclipped ({peak_db:.1f} dBFS) - nudge gain DOWN ~{drop:.0f} dB for extra transient headroom."
        else:
            coaching = f"Sweet spot ({peak_db:.1f} dBFS peak, {headroom_db:.1f} dB headroom) - optimal DI gain staging!"
    return {
        "peak_db": round(float(peak_db), 1),
        "rms_db": round(float(rms_overall_db), 1),
        "noise_floor_db": round(floor_db, 1),
        "headroom_db": headroom_db,
        "clipping": bool(clipping),
        "in_sweet_spot": in_sweet_spot,
        "sweet_spot_db": [DI_SWEET_LOW_DB, DI_SWEET_HIGH_DB],
        "gain_delta_db": gain_delta_db,
        "verdict": verdict,
        "advice": advice,
        "coaching": coaching,
    }


def analyze_tuner(audio, sample_rate: int, *, tuning_name: str | None = None) -> dict:
    """Detect fundamental pitch (down to 25 Hz for 5/6-string & drop bass) and compare to open strings."""
    from ..learn.instrument import midi_to_hz, midi_to_note as midi_to_name, tuning as get_tuning
    from ..match.pitch import _estimate_f0

    x = np.asarray(audio, dtype=np.float64)
    x = x[:, 0] if x.ndim > 1 else x
    tune = get_tuning(tuning_name)
    open_targets = [
        {
            "string": s,
            "midi": tune.open_midi(s),
            "note": midi_to_name(tune.open_midi(s)),
            "hz": round(float(midi_to_hz(tune.open_midi(s))), 2),
        }
        for s in range(1, tune.string_count + 1)
    ]

    # Use the most recent ~250 ms for low-latency pitch tracking down to 25 Hz (A0/B0/E1)
    win_len = min(len(x), max(int(0.25 * sample_rate), 4096))
    tail = x[-win_len:] if win_len > 0 else x
    rms = float(np.sqrt(np.mean(tail**2))) if len(tail) else 0.0
    rms_db = 20 * np.log10(max(rms, 1e-9))

    if len(tail) < int(0.06 * sample_rate) or rms_db < -54.0:
        return {
            "pitched": False,
            "hz": None,
            "midi": None,
            "note": None,
            "cents": None,
            "clarity": 0.0,
            "target_string": None,
            "target_note": None,
            "target_midi": None,
            "target_hz": None,
            "target_cents": None,
            "in_tune": False,
            "direction": "silent",
            "tuning": tune.name,
            "is_bass": tune.is_bass,
            "strings": open_targets,
        }

    hz, clarity = _estimate_f0(tail, sample_rate, min_f0=25.0 if tune.is_bass else 35.0)
    if hz <= 0 or clarity < 0.50:
        return {
            "pitched": False,
            "hz": None,
            "midi": None,
            "note": None,
            "cents": None,
            "clarity": round(float(clarity), 3),
            "target_string": None,
            "target_note": None,
            "target_midi": None,
            "target_hz": None,
            "target_cents": None,
            "in_tune": False,
            "direction": "unpitched",
            "tuning": tune.name,
            "is_bass": tune.is_bass,
            "strings": open_targets,
        }

    midi_float = 69.0 + 12.0 * float(np.log2(hz / 440.0))
    nearest_midi = int(round(midi_float))
    cents = round((midi_float - nearest_midi) * 100.0, 1)
    note = midi_to_name(nearest_midi)

    # Closest open string in the active tuning
    closest = min(open_targets, key=lambda item: abs(midi_float - item["midi"]))
    target_cents = round((midi_float - closest["midi"]) * 100.0, 1)
    in_tune = bool(abs(target_cents) <= 5.0)
    if in_tune:
        direction = "in-tune"
    elif target_cents < 0:
        direction = "flat"
    else:
        direction = "sharp"

    return {
        "pitched": True,
        "hz": round(float(hz), 2),
        "midi": round(float(midi_float), 2),
        "nearest_midi": nearest_midi,
        "note": note,
        "cents": cents,
        "clarity": round(float(clarity), 3),
        "target_string": closest["string"],
        "target_note": closest["note"],
        "target_midi": closest["midi"],
        "target_hz": closest["hz"],
        "target_cents": target_cents,
        "in_tune": in_tune,
        "direction": direction,
        "tuning": tune.name,
        "is_bass": tune.is_bass,
        "strings": open_targets,
    }


def input_level(device: str, *, channel: int = 1, seconds: float = 3.0, hostapi: str | None = None, backend: str | None = None) -> dict:
    """Listen to a guitar input for a few seconds - play hard - and judge it.

    Nothing is saved; this is the check before a recording that matters.
    """
    if not 0.2 <= seconds <= 15:
        raise Invalid("listen for between 0.2 seconds and fifteen seconds")
    # If the live monitor is already running on this device/channel, read its rolling buffer
    mon = _MONITOR.snapshot(device=device, channel=channel, seconds=seconds)
    if mon is not None:
        audio, rate, dev_name = mon
        return {"device": dev_name, "channel": channel, **judge_level(audio, rate)}
    chosen = backend_for(name=backend)
    found = chosen.find(device, hostapi=hostapi, need_inputs=channel)
    native = int(round(found.sample_rate))
    rate = native if native in (44100, 48000, 88200, 96000) else 48000
    data = chosen.record(seconds, input_device=device, input_channels=[channel], sample_rate=rate, hostapi=hostapi)
    return {"device": found.name, "channel": channel, **judge_level(data, rate)}


def tuner_probe(
    device: str,
    *,
    channel: int = 1,
    seconds: float = 0.35,
    tuning: str | None = None,
    hostapi: str | None = None,
    backend: str | None = None,
) -> dict:
    """Single-shot or monitor-backed tuner + gain-staging reading."""
    seconds = float(np.clip(seconds, 0.18, 5.0))
    mon = _MONITOR.snapshot(device=device, channel=channel, seconds=max(seconds, 0.5))
    if mon is not None:
        audio, rate, dev_name = mon
    else:
        chosen = backend_for(name=backend)
        found = chosen.find(device, hostapi=hostapi, need_inputs=channel)
        native = int(round(found.sample_rate))
        rate = native if native in (44100, 48000, 88200, 96000) else 48000
        data = chosen.record(seconds, input_device=device, input_channels=[channel], sample_rate=rate, hostapi=hostapi)
        audio = np.asarray(data, dtype=np.float64)
        audio = audio[:, 0] if audio.ndim > 1 else audio
        dev_name = found.name
    level_info = judge_level(audio, rate)
    tuner_info = analyze_tuner(audio, rate, tuning_name=tuning)
    return {
        "device": dev_name,
        "channel": channel,
        "sample_rate": rate,
        "level": level_info,
        "tuner": tuner_info,
    }


def check_clip(
    device: str,
    *,
    channel: int = 1,
    seconds: float = 3.5,
    tuning: str | None = None,
    hostapi: str | None = None,
    backend: str | None = None,
) -> dict:
    """Record a short check clip, judge its gain staging & pitch, and save a playable WAV."""
    from ..match.render_offline import save_audio

    require_writable("record a level check clip")
    seconds = float(np.clip(seconds, 1.0, 15.0))
    was_monitoring = _MONITOR.state_dict()
    stop_monitor()
    try:
        chosen = backend_for(name=backend)
        found = chosen.find(device, hostapi=hostapi, need_inputs=channel)
        native = int(round(found.sample_rate))
        rate = native if native in (44100, 48000, 88200, 96000) else 48000
        data = chosen.record(seconds, input_device=device, input_channels=[channel], sample_rate=rate, hostapi=hostapi)
        audio = np.asarray(data, dtype=np.float64)
        audio = audio[:, 0] if audio.ndim > 1 else audio
        settings = get_settings()
        settings.ensure_workspace()
        path = save_audio(settings.audio_dir / "checks" / "level-check.wav", audio, rate)
        level_info = judge_level(audio, rate)
        tuner_info = analyze_tuner(audio, rate, tuning_name=tuning)
        return {
            "path": str(path),
            "device": found.name,
            "channel": channel,
            "seconds": round(len(audio) / rate, 2),
            "sample_rate": rate,
            "level": level_info,
            "tuner": tuner_info,
        }
    finally:
        if was_monitoring.get("active"):
            try:
                start_monitor(
                    input_device=was_monitoring["input_device"],
                    input_channel=was_monitoring["input_channel"],
                    output_device=was_monitoring.get("output_device"),
                    output_channels=was_monitoring.get("output_channels") or [1, 2],
                    listen=bool(was_monitoring.get("listen", False)),
                    gain_db=float(was_monitoring.get("gain_db", 0.0)),
                    tuning=tuning or was_monitoring.get("tuning"),
                    backend=backend,
                )
            except Exception:
                pass


# --------------------------------------------------------------------------
# live low-latency tuner, gain meter & audio pass-through monitor
# --------------------------------------------------------------------------

import collections
import threading
import time


class _LiveMonitor:
    """Background low-latency input capture + optional hardware output pass-through."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active: bool = False
        self.input_device: str | None = None
        self.input_channel: int = 1
        self.output_device: str | None = None
        self.output_channels: list[int] = [1, 2]
        self.listen: bool = False
        self.gain_db: float = 0.0
        self.tuning: str | None = None
        self.sample_rate: int = 48000
        self.hostapi: str | None = None
        self.backend_name: str = "sounddevice"
        self.error: str | None = None
        self.peak_hold_db: float = -120.0
        self.started_at: float = 0.0
        self._ring: np.ndarray = np.zeros(48000 * 2, dtype=np.float32)
        self._ring_filled: int = 0
        self._audio_q: collections.deque = collections.deque(maxlen=6)
        self._in_stream = None
        self._out_stream = None
        self._sim_thread: threading.Thread | None = None
        self._stop_evt = threading.Event()

    def start(
        self,
        *,
        input_device: str,
        input_channel: int = 1,
        output_device: str | None = None,
        output_channels: list[int] | None = None,
        listen: bool = False,
        gain_db: float = 0.0,
        tuning: str | None = None,
        backend: str | None = None,
    ) -> dict:
        with self._lock:
            out_ch = output_channels or [1, 2]
            # If already running on the exact same devices, update live parameters in-place
            if (
                self.active
                and self.input_device == input_device
                and self.input_channel == int(input_channel)
                and self.output_device == (output_device if listen else self.output_device)
                and (not listen or self._out_stream is not None or self.backend_name == "virtual")
            ):
                self.listen = bool(listen)
                self.gain_db = float(np.clip(gain_db, -36.0, 18.0))
                if tuning:
                    self.tuning = tuning
                return self._status_unlocked(tuning=self.tuning)

        self.stop()

        chosen = backend_for(name=backend)
        in_info = chosen.find(input_device, need_inputs=input_channel)
        native = int(round(in_info.sample_rate))
        rate = native if native in (44100, 48000, 88200, 96000) else 48000
        out_info = None
        if output_device:
            try:
                out_info = chosen.find(output_device, hostapi=in_info.hostapi, need_outputs=max(out_ch))
            except Exception:
                try:
                    out_info = chosen.find(output_device, need_outputs=1)
                except Exception:
                    out_info = None

        with self._lock:
            self.input_device = in_info.name
            self.input_channel = int(input_channel)
            self.output_device = out_info.name if out_info else output_device
            self.output_channels = [c for c in out_ch if not out_info or c <= max(1, out_info.outputs)] or [1]
            self.listen = bool(listen)
            self.gain_db = float(np.clip(gain_db, -36.0, 18.0))
            self.tuning = tuning
            self.sample_rate = rate
            self.hostapi = in_info.hostapi
            self.backend_name = chosen.name
            self.error = None
            self.peak_hold_db = -120.0
            self.started_at = time.time()
            self._ring = np.zeros(rate * 2, dtype=np.float32)
            self._ring_filled = 0
            self._audio_q.clear()
            self._stop_evt.clear()

            if chosen.name == "virtual":
                self.active = True
                self._sim_thread = threading.Thread(target=self._run_virtual, daemon=True)
                self._sim_thread.start()
                return self._status_unlocked(tuning=self.tuning)

            from ..loop.backend import _ensure_com

            _ensure_com()
            sd = chosen.sd

            blocksize = 512

            def _in_cb(indata, frames, _time_info, _status):
                col = indata[:, self.input_channel - 1] if indata.shape[1] >= self.input_channel else indata[:, 0]
                mono = np.asarray(col, dtype=np.float32).copy()
                n = len(mono)
                if n >= len(self._ring):
                    self._ring[:] = mono[-len(self._ring) :]
                    self._ring_filled = len(self._ring)
                else:
                    self._ring[:-n] = self._ring[n:]
                    self._ring[-n:] = mono
                    self._ring_filled = min(len(self._ring), self._ring_filled + n)
                peak = float(np.max(np.abs(mono))) if n else 0.0
                pdb = 20.0 * float(np.log10(max(peak, 1e-9)))
                if pdb > self.peak_hold_db:
                    self.peak_hold_db = pdb
                else:
                    self.peak_hold_db = max(pdb, self.peak_hold_db - 0.15)
                if self.listen:
                    scale = float(10.0 ** (self.gain_db / 20.0))
                    self._audio_q.append(np.clip(mono * scale, -0.99, 0.99))

            def _out_cb(outdata, frames, _time_info, _status):
                if not self.listen or not self._audio_q:
                    outdata.fill(0)
                    return
                block = self._audio_q.popleft()
                if len(block) < frames:
                    padded = np.zeros(frames, dtype=np.float32)
                    padded[: len(block)] = block
                    block = padded
                elif len(block) > frames:
                    block = block[:frames]
                outdata.fill(0)
                for ch in self.output_channels:
                    if 1 <= ch <= outdata.shape[1]:
                        outdata[:, ch - 1] = block

            started_in = False
            in_candidates = [in_info] + (chosen._fallbacks(input_device, need_inputs=self.input_channel, exclude_index=in_info.index) if hasattr(chosen, "_fallbacks") else [])
            for cand_in in in_candidates:
                try:
                    stream_rate = int(round(cand_in.sample_rate)) if int(round(cand_in.sample_rate)) in (44100, 48000, 88200, 96000) else rate
                    in_s = sd.InputStream(
                        device=cand_in.index,
                        channels=self.input_channel,
                        samplerate=stream_rate,
                        blocksize=blocksize,
                        dtype="float32",
                        latency="low",
                        callback=_in_cb,
                    )
                    in_s.start()
                    self._in_stream = in_s
                    self.sample_rate = stream_rate
                    self.hostapi = cand_in.hostapi
                    started_in = True
                    break
                except Exception as exc:
                    self.error = str(exc)
                    continue

            if not started_in:
                from .errors import Unavailable

                raise Unavailable(
                    f"could not open live monitor on '{input_device}': {self.error or 'PortAudio error'}",
                    hint="Close any app holding exclusive audio access and try again.",
                )

            if out_info is not None:
                out_candidates = [out_info] + (chosen._fallbacks(out_info.name, need_outputs=max(self.output_channels), exclude_index=out_info.index) if hasattr(chosen, "_fallbacks") else [])
                for cand_out in out_candidates:
                    try:
                        out_s = sd.OutputStream(
                            device=cand_out.index,
                            channels=max(self.output_channels),
                            samplerate=self.sample_rate,
                            blocksize=blocksize,
                            dtype="float32",
                            latency="low",
                            callback=_out_cb,
                            extra_settings=chosen._extra(cand_out, self.sample_rate) if hasattr(chosen, "_extra") else None,
                        )
                        out_s.start()
                        self._out_stream = out_s
                        break
                    except Exception:
                        continue

            self.active = True
            self.error = None
            return self._status_unlocked(tuning=self.tuning)

    def _run_virtual(self) -> None:
        from ..learn.instrument import midi_to_hz, tuning as get_tuning

        rng = np.random.default_rng(42)
        block = 1024
        phase = 0.0
        while not self._stop_evt.is_set():
            tune = get_tuning(self.tuning)
            f0 = float(midi_to_hz(tune.open_midi(tune.string_count)))
            t = (np.arange(block) + phase) / self.sample_rate
            phase += block
            sig = (
                0.22 * np.sin(2 * np.pi * f0 * t)
                + 0.11 * np.sin(2 * np.pi * 2 * f0 * t)
                + 0.05 * np.sin(2 * np.pi * 3 * f0 * t)
                + rng.normal(0, 10 ** (-72 / 20), block)
            ).astype(np.float32)
            with self._lock:
                self._ring[:-block] = self._ring[block:]
                self._ring[-block:] = sig
                self._ring_filled = min(len(self._ring), self._ring_filled + block)
                peak = float(np.max(np.abs(sig)))
                self.peak_hold_db = max(self.peak_hold_db, 20.0 * float(np.log10(max(peak, 1e-9))))
            time.sleep(block / self.sample_rate)

    def stop(self) -> dict:
        self._stop_evt.set()
        with self._lock:
            for stream in (self._in_stream, self._out_stream):
                if stream is not None:
                    try:
                        stream.stop()
                    except Exception:
                        pass
                    try:
                        stream.close()
                    except Exception:
                        pass
            self._in_stream = None
            self._out_stream = None
            self._audio_q.clear()
            self.active = False
            return {"active": False}

    def reset_peak_hold(self) -> None:
        with self._lock:
            self.peak_hold_db = -120.0

    def snapshot(self, *, device: str | None = None, channel: int | None = None, seconds: float = 0.5):
        with self._lock:
            if not self.active or self._ring_filled < int(0.1 * self.sample_rate):
                return None
            if device and self.input_device and device.lower() not in self.input_device.lower():
                return None
            if channel and self.input_channel != int(channel):
                return None
            n = min(self._ring_filled, max(int(seconds * self.sample_rate), int(0.2 * self.sample_rate)))
            return self._ring[-n:].copy(), self.sample_rate, self.input_device or device or ""

    def state_dict(self) -> dict:
        with self._lock:
            return {
                "active": self.active,
                "input_device": self.input_device,
                "input_channel": self.input_channel,
                "output_device": self.output_device,
                "output_channels": list(self.output_channels),
                "listen": self.listen,
                "gain_db": self.gain_db,
                "tuning": self.tuning,
                "sample_rate": self.sample_rate,
            }

    def status(self, *, tuning: str | None = None) -> dict:
        with self._lock:
            return self._status_unlocked(tuning=tuning or self.tuning)

    def _status_unlocked(self, *, tuning: str | None = None) -> dict:
        if not self.active:
            return {
                "active": False,
                "listen": False,
                "input_device": self.input_device,
                "input_channel": self.input_channel,
                "output_device": self.output_device,
                "gain_db": self.gain_db,
            }
        n = min(max(self._ring_filled, 1), int(0.45 * self.sample_rate))
        audio = self._ring[-n:].copy() if self._ring_filled > 0 else np.zeros(int(0.2 * self.sample_rate), dtype=np.float32)
        level_info = judge_level(audio, self.sample_rate)
        level_info["peak_hold_db"] = round(float(max(self.peak_hold_db, level_info["peak_db"])), 1)
        tuner_info = analyze_tuner(audio, self.sample_rate, tuning_name=tuning or self.tuning)
        return {
            "active": True,
            "listen": bool(self.listen and (self._out_stream is not None or self.backend_name == "virtual")),
            "listen_requested": self.listen,
            "input_device": self.input_device,
            "input_channel": self.input_channel,
            "output_device": self.output_device,
            "output_channels": list(self.output_channels),
            "gain_db": round(self.gain_db, 1),
            "sample_rate": self.sample_rate,
            "hostapi": self.hostapi,
            "level": level_info,
            "tuner": tuner_info,
        }


_MONITOR = _LiveMonitor()


def start_monitor(
    *,
    input_device: str,
    input_channel: int = 1,
    output_device: str | None = None,
    output_channels: list[int] | None = None,
    listen: bool = False,
    gain_db: float = 0.0,
    tuning: str | None = None,
    backend: str | None = None,
) -> dict:
    return _MONITOR.start(
        input_device=input_device,
        input_channel=input_channel,
        output_device=output_device,
        output_channels=output_channels,
        listen=listen,
        gain_db=gain_db,
        tuning=tuning,
        backend=backend,
    )


def stop_monitor() -> dict:
    return _MONITOR.stop()


def monitor_status(*, tuning: str | None = None, reset_peak: bool = False) -> dict:
    if reset_peak:
        _MONITOR.reset_peak_hold()
    return _MONITOR.status(tuning=tuning)


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
    peak_db = float(20 * np.log10(max(peak, 1e-9)))
    connected = bool(peak_db > -60)
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

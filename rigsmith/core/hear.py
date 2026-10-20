"""A draft heard without the Core: your DI through its amp, cab, EQ and effects.

Autopilot hears amps and cabs on the simulated bench, but a draft is more
than an amp - a boost in front, a G EQ after the cab, a delay synced to the
song, a room. This plays a DI through the whole of it, block by block in its
own order, on the simulated amp and simple stand-ins for the effects, so a
draft can be listened to - and scored against the record, effects and all -
before anything is written. It is a rehearsal: the Core's own amps, cabs
and effects are not these, and the result says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import get_settings
from .errors import Invalid

#: AIR Reverb's modes as reverb times, for the modes rigsmith sets.
ROOM_SECONDS = {0: 0.8, 1: 1.6, 7: 3.5}


@dataclass
class Heard:
    path: str
    di_path: str
    target_path: str | None = None
    composite: float | None = None
    """Against the record's part, when the draft came from a run that has one."""
    played: list[str] = field(default_factory=list)
    """What was heard, block by block."""
    skipped: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path, "di_path": self.di_path, "target_path": self.target_path,
            "composite": self.composite, "played": self.played, "skipped": self.skipped,
        }

    def describe(self) -> str:
        lines = ["through " + (", ".join(self.played) or "nothing")]
        if self.skipped:
            lines.append("not heard (no stand-in): " + ", ".join(self.skipped))
        if self.composite is not None:
            lines.append(f"against the record's part: {self.composite:.3f}")
        lines.append("a rehearsal on the simulated amp - the Core's amps and effects are not these")
        return "\n".join(lines)


def _run_for(draft_id: str):
    """The autopilot run that built this draft, if one did."""
    from . import autopilot

    for run in sorted(autopilot.list_runs(), key=lambda r: r.created_at, reverse=True):
        if run.draft_id == draft_id or (run.song_rig or {}).get("draft_id") == draft_id:
            return run
    return None


def _latest_di() -> str | None:
    from . import takes

    found = takes.list_takes(kind="di")
    return found[0].path if found else None


def _seconds_per_beat(tempo: float | None) -> float:
    return 60.0 / (tempo or 120.0)


def _echo(audio, seconds: float, mix: float, feedback: float, rate: int):
    from scipy.signal import lfilter

    lag = max(1, int(seconds * rate))
    b, a = np.zeros(lag + 1), np.zeros(lag + 1)
    b[0], b[lag], a[0], a[lag] = 1.0, mix - feedback, 1.0, -feedback
    return lfilter(b, a, audio)


def _chorus(audio, rate: int, hz: float = 1.0, depth_ms: float = 3.0, mix: float = 0.5):
    n = np.arange(len(audio))
    delay = (12.0 + depth_ms * np.sin(2 * np.pi * hz * n / rate)) * rate / 1000.0
    position = n - delay
    low = np.clip(np.floor(position).astype(int), 0, len(audio) - 1)
    high = np.clip(low + 1, 0, len(audio) - 1)
    fraction = position - np.floor(position)
    wet = audio[low] * (1 - fraction) + audio[high] * fraction
    return (1 - mix) * audio + mix * wet


def _tremolo(audio, rate: int, hz: float, depth: float):
    t = np.arange(len(audio)) / rate
    return audio * (1 - depth / 2 + depth / 2 * np.sin(2 * np.pi * hz * t))


def _in_scene(rig, scene: str | None) -> set[int] | None:
    """The slots a scene turns on, or None for every enabled block."""
    if not scene:
        return None
    found = rig.scene_named(scene) if hasattr(rig, "scene_named") else None
    if found is None:
        raise Invalid(f"no scene '{scene}' in this rig", hint="Scenes: " + ", ".join(s.name for s in rig.scenes))
    return {slot for slot, state in found.states.items() if getattr(state, "value", state) == "on"}


def play(rig, audio, rate: int, *, scene: str | None = None) -> tuple[np.ndarray, list[str], list[str]]:
    """``audio`` through ``rig``'s enabled blocks in slot order (only those
    ``scene`` turns on, when given): ``(out, played, skipped)``."""
    from ..loop.virtual import SimAmp
    from ..tune.candidates import DELAY_DIVISIONS, DIVISION_BEATS, RATE_DIVISIONS, VIRTUAL_VOICING, _tier_of, virtual_cab
    from ..tune.eq import GEQ_NAMES, GEQ_RANGE, apply_graphic

    on = _in_scene(rig, scene)
    blocks = sorted((b for b in rig.blocks if b.enabled and (on is None or b.slot in on)), key=lambda b: b.slot)
    beat = _seconds_per_beat(rig.tempo)
    played: list[str] = []
    skipped: list[str] = []
    x = np.asarray(audio, dtype=np.float64)
    amp_seen = False
    for index, block in enumerate(blocks):
        model, params, kind = block.model_id, dict(block.params), block.category.value
        if kind == "amp":
            voicing = VIRTUAL_VOICING[_tier_of(model, "crunch")]
            cab = next((b for b in blocks[index + 1:] if b.category.value in ("cab", "ir")), None)
            amp = SimAmp(Type=voicing)
            amp.set(**{k: v for k, v in params.items() if isinstance(v, (int, float)) and not isinstance(v, bool)})
            if cab is not None:
                amp.set(CabType=virtual_cab(cab.model_id))
            x = amp.process(x, rate)
            amp_seen = True
            played.append(f"{model} (as the simulated {('clean', 'crunch', 'lead', 'modern')[voicing]} voicing)" + (f" into {cab.model_id}" if cab else ""))
        elif kind in ("cab", "ir"):
            continue  # heard with its amp
        elif model == "G EQ" or "EQ" in model.split():
            gains = [float(params.get(name, 0.5)) * 2 * GEQ_RANGE - GEQ_RANGE for name in GEQ_NAMES]
            x = apply_graphic(x, rate, gains)
            played.append("G EQ")
        elif "Delay" in model:
            if params.get("Sync") is True and isinstance(params.get("DelayTime_Synced"), int) and 0 <= params["DelayTime_Synced"] < len(DELAY_DIVISIONS):
                seconds = DIVISION_BEATS[DELAY_DIVISIONS[params["DelayTime_Synced"]]] * beat
            else:
                seconds = (20.0 + 780.0 * float(params.get("DelayTime", 0.4))) / 1000.0
            mix = float(params.get("Mix", 0.3))
            x = _echo(x, seconds, mix, float(params.get("Feedback", 0.35)), rate)
            played.append(f"{model} ({seconds * 1000:.0f} ms, {mix:.0%})")
        elif "Reverb" in model:
            from scipy.signal import fftconvolve

            rt60 = ROOM_SECONDS.get(int(params.get("Mode", 1)), 1.6) if "AIR" in model else 1.0
            mix = float(params.get("Mix", 0.25))
            t = np.arange(int(rt60 * 1.2 * rate)) / rate
            ir = np.random.default_rng(0).normal(0, 1, len(t)) * 10 ** (-3 * t / rt60)
            wet = fftconvolve(x, ir / np.sqrt(np.sum(ir**2)))[: len(x)]
            x = (1 - mix) * x + mix * wet / max(float(np.std(wet)), 1e-12) * float(np.std(x))
            played.append(f"{model} ({rt60:.1f} s, {mix:.0%})")
        elif "Chorus" in model:
            x = _chorus(x, rate, mix=float(params.get("Mix", 0.5)))
            played.append(model)
        elif "Tremolo" in model:
            if params.get("Sync") is True and isinstance(params.get("Rate_Synced"), int) and 0 <= params["Rate_Synced"] < len(RATE_DIVISIONS):
                hz = 1.0 / (DIVISION_BEATS[RATE_DIVISIONS[params["Rate_Synced"]]] * beat)
            else:
                hz = 1.0 + 9.0 * float(params.get("Rate", 0.4))
            x = _tremolo(x, rate, hz, float(params.get("Depth", 0.5)))
            played.append(f"{model} ({hz:.1f} Hz)")
        elif not amp_seen and _is_boost(model):
            x = x * BOOST_GAIN  # a boost in front: about 6 dB more into the amp
            played.append(f"{model} (as 6 dB more into the amp)")
        else:
            skipped.append(model)
    if not amp_seen:
        skipped.append("no amp - the DI as it is")
    return x, played, skipped


#: A boost in front of the amp, heard as this much more level into it.
BOOST_GAIN = 2.0


def _is_boost(model: str) -> bool:
    return any(word in model for word in ("OD", "Drive", "Boost", "Screamer", "JRC"))


def score(run, audio, rate: int) -> float | None:
    """``audio`` against the record's part of ``run`` - the way the run compared."""
    from ..match.metrics import ComparisonMode
    from ..match.render_offline import load_audio, resample
    from ..match.score import score_candidate

    if run is None or not run.target_path or not Path(run.target_path).is_file():
        return None
    target, target_rate = load_audio(run.target_path)
    if target_rate != rate:
        target = resample(target, target_rate, rate)
    mode = ComparisonMode.PERFORMANCE if run.mode == "performance" else ComparisonMode.DISTRIBUTION
    mine = audio[: len(target)] if mode is ComparisonMode.PERFORMANCE else audio
    try:
        return round(float(score_candidate(target, mine, rate, mode=mode).composite), 4)
    except Exception:
        return None


def render(draft_id: str, *, di: str | None = None, scene: str | None = None) -> Heard:
    """Play a DI through the draft, offline. ``di`` defaults to the DI of the
    run that built the draft, else your latest DI take."""
    from ..match.render_offline import load_audio, save_audio
    from . import autopilot, rigs

    rig = rigs.get(draft_id).rig
    run = _run_for(draft_id)
    # A song rig's scene is its own part: its run's DI and its record.
    if scene and run is not None and (run.song_rig or {}).get("draft_id") == draft_id:
        owner = next((s["run"] for s in run.song_rig["scenes"] if s["scene"] == scene), None)
        if owner:
            run = autopilot.get(owner)
    di_path = di or (run.di_path if run else None) or _latest_di()
    if not di_path:
        raise Invalid("there is no DI to play through it", hint="Record or render one - a part's 'on my guitar', or a DI take.")
    audio, rate = load_audio(di_path)
    x, played, skipped = play(rig, audio, rate, scene=scene)
    peak = float(np.max(np.abs(x))) or 1.0
    x = x / peak * 0.9 if peak > 0.9 else x
    heard = Heard(path="", di_path=str(di_path), played=played, skipped=skipped)
    heard.path = str(save_audio(get_settings().audio_dir / "heard" / f"{draft_id}{'-' + scene if scene else ''}.wav", x, rate))
    if di is None:
        heard.composite = score(run, x, rate)
        if heard.composite is not None:
            heard.target_path = run.target_path
    return heard

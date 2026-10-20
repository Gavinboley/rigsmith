"""A song to try autopilot on - made here, with its answer known.

Nobody's first run should wait on finding a file. The demo is a short band
recording rendered on the spot: a clean intro, a double-tracked riff and
power-chord chorus through one simulated amp, a lead solo through another,
with drums and a bass under them in the mix. rigsmith made it, so it knows
what every guitar went through - and a finished run on it says how close it
got: the voicing it chose, and how far each knob landed from the one the
record was made with. On the simulated bench that checks the whole chain end
to end; on the Core it is a rehearsal, since the Core's amps are not the
simulated ones and only the gain range can be compared.
"""

from __future__ import annotations

import json

import numpy as np

from ..config import get_settings
from .errors import Conflict, NotFound

TITLE = "Demo Song"
ARTIST = "rigsmith"
SONG_ID = "rigsmith-demo"
ANSWER = "demo.json"
RATE = 48000
BEAT = 0.5
SECTION_S = 12.0

#: What each guitar went through, by the simulated amp's own knob names.
AMPS = {
    "rhythm": {"Type": 3, "Gain": 0.7, "Bass": 0.6, "Mid": 0.4, "Treble": 0.6, "Pres": 0.55},
    "lead": {"Type": 2, "Gain": 0.6, "Mid": 0.7},
    "clean": {"Type": 0, "Gain": 0.1, "Level": 0.3},
}
PLAN = (("intro", "clean"), ("verse", "riff"), ("chorus", "chords"), ("solo", "lead"), ("chorus", "chords"))
#: The knobs a run reports, under the names the simulated amp answers to.
KNOBS = (("Gain", "gain"), ("Bass", "bass"), ("Mid", "mid"), ("Treble", "treble"))
#: Which candidate tier each simulated voicing stands for (as
#: :func:`~rigsmith.tune.candidates.candidates` maps tiers onto them).
TIERS = {0: "clean", 1: "crunch", 2: "high", 3: "modern"}


def render(*, seconds_per_section: float | None = None, seed: int = 7) -> tuple[np.ndarray, np.ndarray, list[dict]]:
    """The demo's mix and its guitar stem (stereo), and when each section is.

    Arranged like a song, so the map finds its sections the way it would on
    a record: the verse is a riff on E over a light kit, the chorus power
    chords on another set of roots with the kit opened up and a crash on
    each bar, the solo a half-time lead - and the bass follows each.
    """
    from scipy.signal import butter, sosfilt

    from ..learn.pluck import render_notes
    from ..loop.virtual import SimAmp

    rng = np.random.default_rng(seed)
    beats = int((seconds_per_section or SECTION_S) / BEAT)
    left, right, lead, clean, bass, sections = [], [], [], [], [], []
    t = 0.0
    for name, kind in PLAN:
        sections.append({"name": name, "start": t, "end": t + beats * BEAT, "kind": kind})
        for b in range(beats):
            when = t + b * BEAT
            chorus_root = (45, 48, 43, 50)[(b // 4) % 4]
            if kind == "clean":
                clean.append((when, 0.9, (52, 55, 59, 64)[b % 4], 0.5))
            elif kind == "riff":
                for k in range(2):
                    note = (40, 40, 43, 40, 47, 45, 43, 42)[(2 * b + k) % 8]
                    left.append((when + k * BEAT / 2, BEAT / 2 * 0.9, note, 0.85))
                    right.append((when + k * BEAT / 2 + rng.normal(0, 0.01) + 0.01, BEAT / 2 * 0.9, note + rng.normal(0, 0.05), 0.85))
                    bass.append((when + k * BEAT / 2, BEAT / 2 * 0.8, 28, 0.7))
            elif kind == "chords":
                for note in (chorus_root, chorus_root + 7, chorus_root + 12):
                    left.append((when, BEAT * 0.95, note, 0.8))
                    right.append((when + rng.normal(0, 0.01) + 0.01, BEAT * 0.95, note + rng.normal(0, 0.05), 0.8))
                bass.append((when, BEAT * 0.9, chorus_root - 12, 0.85))
            else:
                lead.append((when, BEAT * 0.9, (64, 67, 69, 71, 74, 71, 69, 67)[b % 8], 0.9))
                left.append((when, BEAT * 0.95, chorus_root, 0.6))
                right.append((when + rng.normal(0, 0.01) + 0.01, BEAT * 0.95, chorus_root, 0.6))
                if b % 2 == 0:
                    bass.append((when, BEAT * 1.8, chorus_root - 12, 0.8))
        t += beats * BEAT
    total = int((t + 1) * RATE)

    def play(notes, amp, offset):
        audio = render_notes(notes, seed=seed + offset)
        audio = np.pad(audio, (0, max(0, total - len(audio))))[:total]
        return SimAmp(**amp).process(audio, RATE) if amp else audio

    # The rhythm is double-tracked, one take each side; lead and clean centred.
    L = play(left, AMPS["rhythm"], 1)
    R = play(right, AMPS["rhythm"], 2)
    C = play(lead, AMPS["lead"], 3)
    K = play(clean, AMPS["clean"], 4)
    stem = np.stack([L + 0.7 * C + K, R + 0.7 * C + K], axis=1)
    stem /= float(np.abs(stem).max()) * 1.1

    drums = _drums(sections, total, seed)
    low = render_notes(bass, seed=seed + 5)
    low = sosfilt(butter(4, 400, fs=RATE, output="sos"), np.pad(low, (0, max(0, total - len(low))))[:total])
    low *= 0.35 / max(1e-9, float(np.abs(low).max()))
    mix = stem * 0.8 + (drums + low)[:, None]
    mix /= float(np.abs(mix).max()) * 1.05
    return mix, stem, sections


def _drums(sections: list[dict], total: int, seed: int) -> np.ndarray:
    """A kit that changes with the song: none under the intro, closed hats in
    the verse, open hats, a busier kick and a crash a bar in the chorus, a
    half-time ride under the solo."""
    noise = np.random.default_rng(seed + 9).normal(0, 1, total)
    n = np.arange(2400)
    kick = np.sin(2 * np.pi * 55 * n / RATE * np.exp(-n / 4000)) * np.exp(-n / 900) * 0.5
    drums = np.zeros(total)

    def hit(at: float, sound: np.ndarray) -> None:
        start = int(at * RATE)
        if start < total:
            piece = sound[: total - start]
            drums[start : start + len(piece)] += piece

    def burst(at: float, length: int, decay: float, level: float, bright: bool = False) -> np.ndarray:
        start = int(at * RATE)
        piece = noise[start : start + length]
        if bright:
            piece = np.diff(piece, prepend=0)
        return piece * np.exp(-np.arange(len(piece)) / decay) * level

    for section in sections:
        kind = section["kind"]
        if kind == "clean":
            continue
        beats = int(round((section["end"] - section["start"]) / BEAT))
        for b in range(beats):
            at = section["start"] + b * BEAT
            if kind == "riff":
                if b % 2 == 0:
                    hit(at, kick)
                else:
                    hit(at, burst(at, 3600, 1100, 0.18))
                for half in (0, BEAT / 2):
                    hit(at + half, burst(at + half, 1200, 180, 0.04, bright=True))
            elif kind == "chords":
                hit(at, kick)
                if b % 2:
                    hit(at, burst(at, 4800, 1600, 0.3))
                if b % 4 == 0:
                    hit(at, burst(at, 48000, 9000, 0.09, bright=True))
                for half in (0, BEAT / 2):
                    hit(at + half, burst(at + half, 6000, 1500, 0.05, bright=True))
            else:
                if b % 4 == 0:
                    hit(at, kick)
                if b % 4 == 2:
                    hit(at, burst(at, 4800, 1600, 0.28))
                hit(at, burst(at, 9000, 3000, 0.035, bright=True))
    return drums


def make():
    """The demo song - made the first time, then kept like any other song."""
    from ..match.render_offline import save_audio
    from . import songs

    try:
        song = songs.get(SONG_ID)
    except NotFound:
        song = None
    if song is not None:
        if answer(SONG_ID) is None:
            raise Conflict(f"a song called '{SONG_ID}' exists and is not the demo", hint="Rename or delete it to make the demo.")
        return song
    mix, stem, sections = render()
    folder = get_settings().audio_dir / "demo"
    mix_path = save_audio(folder / "demo-mix.wav", mix, RATE)
    stem_path = save_audio(folder / "demo-stem.wav", stem, RATE)
    song = songs.create(TITLE, artist=ARTIST, mix=mix_path, stem=stem_path, song_id=SONG_ID)
    (song.directory / ANSWER).write_text(json.dumps({"amps": AMPS, "sections": sections}, indent=2), encoding="utf-8")
    return song


def start(*, profile_id: str | None = None, bench: str = "virtual", route_id: str | None = None):
    """Make the demo if need be, and start autopilot on it."""
    from . import autopilot

    song = make()
    return autopilot.start(
        autopilot.Request(
            title=TITLE, artist=ARTIST, song_id=song.song_id, profile_id=profile_id,
            bench=bench, route_id=route_id, research=False,
        )
    )


def answer(song_id: str | None) -> dict | None:
    """What the demo's guitars went through, or None for any other song."""
    if song_id != SONG_ID:
        return None
    from . import songs

    try:
        path = songs.get(song_id).directory / ANSWER
    except NotFound:
        return None
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _guitar(part, sections: list[dict]) -> str:
    """Which of the demo's guitars a mapped part mostly is."""
    def overlap(section):
        return min(part.end, section["end"]) - max(part.start, section["start"])

    kind = max(sections, key=overlap)["kind"]
    if kind == "clean":
        return "clean"
    if kind == "lead" and part.layer not in ("left", "right"):
        return "lead"
    return "rhythm"


def verdict(run) -> dict | None:
    """How close a finished run on the demo came to the amp it was made with."""
    from ..loop.virtual import SimAmp, VOICINGS
    from . import songs

    known = answer(run.song_id)
    if known is None or not run.results or not run.part_ids:
        return None
    part = next((p for p in songs.get(run.song_id).parts if p.part_id == run.part_ids[0]), None)
    if part is None:
        return None
    guitar = _guitar(part, known["sections"])
    truth = {**SimAmp().params(), **known["amps"][guitar]}
    best = run.results[0]
    made = f"through {VOICINGS[truth['Type']][0]} at " + ", ".join(f"{word} {truth[name]:.0%}" for name, word in KNOBS)
    found = {"label": best["label"], "voicing": best.get("voicing"), "tier": best.get("tier")}
    if run.request.bench != "virtual":
        tier = TIERS[truth["Type"]]
        same = best.get("tier") == tier
        summary = (
            f"The demo's {guitar} guitar ({part.label}) went {made} - a {tier} sound. "
            f"Autopilot's closest on the Core, {best['label']}, is a {best.get('tier')} amp"
            + (" - the same range." if same else f", not {tier}.")
            + " The Core's amps are not the simulated ones, so only the range compares."
        )
        return {"guitar": guitar, "part": part.label, "truth": truth, "found": found, "voicing_right": None, "summary": summary}
    knobs = {SimAmp.ALIASES.get(k.split(".", 1)[-1], k.split(".", 1)[-1]): v for k, v in (best.get("knobs") or {}).items()}
    found["knobs"] = knobs
    right = best.get("voicing") == truth["Type"]
    voice = VOICINGS.get(best.get("voicing") if best.get("voicing") is not None else 1)[0]
    said = (
        f"The demo's {guitar} guitar ({part.label}) went {made}. "
        f"Autopilot's closest, {best['label']}, plays on {voice} - "
        + ("the voicing it was made with" if right else "a different voicing (neighbouring ones trade gain for character, so this can still sound close)")
    )
    if knobs:
        said += "; tuned to " + ", ".join(
            f"{word} {knobs[name]:.0%} ({(knobs[name] - truth[name]) * 100:+.0f})" for name, word in KNOBS if name in knobs
        ) + "."
    else:
        said += "; its knobs were not tuned."
    return {"guitar": guitar, "part": part.label, "truth": truth, "found": found, "voicing_right": right, "summary": said}

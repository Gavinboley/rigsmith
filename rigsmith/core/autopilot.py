"""From a song to a rig, with as little asked of you as possible.

Every piece of this exists on its own - mapping a song, learning a guitar,
the loop, the tuner, the agents. Autopilot strings them into one run you can
start from a title and a file and walk away from:

1. **song** - find or create it; look up its credits in accuracy mode.
2. **stem** - isolate the guitar if there is no stem and Demucs is installed.
3. **map** - sections, layers, a sketch of every part.
4. **parts** - the ones you chose, or the most prominent guitar part.
5. **research** - a researcher agent fills in the recipe with sources
   (accuracy mode), or the recipe is seeded from the measurements alone,
   every claim marked inferred.
6. **di** - the part played on your guitar, on the record's clock.
7. **candidates** - amps worth trying, from the research and the measured
   gain; each one heard through the bench and scored against the part.
8. **tune** - the best two get their knobs turned, then a match EQ.
9. **rig** - a draft with the winning amp, its cab, and the blocks the part
   implies; scored candidates set up as a blind A/B.

Two benches. ``virtual`` runs the whole thing on the simulated amp - fast,
needs nothing, and says on every result that it was a rehearsal. ``device``
switches the Core's amp model live over the network and reamps through the
hardware loop, so every score is the Core's own sound.

Nothing is written to the device. The run ends with a draft and a listening
test; writing is the usual gated step, and stays yours.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings, require_writable
from . import events
from .errors import Invalid, NotFound
from .jobs import Progress

STAGES = ("song", "stem", "map", "parts", "research", "di", "candidates", "tune", "rig")

#: Stages whose failure ends the run; the rest become notes.
ESSENTIAL = {"song", "map", "parts", "di", "candidates"}


def _dir() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    return settings.autopilot_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Request:
    title: str
    artist: str | None = None
    mix: str | None = None
    stem: str | None = None
    song_id: str | None = None
    parts: list[str] = field(default_factory=list)
    profile_id: str | None = None
    di: str | None = None
    """A DI of the part you already have; skips rendering one."""
    midi: str | None = None
    midi_track: int | None = None
    bench: str = "virtual"
    route_id: str | None = None
    candidates: int = 5
    tune_budget: int = 24
    research: bool = True
    research_timeout_s: float = 600.0


@dataclass
class Stage:
    name: str
    state: str = "waiting"
    """waiting, running, done, skipped, failed."""
    summary: str = ""
    data: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    """Notes belong to the stage that made them, so re-running a stage
    replaces its notes rather than piling a stale one on top."""
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class Run:
    run_id: str
    request: Request
    stages: dict[str, Stage] = field(default_factory=lambda: {name: Stage(name) for name in STAGES})
    state: str = "running"
    song_id: str | None = None
    part_ids: list[str] = field(default_factory=list)
    recipe_id: str | None = None
    di_path: str | None = None
    target_path: str | None = None
    mode: str = "distribution"
    results: list[dict] = field(default_factory=list)
    """Every candidate's score, best first."""
    tuning_run: str | None = None
    draft_id: str | None = None
    ab_session: str | None = None
    notes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["describe"] = self.describe()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Run":
        request = Request(**{k: v for k, v in data["request"].items() if k in Request.__dataclass_fields__})
        stages = {name: Stage(**value) for name, value in (data.get("stages") or {}).items()}
        known = set(cls.__dataclass_fields__) - {"request", "stages"}
        run = cls(run_id=data["run_id"], request=request, **{k: v for k, v in data.items() if k in known and k != "run_id"})
        run.stages = {name: stages.get(name, Stage(name)) for name in STAGES}
        return run

    def note(self, text: str) -> None:
        """Note something about the stage that is running."""
        running = next((s for s in self.stages.values() if s.state == "running"), None)
        target = running.notes if running is not None else self.notes
        if text not in target:
            target.append(text)

    def all_notes(self) -> list[str]:
        found = list(self.notes)
        for name in STAGES:
            for text in self.stages[name].notes:
                if text not in found:
                    found.append(text)
        return found

    def save(self) -> None:
        from . import storage

        storage.write_json(_dir() / f"{self.run_id}.json", self.to_dict())

    def describe(self) -> str:
        lines = [f"autopilot {self.run_id}: {self.request.title}" + (f" - {self.request.artist}" if self.request.artist else "") + f" [{self.state}, {self.request.bench} bench]"]
        for name in STAGES:
            stage = self.stages[name]
            mark = {"done": "+", "skipped": "-", "failed": "x", "running": ">", "waiting": " "}.get(stage.state, "?")
            lines.append(f"  {mark} {name:<11} {stage.summary}")
        if self.results:
            lines.append("  ranking:")
            for result in self.results[:5]:
                lines.append(f"    {result['composite']:.4f}  {result['label']}")
        if self.draft_id:
            lines.append(f"  draft: {self.draft_id}")
        if self.ab_session:
            lines.append(f"  listen: blind A/B session '{self.ab_session}'")
        lines.extend(f"  note: {n}" for n in self.all_notes())
        return "\n".join(lines)


def get(run_id: str) -> Run:
    path = _dir() / f"{run_id}.json"
    if not path.is_file():
        raise NotFound(f"no autopilot run '{run_id}'")
    from . import storage

    return Run.from_dict(storage.read_json(path))


def list_runs() -> list[Run]:
    runs = []
    for path in _dir().glob("*.json"):
        try:
            runs.append(Run.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return runs


# --------------------------------------------------------------------------
# the stages
# --------------------------------------------------------------------------


def _stage_song(run: Run) -> str:
    from . import appsettings, songs

    request = run.request
    if request.song_id:
        song = songs.get(request.song_id)
    else:
        identifier = songs.slugify(f"{request.title} {request.artist}" if request.artist else request.title)
        try:
            song = songs.get(identifier)
            if request.mix or request.stem:
                song = songs.attach(identifier, mix=request.mix, stem=request.stem)
        except NotFound:
            song = songs.create(request.title, artist=request.artist, mix=request.mix, stem=request.stem)
    if not appsettings.load().local and not song.credits:
        try:
            song = songs.enrich(song.song_id)
        except Exception as exc:
            run.note(f"the online lookup failed ({exc}); carrying on without credits")
    if not song.mix and not song.stem:
        if not appsettings.load().local and appsettings.load().allow_preview_downloads:
            try:
                song = songs.preview(song.song_id)
                run.note("no file given, so the 30-second store preview is standing in for the song")
            except Exception as exc:
                raise Invalid(f"this song has no audio and no preview could be fetched ({exc})", hint="Give the run your copy of the song.")
        else:
            raise Invalid("this song has no audio", hint="Give the run your copy of the song (mix) or a guitar stem.")
    run.song_id = song.song_id
    return f"{song.title}" + (f" - {song.artist}" if song.artist else "")


def _stage_stem(run: Run, progress) -> str:
    from ..match.separate import demucs_available
    from . import songs

    song = songs.get(run.song_id)
    if song.stem:
        return "using the stem given"
    if not demucs_available():
        run.note("Demucs is not installed, so parts are measured from the full mix - expect drums and bass in the numbers")
        raise _Skip("no Demucs - mapping the full mix")
    songs.isolate(run.song_id, progress=progress)
    return "guitar isolated"


def _stage_map(run: Run, progress) -> str:
    from . import songs

    song = songs.analyse(run.song_id, progress=progress)
    return f"{len(song.structure.get('sections', []))} sections, {len(song.parts)} parts; {(song.layers or {}).get('summary', '')}"


def _stage_parts(run: Run) -> str:
    from . import songs

    song = songs.get(run.song_id)
    wanted = run.request.parts or [p.part_id for p in song.chosen()]
    if not wanted:
        wanted = [_most_prominent(song).part_id]
        run.note(f"no part chosen, so the most prominent guitar part was picked: {wanted[0]}")
    songs.choose(run.song_id, wanted)
    run.part_ids = wanted
    return ", ".join(wanted)


def _most_prominent(song):
    """The part most worth chasing when nobody said: a loud, repeated, mid-song part."""
    names = {s["name"]: 0 for s in (song.structure or {}).get("sections", [])}

    def score(part) -> float:
        value = part.presence
        if part.section_name == "chorus":
            value += 0.5
        elif part.section_name in ("verse", "solo"):
            value += 0.3
        if part.layer == "stem":
            value -= 0.2
        return value

    playable = [p for p in song.parts if p.end - p.start >= 4] or song.parts
    if not playable:
        raise Invalid("the map has no parts to choose from")
    del names
    return max(playable, key=score)


def _stage_research(run: Run, progress) -> str:
    from . import appsettings, chat, recipes, songs

    song = songs.get(run.song_id)
    part = song.part(run.part_ids[0])
    sketch = part.sketch or {}
    recipe_id = song.recipe_id
    if not recipe_id:
        recipe_id, _ = recipes.create(song.title, artist=song.artist, album=song.album, year=song.year, summary=sketch.get("summary"))
        songs.update(run.song_id, recipe_id=recipe_id)
    run.recipe_id = recipe_id

    # What the record itself says, before anyone's interview does.
    from ..tune.candidates import candidates as pick, tier_for

    tier = tier_for(float(sketch.get("gain") or 0))
    seeds = pick(float(sketch.get("gain") or 0), count=1)
    measured = "measured by rigsmith from the record"
    try:
        if seeds:
            recipes.add_gear(
                recipe_id, "amp", seeds[0].model, category="amp", confidence="inferred",
                source_title=measured, source_kind="measurement",
                note=f"the {part.label} measures as {tier} ({sketch.get('summary', '')})",
            )
    except Exception as exc:
        run.note(f"could not seed the recipe: {exc}")

    if not run.request.research:
        raise _Skip("research turned off; the recipe holds the measurements")
    settings = appsettings.load()
    try:
        message = chat.dispatch("researcher", songs.brief(run.song_id, part.part_id) + f"\nWrite your findings into recipe '{recipe_id}'.")
    except Exception as exc:
        run.note(f"no researcher could be started ({exc}); the recipe holds only the measurements")
        raise _Skip("no model available for research")
    research_id = message.split("run ")[1].split(".")[0]
    import time

    waited = 0.0
    while waited < run.request.research_timeout_s:
        state = chat.agent_run(research_id)["state"]
        if state != "running":
            break
        if progress:
            progress.check()
            progress.update(None, f"researcher at work ({int(waited)}s)")
        time.sleep(2.0)
        waited += 2.0
    result = chat.agent_result(research_id)
    if settings.local:
        run.note("researched by a local model with no web access - its claims are inferences, and the recipe marks them so")
    return f"researcher run {research_id}: {result[:160]}"


def _researched_amps(run: Run) -> list[str]:
    from . import recipes

    if not run.recipe_id:
        return []
    try:
        recipe = recipes.get(run.recipe_id)
    except Exception:
        return []
    names = []
    for gear in getattr(recipe, "gear", []) or []:
        category = getattr(gear, "category", "")
        sources = getattr(gear, "sources", []) or []
        # The measured seed is the record's say-so, not research: counting it
        # here would label it "named by the research" in the ranking.
        if sources and all(getattr(source, "kind", "") == "measurement" for source in sources):
            continue
        if str(getattr(category, "value", category)) in ("amp",):
            for name in (getattr(gear, "resolved_model_id", None), getattr(gear, "model", None), getattr(gear, "name", None)):
                if name:
                    names.append(name)
    return names


def _stage_di(run: Run, progress) -> str:
    from . import appsettings, songs

    song = songs.get(run.song_id)
    part = song.part(run.part_ids[0])
    run.target_path = str(songs.part_audio(run.song_id, part.part_id))
    if run.request.di:
        from .takes import resolve

        run.di_path = str(resolve(run.request.di))
        run.mode = "distribution"
        return "using the DI given (scored as a distribution - it is not the record's timing)"
    profile = run.request.profile_id or appsettings.load().default_profile
    if not profile:
        run.note("no guitar profile, so the part is played on the synthetic string - learn your guitar for a truer test")
    result = songs.render_part_di(
        run.song_id, part.part_id, profile, midi=run.request.midi, track=run.request.midi_track, progress=progress,
    )
    run.di_path = str(result.path)
    run.mode = "performance"
    confidence = f", notes {result.confidence:.0%} confident" if result.confidence is not None else ""
    return f"{part.label} on {profile or 'the synthetic string'} ({result.source}{confidence})"


def _stage_candidates(run: Run, progress) -> str:
    from ..match.metrics import ComparisonMode
    from ..match.render_offline import load_audio, resample, save_audio
    from ..match.score import score_candidate
    from ..rig.catalog import get_catalog
    from ..tune.candidates import candidates as pick
    from . import songs

    song = songs.get(run.song_id)
    part = song.part(run.part_ids[0])
    gain = float((part.sketch or {}).get("gain") or 0)
    field_ = pick(gain, researched=_researched_amps(run), count=run.request.candidates, catalog=get_catalog())
    if not field_:
        raise Invalid("no candidate amps to try")
    if run.request.bench == "virtual":
        # The simulated amp has four voicings, so two "Lead 800" variants
        # sound identical on it and would tie - a ranking that means
        # nothing. One per voicing, and said plainly.
        seen, kept = set(), []
        for candidate in field_:
            if candidate.voicing not in seen:
                seen.add(candidate.voicing)
                kept.append(candidate)
        if len(kept) < len(field_):
            run.note(
                f"the simulated amp has only four voicings, so {len(kept)} of {len(field_)} candidates "
                "were heard (one per voicing); the device bench hears every model"
            )
        field_ = kept

    target, rate = load_audio(run.target_path)
    di, di_rate = load_audio(run.di_path)
    if di_rate != rate:
        di = resample(di, di_rate, rate)
    comparison = ComparisonMode.PERFORMANCE if run.mode == "performance" else ComparisonMode.DISTRIBUTION
    if comparison is ComparisonMode.PERFORMANCE:
        di = di[: len(target) + rate // 2]

    render = _bench(run, rate)
    directory = _dir() / run.run_id
    results = []
    for index, candidate in enumerate(field_):
        if progress:
            progress.check()
            progress.update(index / len(field_), f"hearing {candidate.model}")
        audio = render(candidate, di, None)
        if comparison is ComparisonMode.PERFORMANCE:
            audio = audio[: len(target)]
        scored = score_candidate(target, audio, rate, mode=comparison)
        path = save_audio(directory / f"{songs.slugify(candidate.model)}.wav", audio, rate)
        results.append({**candidate.to_dict(), "label": candidate.model, "composite": scored.composite, "path": str(path), "mode": comparison.value})
        try:
            songs.record_result(run.song_id, part.part_id, label=candidate.model, composite=scored.composite, mode=comparison.value, path=str(path))
        except Exception:
            pass
    results.sort(key=lambda r: r["composite"])
    run.results = results
    return f"{len(results)} amp(s) heard; closest {results[0]['label']} ({results[0]['composite']:.3f})"


class _Skip(Exception):
    pass


def _bench(run: Run, rate: int):
    """A function that plays a DI through a candidate amp, on the chosen bench."""
    if run.request.bench == "virtual":
        from ..loop.virtual import SimAmp

        def render(candidate, di, knobs):
            amp = SimAmp(Type=candidate.voicing if candidate.voicing is not None else 1)
            if knobs:
                amp.set(**{k.split(".", 1)[1]: v for k, v in knobs.items() if k.startswith("Amp")})
            return amp.process(di, rate)

        return render

    if run.request.bench == "device":
        from ..loop.run import reamp as loop_reamp
        from ..match.render_offline import resample
        from . import loop as hardware_loop
        from .tuning import DeviceSink, VirtualSink

        route = hardware_loop.get_route(run.request.route_id)
        backend = hardware_loop.backend_for(route)
        sink = VirtualSink() if route.backend == "virtual" else DeviceSink()

        def render(candidate, di, knobs):
            values = {"Amp": {}}
            if route.backend == "virtual":
                values["Amp"]["Type"] = candidate.voicing if candidate.voicing is not None else 1
            elif candidate.device_index is not None:
                values["Amp"]["Type"] = candidate.device_index
            for key, value in (knobs or {}).items():
                node, param = key.split(".", 1)
                values.setdefault(node, {})[param] = value
            sink.apply(values)
            capture = loop_reamp(route, di, backend=backend)
            audio = capture.wet
            if route.sample_rate != rate:
                audio = resample(audio, route.sample_rate, rate)
            return audio

        return render
    raise Invalid(f"unknown bench '{run.request.bench}'", hint="Use 'virtual' or 'device'.")


def _stage_tune(run: Run, progress) -> str:
    from ..match.metrics import ComparisonMode
    from ..match.render_offline import load_audio, resample, save_audio
    from ..match.score import score_candidate
    from ..tune import eq as match_eq
    from ..tune.candidates import Candidate
    from ..tune.optimize import optimise
    from ..tune.space import virtual_amp

    if not run.results:
        raise _Skip("nothing to tune")
    target, rate = load_audio(run.target_path)
    di, di_rate = load_audio(run.di_path)
    if di_rate != rate:
        di = resample(di, di_rate, rate)
    comparison = ComparisonMode.PERFORMANCE if run.mode == "performance" else ComparisonMode.DISTRIBUTION
    if comparison is ComparisonMode.PERFORMANCE:
        di = di[: len(target) + rate // 2]
    render = _bench(run, rate)
    space = virtual_amp(("GainA", "Bass", "Mid", "Treble", "Presence", "Master"))
    budget = max(6, run.request.tune_budget // 2)
    tuned = []
    for entry in run.results[:2]:
        candidate = Candidate(entry["model"], entry["tier"], entry["cab"], entry["reason"], entry.get("device_index"), entry.get("voicing"))

        def evaluate(values, candidate=candidate):
            audio = render(candidate, di, values)
            if comparison is ComparisonMode.PERFORMANCE:
                audio = audio[: len(target)]
            return score_candidate(target, audio, rate, mode=comparison).composite, {}

        result = optimise(space, evaluate, budget=budget, progress=progress)
        best = result.best
        audio = render(candidate, di, best.values)
        if comparison is ComparisonMode.PERFORMANCE:
            audio = audio[: len(target)]
        path = save_audio(_dir() / run.run_id / f"{entry['label'].replace(' ', '-').lower()}-tuned.wav", audio, rate)
        plan = match_eq.match(target, audio, rate)
        tuned.append({**entry, "label": f"{entry['label']} (tuned)", "composite": best.loss, "knobs": best.values, "path": str(path), "eq": plan.to_dict(), "untuned": entry["composite"]})
    run.results = sorted(run.results + tuned, key=lambda r: r["composite"])
    best = run.results[0]
    gain = ""
    if best.get("untuned") is not None:
        gain = f", {1 - best['composite'] / best['untuned']:.0%} closer than untuned"
    return f"best {best['label']} at {best['composite']:.3f}{gain}"


def _stage_rig(run: Run) -> str:
    from ..tune.candidates import surrounding_blocks
    from . import matching, rigs, songs

    best = run.results[0] if run.results else None
    if best is None:
        raise _Skip("no result to build from")
    song = songs.get(run.song_id)
    part = song.part(run.part_ids[0])
    name = f"{song.title} - {part.section_name}"[:40]
    if run.draft_id:
        # A re-run replaces this run's draft rather than adding another.
        try:
            rigs.delete(run.draft_id)
        except Exception:
            pass
    draft = rigs.create(name, notes=f"autopilot {run.run_id}: {part.label}, {part.sketch.get('summary', '')}").draft
    run.draft_id = draft.draft_id

    order = {"gate": 0, "boost": 1, "tremolo": 5, "delay": 6, "reverb": 7}
    extras = surrounding_blocks(part.sketch or {})
    before = [b for b in extras if order.get(b["role"], 9) < 2]
    after = [b for b in extras if order.get(b["role"], 9) >= 2]
    notes = []

    def add(category, model, params=None):
        try:
            rigs.add_block(run.draft_id, category, model, params=params or {})
        except Exception as exc:
            notes.append(f"could not add {model}: {exc}")

    for block in before:
        add(block["category"], block["model"])
    knobs = {k.split(".", 1)[1]: round(float(v), 4) for k, v in (best.get("knobs") or {}).items()}
    add("amp", best["model"], knobs)
    add("cab", best["cab"])
    for block in after:
        add(block["category"], block["model"])
    for text in notes:
        run.note(text)
    if run.request.bench == "virtual":
        run.note(
            "the amp knob values came from the simulated amp - a rehearsal of the search. "
            "Run again on the device bench to tune them on the Core itself."
        )

    clips = {}
    for entry in run.results[:3]:
        clips[entry["label"]] = entry["path"]
    if len(clips) >= 2:
        session_id = f"{run.run_id}-ab"
        try:
            matching.create_ab(session_id, f"{song.title}: {part.label}", clips)
            run.ab_session = session_id
        except Exception as exc:
            run.note(f"the blind A/B could not be set up: {exc}")
    return f"draft '{run.draft_id}' with {best['model']} and {best['cab']}"


# --------------------------------------------------------------------------
# running it
# --------------------------------------------------------------------------


def start(request: Request) -> Run:
    """Begin a run in the background; returns at once."""
    from .jobs import get_manager

    require_writable("start autopilot")
    if request.bench not in ("virtual", "device"):
        raise Invalid(f"unknown bench '{request.bench}'", hint="Use 'virtual' or 'device'.")
    if not (request.title or request.song_id):
        raise Invalid("autopilot needs a song: a title (and your file of it) or a song id")
    run = Run(run_id=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4], request=request)
    run.save()
    get_manager().submit(f"autopilot: {request.title or request.song_id}", lambda progress: execute(run.run_id, progress=progress))
    return run


def resume(run_id: str, *, from_stage: str | None = None, background: bool = True) -> Run:
    """Pick a stopped or failed run back up, from its first unfinished stage."""
    from .jobs import get_manager

    run = get(run_id)
    if from_stage:
        if from_stage not in STAGES:
            raise Invalid(f"no stage '{from_stage}'", hint="Stages: " + ", ".join(STAGES))
        for name in STAGES[STAGES.index(from_stage):]:
            run.stages[name] = Stage(name)
    run.state = "running"
    run.save()
    if not background:
        return execute(run.run_id)
    get_manager().submit(f"autopilot (resumed): {run.request.title}", lambda progress: execute(run.run_id, progress=progress))
    return run


def execute(run_id: str, *, progress: Progress | None = None) -> Run:
    """Run every stage that has not finished yet. Synchronous."""
    run = get(run_id)
    handlers = {
        "song": lambda: _stage_song(run),
        "stem": lambda: _stage_stem(run, progress),
        "map": lambda: _stage_map(run, progress),
        "parts": lambda: _stage_parts(run),
        "research": lambda: _stage_research(run, progress),
        "di": lambda: _stage_di(run, progress),
        "candidates": lambda: _stage_candidates(run, progress),
        "tune": lambda: _stage_tune(run, progress),
        "rig": lambda: _stage_rig(run),
    }
    for position, name in enumerate(STAGES):
        stage = run.stages[name]
        if stage.state in ("done", "skipped"):
            continue
        stage.state, stage.started_at = "running", _now()
        run.save()
        events.publish({"type": "autopilot", "run_id": run.run_id, "stage": name, "state": "running"})
        if progress:
            progress.update(position / len(STAGES), f"{name}...")
        try:
            stage.summary = handlers[name]() or ""
            stage.state = "done"
        except _Skip as skip:
            stage.state, stage.summary = "skipped", str(skip)
        except Exception as exc:
            if type(exc).__name__ == "Cancelled":
                stage.state, stage.summary = "failed", "stopped"
                run.state = "stopped"
                run.save()
                raise
            stage.state = "failed"
            stage.summary = f"{type(exc).__name__}: {exc}"
            if name in ESSENTIAL:
                run.state = "failed"
                stage.finished_at = _now()
                run.save()
                events.publish({"type": "autopilot", "run_id": run.run_id, "stage": name, "state": "failed", "summary": stage.summary})
                return run
            run.note(f"{name} failed and was skipped: {exc}")
        stage.finished_at = _now()
        run.save()
        events.publish({"type": "autopilot", "run_id": run.run_id, "stage": name, "state": stage.state, "summary": stage.summary})
    run.state = "done"
    run.finished_at = _now()
    run.save()
    events.publish({"type": "autopilot", "run_id": run.run_id, "state": "done"})
    if progress:
        progress.update(1.0, "done")
    return run

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
6. **di** - the part played on your guitar, on the record's clock, moved
   toward the record's guitar when the research names one.
7. **candidates** - amps worth trying, from the research and the measured
   gain; each one heard through its tier's cab and scored against the part.
8. **cabs** - the best amp through the other cabs worth hearing (and any
   the research names), so the tuner starts from the right pair.
9. **tune** - the best two get their knobs turned, then a match EQ.
10. **rig** - a draft with the winning amp, its cab, and the blocks the part
    implies; scored candidates set up as a blind A/B.

Two benches. ``virtual`` runs the whole thing on the simulated amp - fast,
needs nothing, and says on every result that it was a rehearsal. ``device``
switches the Core's amp and cab models live over the network and reamps
through the hardware loop, so every score is the Core's own sound.

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
from .errors import Invalid, NotFound, Unavailable
from .jobs import Progress

STAGES = ("song", "stem", "map", "parts", "research", "di", "candidates", "cabs", "tune", "rig")

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
    like: str | None = None
    """Move the part DI toward this guitar's pickup (a learned guitar or a
    type like ``strat``). Unset: whatever the research says the record's
    guitar was, when it says."""
    like_amount: float = 1.0
    match_guitar: bool = True
    """Follow the research's guitar when ``like`` is unset."""
    use_part_take: bool = True
    """Use your own take of the part, when one was lined up with it before
    ('song take'), instead of rendering a DI from your guitar's voice."""


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
    remaining: list[dict] = field(default_factory=list)
    """What still differs from the record, band by band, for the result the
    draft was built from: ``{band, low, high, delta_db, audible, meaning, try}``."""
    ear_pick: str | None = None
    """The candidate the blind A/B chose, when it was not the score's."""
    part_runs: list[str] = field(default_factory=list)
    """The runs that chased this song's other chosen parts, one each."""
    song_rig: dict | None = None
    """When several parts were chased: the one rig with a scene per part."""
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
        finished = data.get("state") in ("done", "failed", "stopped")
        run.stages = {
            name: stages.get(name) or (Stage(name, state="skipped", summary="added after this run") if finished else Stage(name))
            for name in STAGES
        }
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
            lines.append(f"  draft: {self.draft_id}" + (f" (your ears' pick: {self.ear_pick})" if self.ear_pick else ""))
        if self.ab_session:
            lines.append(f"  listen: blind A/B session '{self.ab_session}'")
        if self.song_rig:
            lines.append(f"  song rig: {self.song_rig['draft_id']}")
            lines.extend(f"    FS{s['footswitch']}  {s['scene']:<20} {s['sound']}  (run {s['run']})" for s in self.song_rig["scenes"])
        audible = [r for r in self.remaining if r.get("audible")]
        if audible:
            lines.append("  still different from the record:")
            for row in sorted(audible, key=lambda r: -abs(r["delta_db"])):
                lines.append(f"    {row['band']:<9} {row['delta_db']:+5.1f} dB  try {row['try']}")
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
        # Your own copy first: a file in the music library whose name is the
        # song (and whose path names the artist, when one was given).
        found = songs.find_local(song.title, song.artist)
        found = [f for f in found if f["artist_match"] or not song.artist]
        if found:
            song = songs.attach(song.song_id, mix=found[0]["path"])
            run.note(f"no file given, so your library's {found[0]['name']} is the song")
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
    data = chat.agent_run(research_id)
    text = ((data.get("result") or {}).get("text") or "").strip()
    if data["state"] == "running":
        run.note(f"the researcher was still at work after {int(waited)}s - the run carried on from the measurements; its findings reach the recipe when it finishes")
        raise _Skip(f"researcher run {research_id} still working - carried on without it")
    if data["state"] == "failed":
        from ..agent.providers import LOGIN_HINT, claude_code_signed_in, needs_login

        first = text.splitlines()[0] if text else "no reason given"
        # The text can only suggest a sign-in problem; `claude auth status`
        # can rule one out, and the error itself is always shown.
        if needs_login(text) and claude_code_signed_in() is not True:
            run.note(f"the researcher could not sign in ({first[:200]}). " + LOGIN_HINT + " Then resume this run from research.")
            raise _Skip("the researcher could not sign in to Claude - the recipe holds only the measurements")
        run.note(f"the researcher failed ({first[:200]}); the recipe holds only the measurements")
        raise _Skip(f"researcher run {research_id} failed")
    if settings.local:
        run.note("researched by a local model with no web access - its claims are inferences, and the recipe marks them so")
    return f"researcher run {research_id}: {chat.agent_result(research_id)[:160]}"


def _ear_winners(run: Run) -> list[str]:
    """Amp models the user's ears chose in earlier blind tests of this song."""
    from . import recipes

    if not run.recipe_id:
        return []
    try:
        recipe = recipes.get(run.recipe_id)
    except Exception:
        return []
    found = []
    for measurement in getattr(recipe, "measurements", []) or []:
        if getattr(measurement, "kind", "") == "listen" and getattr(measurement, "won", False):
            # "Model + Cab (tuned + G EQ)" -> "Model"
            model = measurement.label.split(" + ")[0].split(" (")[0].strip()
            if model and model not in found:
                found.append(model)
    return found


def _researched_cabs(run: Run) -> list[str]:
    return _researched(run, "cab")


def _researched_amps(run: Run) -> list[str]:
    return _researched(run, "amp")


def _researched(run: Run, wanted: str) -> list[str]:
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
        if str(getattr(category, "value", category)) == wanted:
            for name in (getattr(gear, "resolved_model_id", None), getattr(gear, "model", None), getattr(gear, "name", None)):
                if name:
                    names.append(name)
    return names


def _researched_settings(run: Run, model: str) -> dict | None:
    """The amp settings the research recorded, as Core controls: ``{controls, source}``.

    The gear resolved to this model first; failing that, the one amp the
    research has settings for - a player's "gain on 7" says something
    about the sound even on a modelled amp of another name.
    """
    from ..tune.settings import amp_controls
    from . import recipes

    if not run.recipe_id:
        return None
    try:
        recipe = recipes.get(run.recipe_id)
    except Exception:
        return None
    amps = [
        gear for gear in getattr(recipe, "gear", []) or []
        if str(getattr(getattr(gear, "category", ""), "value", getattr(gear, "category", ""))) == "amp" and amp_controls(gear.settings)
    ]
    if not amps:
        return None
    named = [g for g in amps if model in (g.resolved_model_id, g.model, g.name)]
    if not named and len(amps) > 1:
        return None
    gear = (named or amps)[0]
    return {"controls": amp_controls(gear.settings), "source": f"the research's settings for {gear.name}"}


def _researched_guitar(run: Run) -> str | None:
    """The record's guitar or pickup, as the research recipe names it."""
    from . import recipes

    if not run.recipe_id:
        return None
    try:
        recipe = recipes.get(run.recipe_id)
    except Exception:
        return None
    for text in (getattr(recipe, "pickup", None), getattr(recipe, "guitar", None)):
        if text:
            return text
    return None


def _take_as_di(run: Run) -> str:
    """Your own recording of the part, put on the record's clock when it fits.

    A take played along to the record (or to a click at its tempo) is one
    straight line from the record's timeline: an offset and a tempo ratio.
    When it is that close, it is stretched (pitch kept) onto the part's
    timeline and scored frame by frame like a rendered DI; when it is not,
    it is compared as a distribution and the run says why.
    """
    from ..learn.align import align_take, apply_take
    from ..match.render_offline import load_audio, resample, save_audio
    from .takes import resolve

    path = resolve(run.request.di)
    take, take_rate = load_audio(path)
    target, rate = load_audio(run.target_path)
    if take_rate != rate:
        take = resample(take, take_rate, rate)
    alignment = align_take(take, target, rate)
    if not alignment.usable:
        run.di_path = str(path)
        run.mode = "distribution"
        run.note(f"your take is {alignment.describe()}; it is compared as a distribution instead")
        return "using your take (scored as a distribution - it does not follow the record's timing)"
    placed = apply_take(take, rate, alignment, len(target), record=target)
    run.di_path = str(save_audio(_dir() / run.run_id / "take-on-the-record.wav", placed, rate))
    run.mode = "performance"
    return f"your take, {alignment.describe()}"


def _own_take(part):
    """The part's DI when it is the user's playing rather than a rendering.

    Both kinds are filed as the part's ``di_take``; a rendered one has the
    origin ``synth``. A take that has gone from the library or the disk is
    not one.
    """
    from . import takes

    if not part.di_take:
        return None
    try:
        take = takes.get(part.di_take)
    except NotFound:
        return None
    if take.origin == "synth" or not Path(take.path).is_file():
        return None
    return take


def _stage_di(run: Run, progress) -> str:
    from . import appsettings, songs

    song = songs.get(run.song_id)
    part = song.part(run.part_ids[0])
    run.target_path = str(songs.part_audio(run.song_id, part.part_id))
    if run.request.di:
        return _take_as_di(run)
    own = _own_take(part) if run.request.use_part_take else None
    if own is not None:
        run.di_path = own.path
        run.mode = "performance"
        run.note("your own take of this part, lined up with the record before, is the DI - truer than one rendered from your guitar's voice")
        return f"{part.label}: your take ({own.take_id}), already on the record's clock"
    profile = run.request.profile_id or appsettings.load().default_profile
    if not profile:
        run.note("no guitar profile, so the part is played on the synthetic string - learn your guitar for a truer test")
    like = run.request.like
    if not like and run.request.match_guitar:
        from ..learn.pickup import named

        researched = _researched_guitar(run)
        if researched and named(researched):
            like = researched
        elif researched:
            run.note(f"the research names the guitar as '{researched}', which is no pickup type rigsmith knows - the DI keeps yours")
    result = songs.render_part_di(
        run.song_id, part.part_id, profile, midi=run.request.midi, track=run.request.midi_track,
        like=like, amount=run.request.like_amount, progress=progress,
    )
    run.di_path = str(result.path)
    run.mode = "performance"
    if like:
        rendered_pickup = (result.rendered or "").split("pickup: ", 1)
        run.note("the DI's pickup was " + (rendered_pickup[1].splitlines()[0] if len(rendered_pickup) > 1 else f"moved toward {like}"))
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
    field_ = pick(gain, researched=_researched_amps(run), favoured=_ear_winners(run), count=run.request.candidates, catalog=get_catalog())
    if not field_:
        raise Invalid("no candidate amps to try")
    hosts = _device_hosts(run)
    if hosts is not None:
        heard = [c for c in field_ if (c.selector or "Amp.Type").split(".", 1)[0] in hosts]
        if len(heard) < len(field_):
            missing = sorted({(c.selector or "Amp.Type").split(".", 1)[0] for c in field_} - hosts)
            run.note(f"{len(field_) - len(heard)} candidate(s) need a {', '.join(missing)} block the loaded rig does not have, so were not heard")
        field_ = heard
        if not field_:
            raise Invalid("none of the candidate amps can be switched on the loaded rig", hint="Load a rig with an Amp block.")
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


#: Below this relative difference two cabs are a tie: run-to-run variation
#: in a reamp is about this size, so a smaller "win" is not one.
CAB_NOISE = 0.01


def _stage_cabs(run: Run, progress) -> str:
    """The best amp through the cabs worth hearing, before anything is tuned.

    Tuning the knobs of an amp through the wrong cab spends the budget
    making up for the cab; hearing the cabs first is cheap (one render each)
    and the tuner then starts from the right pair.
    """
    from ..match.metrics import ComparisonMode
    from ..match.render_offline import load_audio, resample, save_audio
    from ..match.score import score_candidate
    from ..rig.catalog import get_catalog
    from ..tune.candidates import Candidate, cab_options, virtual_cab, with_cab
    from . import songs

    if not run.results:
        raise _Skip("no amp to hear cabs with")
    best = run.results[0]
    amp = Candidate.from_dict(best)
    options = cab_options(amp.tier, researched=_researched_cabs(run), catalog=get_catalog(), count=4)
    options = [o for o in options if o[0] != amp.cab]
    hosts = _device_hosts(run)
    if hosts is not None:
        options = [o for o in options if (o[3] or "Cab.CabType").split(".", 1)[0] in hosts]
    if run.request.bench == "virtual":
        # Four simulated cabs, so one option per simulated cab - and none
        # that sounds the same as the cab the amp was already heard through.
        heard, kept = {virtual_cab(amp.cab)}, []
        for option in options:
            if virtual_cab(option[0]) not in heard:
                heard.add(virtual_cab(option[0]))
                kept.append(option)
        if len(kept) < len(options):
            run.note(f"the simulated bench has four cab shapes, so {len(kept)} of {len(options)} cabs were heard; the device bench hears every one")
        options = kept
    if not options:
        raise _Skip(f"no other cab to try with {amp.model}")

    target, rate = load_audio(run.target_path)
    di, di_rate = load_audio(run.di_path)
    if di_rate != rate:
        di = resample(di, di_rate, rate)
    comparison = ComparisonMode.PERFORMANCE if run.mode == "performance" else ComparisonMode.DISTRIBUTION
    if comparison is ComparisonMode.PERFORMANCE:
        di = di[: len(target) + rate // 2]
    render = _bench(run, rate)
    heard = []
    for index, (cab, why, cab_index, selector) in enumerate(options):
        if progress:
            progress.check()
            progress.update(index / len(options), f"hearing {amp.model} through {cab}")
        candidate = with_cab(amp, cab, why=why, index=cab_index, selector=selector)
        audio = render(candidate, di, None)
        if comparison is ComparisonMode.PERFORMANCE:
            audio = audio[: len(target)]
        scored = score_candidate(target, audio, rate, mode=comparison)
        label = f"{amp.model} + {cab}"
        path = save_audio(_dir() / run.run_id / f"{songs.slugify(label)}.wav", audio, rate)
        heard.append({**candidate.to_dict(), "label": label, "composite": scored.composite, "path": str(path), "mode": comparison.value})
    run.results = sorted(run.results + heard, key=lambda r: r["composite"])
    winner = run.results[0]
    if winner["cab"] == amp.cab:
        return f"{amp.cab} stays: {len(heard)} other cab(s) heard, none closer"
    margin = 1 - winner["composite"] / best["composite"] if best["composite"] else 0.0
    if margin < CAB_NOISE:
        return (
            f"{winner['cab']} and {amp.cab} are a tie with {amp.model} ({margin:.1%} apart, "
            "inside what the score can tell apart) - both go forward, the ear decides"
        )
    return f"{winner['cab']} beats {amp.cab} with {amp.model}, {margin:.0%} closer"


def _device_hosts(run: Run) -> set[str] | None:
    """The switchable blocks in the Core's loaded rig, for the device bench.

    None when there is nothing to check (the virtual bench, or the simulated
    loop). A rig with no Amp or no Cab ends the run here, before an hour of
    reamps that would all sound the same.
    """
    if run.request.bench != "device":
        return None
    from . import loop as hardware_loop
    from .tuning import bench_check

    if hardware_loop.get_route(run.request.route_id).backend == "virtual":
        return None
    check = bench_check()
    if not check.ok:
        raise Invalid(check.describe(), hint="Make one:  rigsmith loop bench-rig  (a draft), then write it as usual - or load any rig with an Amp and a Cab.")
    return set(check.hosts)


def _bench(run: Run, rate: int):
    """A function that plays a DI through a candidate amp, on the chosen bench."""
    if run.request.bench == "virtual":
        from ..loop.virtual import SimAmp

        def render(candidate, di, knobs):
            amp = SimAmp(Type=candidate.voicing if candidate.voicing is not None else 1)
            if candidate.cab_voicing is not None:
                amp.set(CabType=candidate.cab_voicing)
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
            if route.backend == "virtual":
                values = {"Amp": {"Type": candidate.voicing if candidate.voicing is not None else 1}}
                values["Amp"]["CabType"] = candidate.cab_voicing if candidate.cab_voicing is not None else -1
            else:
                # Each model by its own selector: a ReValver amp lives on the
                # ReValver Amp block, not on Amp's Type.
                values = candidate.device_values()
            for key, value in (knobs or {}).items():
                node, param = key.split(".", 1)
                values.setdefault(node, {})[param] = value
            try:
                sink.apply(values)
            except Exception as exc:
                raise Unavailable(
                    f"could not switch the Core to {candidate.model} / {candidate.cab}: {exc}",
                    hint="The loaded rig needs the blocks being switched - an Amp (or ReValver Amp) and a Cab.",
                ) from exc
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
    from ..match.metrics import band_differences
    from ..tune import eq as match_eq
    from ..tune.candidates import Candidate
    from ..tune.eq import informed_start
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
    # The best two *amps*, each through its best cab - after the cab stage
    # the top two results can be one amp through two cabs, and tuning both
    # would spend the whole budget on one amp.
    chosen, seen = [], set()
    for entry in run.results:
        if entry["model"] not in seen:
            seen.add(entry["model"])
            chosen.append(entry)
        if len(chosen) == 2:
            break
    for entry in chosen:
        candidate = Candidate.from_dict(entry)

        def evaluate(values, candidate=candidate):
            audio = render(candidate, di, values)
            if comparison is ComparisonMode.PERFORMANCE:
                audio = audio[: len(target)]
            return score_candidate(target, audio, rate, mode=comparison).composite, {}

        # Start from what the research says the amp was set to, when it
        # says; then where the untuned sound says to - too much low end
        # starts with the bass down. On the simulated amp the second alone
        # lowered the tuned loss by about a fifth at this budget.
        said = _researched_settings(run, candidate.model)
        current = None
        if said:
            current = space.start()
            for knob in space.knobs:
                if knob.param in said["controls"]:
                    current[knob.key] = said["controls"][knob.param]
            run.note(f"tuning {candidate.model} started from {said['source']}: " + ", ".join(f"{k} {v:.0%}" for k, v in said["controls"].items()))
        try:
            untuned, _ = load_audio(entry["path"])
            start = informed_start(space, band_differences(target, untuned, rate), current=current)
        except Exception:
            start = space.clip(current) if current else None
        result = optimise(space, evaluate, budget=budget, start=start, progress=progress)
        best = result.best
        audio = render(candidate, di, best.values)
        if comparison is ComparisonMode.PERFORMANCE:
            audio = audio[: len(target)]
        path = save_audio(_dir() / run.run_id / f"{entry['label'].replace(' ', '-').lower()}-tuned.wav", audio, rate)
        plan = match_eq.match(target, audio, rate)
        tuned.append({**entry, "label": f"{entry['label']} (tuned)", "composite": best.loss, "knobs": best.values, "path": str(path), "eq": plan.to_dict(), "untuned": entry["composite"]})
    tuned.extend(_with_eq(tuned, target, rate, comparison))
    run.results = sorted(run.results + tuned, key=lambda r: r["composite"])
    best = run.results[0]
    gain = ""
    if best.get("untuned") is not None:
        share = 1 - best["composite"] / best["untuned"] if best["untuned"] else 0.0
        gain = f", {share:.0%} closer than untuned" if share >= 0.01 else ", the knobs it started with were already about right"
    return f"best {best['label']} at {best['composite']:.3f}{gain}"


#: An EQ has to buy at least this much (relative) to go into the draft.
EQ_WORTH = 0.02


def _with_eq(tuned: list[dict], target, rate: int, comparison) -> list[dict]:
    """The best tuned result through the G EQ fitted to it - if that is closer.

    A G EQ after the cab is linear, so applying the fit offline is what the
    block would do in the chain; it is scored like everything else and kept
    only when it is actually closer, since a fit that flattens the spectrum
    can still move the score the wrong way.
    """
    from ..match.render_offline import load_audio, save_audio
    from ..match.score import score_candidate
    from ..tune.eq import GEQ_NAMES, apply_graphic

    if not tuned:
        return []
    top = min(tuned, key=lambda r: r["composite"])
    plan = top.get("eq") or {}
    # No gate on the fit's own estimate of what it buys: a fit that predicted
    # 0.8 dB moved the score 5%. Hearing it costs one scoring; the score decides.
    if not plan or not plan.get("graphic_db"):
        return []
    audio, _ = load_audio(top["path"])
    gains = [plan["graphic_db"].get(name, 0.0) for name in GEQ_NAMES]
    equalised = apply_graphic(audio, rate, gains)
    if comparison.value == "performance":
        equalised = equalised[: len(target)]
    composite = score_candidate(target, equalised, rate, mode=comparison).composite
    if composite > top["composite"] * (1 - EQ_WORTH):
        return []
    path = save_audio(Path(top["path"]).with_name(Path(top["path"]).stem + "-geq.wav"), equalised, rate)
    return [{
        **top,
        "label": top["label"].replace("(tuned)", "(tuned + G EQ)"),
        "composite": composite,
        "path": str(path),
        "geq": plan["graphic_wire"],
        "geq_db": plan["graphic_db"],
        "untuned": top.get("untuned"),
    }]


def _remaining(run: Run, best: dict) -> list[dict]:
    """Band by band: where the result the draft was built from still differs."""
    from ..match.render_offline import load_audio, resample
    from ..tune.eq import remaining

    try:
        target, rate = load_audio(run.target_path)
        audio, audio_rate = load_audio(best["path"])
    except Exception:
        return []
    if audio_rate != rate:
        audio = resample(audio, audio_rate, rate)
    return remaining(target, audio, rate)


def _stage_rig(run: Run, *, from_ears: bool = False) -> str:
    from ..tune.candidates import surrounding_blocks, wire_params
    from . import matching, rigs, songs

    best = run.results[0] if run.results else None
    if best is None:
        raise _Skip("no result to build from")
    if run.ear_pick:
        best = next((r for r in run.results if r["label"] == run.ear_pick), best)
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

    order = {"gate": 0, "boost": 1, "chorus": 4, "vibrato": 4, "tremolo": 5, "delay": 6, "reverb": 7}
    tempo = (song.structure or {}).get("tempo")
    extras = surrounding_blocks(part.sketch or {}, tempo=tempo)
    before = [b for b in extras if order.get(b["role"], 9) < 2]
    after = [b for b in extras if order.get(b["role"], 9) >= 2]
    notes = []

    def add(category, model, params=None):
        try:
            rigs.add_block(run.draft_id, category, model, params=params or {})
        except Exception as exc:
            notes.append(f"could not add {model}: {exc}")

    for block in before:
        add(block["category"], block["model"], wire_params(block))
    knobs = {k.split(".", 1)[1]: round(float(v), 4) for k, v in (best.get("knobs") or {}).items()}
    add("amp", best["model"], knobs)
    add("cab", best["cab"])
    if best.get("geq"):
        add("effect", "G EQ", wire_params({"model": "G EQ", "params": best["geq"], "readings": best.get("geq_db") or {}}))
        shown = ", ".join(f"{k[4:]} {v:+.0f}" for k, v in (best.get("geq_db") or {}).items() if abs(v) >= 1)
        run.note(f"a G EQ after the cab, fitted to the record ({shown}) - it measured closer; trust your ears over it")
    for block in after:
        add(block["category"], block["model"], wire_params(block))
    _carry_tempo(run, run.draft_id, after, tempo, structure=song.structure)
    if part.layer in ("left", "right") and _double_tracked(song, part.layer):
        run.note(
            f"the record plays this part twice, one take each side - this draft is the {part.layer} take's sound. "
            "For that width on the Core, set the amp to 2X stereo (its second model the same, or the other side's amp)"
        )
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
    # Rebuilding from a listening test keeps that test - its verdicts are
    # the reason for the rebuild.
    if len(clips) >= 2 and not from_ears:
        session_id = f"{run.run_id}-ab"
        try:
            matching.create_ab(session_id, f"{song.title}: {part.label}", clips)
            run.ab_session = session_id
        except Exception as exc:
            run.note(f"the blind A/B could not be set up: {exc}")
    run.remaining = _remaining(run, best)
    loudest = sorted((r for r in run.remaining if r["audible"] and r["band"] != "air"), key=lambda r: -abs(r["delta_db"]))[:2]
    if loudest:
        run.note("still different from the record: " + "; ".join(
            f"{r['band']} {r['delta_db']:+.1f} dB (try {r['try']})" for r in loudest
        ))
    return f"draft '{run.draft_id}' with {best['model']} and {best['cab']}"


# --------------------------------------------------------------------------
# one rig for the whole song
# --------------------------------------------------------------------------

#: The Core has five footswitches, so five scenes.
MAX_SONG_PARTS = 5

#: Effects a scene turns on only for the parts that measure as having them.
_SCENE_ROLES = ("gate", "boost", "chorus", "vibrato", "tremolo", "delay", "reverb")


def _choice(run: Run) -> dict:
    """The result a run's draft was built from: the ears' pick, or the score's."""
    if run.ear_pick:
        picked = next((r for r in run.results if r["label"] == run.ear_pick), None)
        if picked:
            return picked
    return run.results[0]


def song_rig(run_ids: list[str], *, name: str | None = None) -> dict:
    """One rig for several parts of a song: two amp sounds, a scene per part.

    A HeadRush scene switches blocks on and off (it cannot hold its own knob
    values), and the Core has two of every block - so a song rig carries up
    to two amp-and-cab sounds side by side in the chain, each scene turning
    on its part's sound and turning off the other, and every effect any part
    measures as having, on only in the scenes of the parts that have it.
    Parts that want one effect set differently - an eighth-note delay in the
    verse, a dotted eighth in the chorus - get one block each, up to two.

    When the parts' best sounds come to more than two, the two most used are
    kept and every other part is given the kept sound nearest its measured
    gain - and the result says which parts were moved, since those scenes
    will not be what their own runs found.
    """
    from ..tune.candidates import ORDER, surrounding_blocks, tier_for, wire_params
    from . import rigs, songs

    require_writable("build a song rig")
    if not run_ids:
        raise Invalid("name the autopilot runs to build from - one per part")
    if len(run_ids) > MAX_SONG_PARTS:
        raise Invalid(f"a rig has {MAX_SONG_PARTS} footswitches, so at most {MAX_SONG_PARTS} parts")
    runs = [get(run_id) for run_id in run_ids]
    for run in runs:
        if not run.results or not run.part_ids:
            raise Invalid(f"run {run.run_id} has nothing to build from - it has to have finished")
    song_ids = {run.song_id for run in runs}
    if len(song_ids) > 1:
        raise Invalid("those runs are for different songs")
    song = songs.get(runs[0].song_id)

    parts = []
    for run in runs:
        part = song.part(run.part_ids[0])
        best = _choice(run)
        parts.append({"run": run, "part": part, "best": best, "config": (best["model"], best["cab"])})

    # Keep at most two sounds: the most used, then the best-scoring.
    counts: dict[tuple, int] = {}
    for item in parts:
        counts[item["config"]] = counts.get(item["config"], 0) + 1
    ranked = sorted(counts, key=lambda c: (-counts[c], min(i["best"]["composite"] for i in parts if i["config"] == c)))
    kept = ranked[:2]
    notes = []

    def tier_of(config) -> int:
        owner = next(i for i in parts if (i["best"]["model"], i["best"]["cab"]) == config)
        return ORDER.index(owner["best"].get("tier") or tier_for(float((owner["part"].sketch or {}).get("gain") or 0)))

    for item in parts:
        if item["config"] in kept:
            continue
        gain_tier = ORDER.index(tier_for(float((item["part"].sketch or {}).get("gain") or 0)))
        nearest = min(kept, key=lambda c: abs(tier_of(c) - gain_tier))
        notes.append(
            f"{item['part'].label} found {item['config'][0]} but a rig holds two amp sounds; "
            f"its scene uses {nearest[0]}, the nearest kept one"
        )
        item["config"] = nearest

    title = name or f"{song.title} - song rig"
    draft = rigs.create(title[:40], notes=f"autopilot song rig from runs {', '.join(run_ids)}").draft
    draft_id = draft.draft_id

    def place(category, model, params=None) -> int | None:
        try:
            result = rigs.add_block(draft_id, category, model, params=params or {})
        except Exception as exc:
            notes.append(f"could not add {model}: {exc}")
            return None
        return max(block.slot for block in result.draft.rig.blocks)

    # Each effect the parts want, in at most two versions: a verse's eighth
    # note delay and a chorus's dotted eighth are two delays, one per scene,
    # not the chorus quietly given the verse's.
    wanted: dict[str, list[dict]] = {}
    tempo = (song.structure or {}).get("tempo")
    for item in parts:
        item["roles"] = {b["role"]: b for b in surrounding_blocks(item["part"].sketch or {}, tempo=tempo)}
        item["variant"] = {}
        for role, block in item["roles"].items():
            variants = wanted.setdefault(role, [])
            index = next((i for i, v in enumerate(variants) if _same_effect(v, block)), None)
            if index is None and len(variants) < MAX_EFFECT_VARIANTS:
                variants.append(block)
                index = len(variants) - 1
            elif index is None:
                index = 0
                notes.append(f"{item['part'].label} wanted a third {role}; it shares the first one")
            item["variant"][role] = index
    for role, variants in wanted.items():
        if len(variants) > 1:
            notes.append(f"the parts want different {role}s ({' and '.join(_effect_label(v) for v in variants)}), so the rig has one for each")

    slots_by_role: dict[tuple[str, int], int] = {}

    def place_role(role: str) -> None:
        for index, block in enumerate(wanted.get(role, [])):
            slot = place(block["category"], block["model"], wire_params(block))
            if slot:
                slots_by_role[(role, index)] = slot

    for role in ("gate", "boost"):
        place_role(role)
    sound_slots: dict[tuple, list[int]] = {}
    for config in kept:
        owner = next(i for i in parts if i["config"] == config and (i["best"]["model"], i["best"]["cab"]) == config)
        knobs = {k.split(".", 1)[1]: round(float(v), 4) for k, v in (owner["best"].get("knobs") or {}).items()}
        slots = [place("amp", config[0], knobs), place("cab", config[1])]
        if owner["best"].get("geq"):
            slots.append(place("effect", "G EQ", wire_params({"model": "G EQ", "params": owner["best"]["geq"], "readings": owner["best"].get("geq_db") or {}})))
        sound_slots[config] = [s for s in slots if s]
    for role in ("chorus", "vibrato", "tremolo", "delay", "reverb"):
        place_role(role)

    # Two amps, two cabs and their EQs is a lot of DSP. Past the warning
    # line, the fitted EQs go first - the least essential blocks here.
    from ..rig.validate import CPU_WARN_THRESHOLD

    if rigs.validate(draft_id).estimated_cpu >= CPU_WARN_THRESHOLD:
        for config, slots in sound_slots.items():
            for slot in list(slots):
                block = next((b for b in rigs.get(draft_id).rig.blocks if b.slot == slot), None)
                if block is not None and block.model_id == "G EQ":
                    rigs.remove_block(draft_id, slot)
                    slots.remove(slot)
        notes.append("the fitted EQs were left out to keep the rig inside the Core's DSP budget")

    _carry_tempo(None, draft_id, [wanted[role][index] for role, index in slots_by_role], tempo, notes, structure=song.structure)

    scenes = []
    for index, item in enumerate(parts, start=1):
        on = list(sound_slots.get(item["config"], []))
        off = [slot for config, slots in sound_slots.items() if config != item["config"] for slot in slots]
        for (role, version), slot in slots_by_role.items():
            (on if item["variant"].get(role) == version else off).append(slot)
        scene_name = _unique_scene_name(item["part"].label, [s["scene"] for s in scenes])
        rigs.configure_scene(draft_id, scene_name, on_slots=on, off_slots=off, footswitch=index, reset=True)
        scenes.append({"footswitch": index, "scene": scene_name, "part": item["part"].part_id, "sound": item["config"][0], "run": item["run"].run_id})
    return {"draft_id": draft_id, "name": title[:40], "scenes": scenes, "sounds": [list(c) for c in kept], "notes": notes}


#: Below this beat-tracking confidence, a detected tempo is called a guess.
SURE_TEMPO = 0.35


def _carry_tempo(run: Run | None, draft_id: str, blocks: list[dict], tempo: float | None, notes: list | None = None, *, structure: dict | None = None) -> None:
    """Give the rig the song's tempo when anything in it is synced to the beat.

    A synced delay is a dotted eighth of *whatever tempo the device is at* -
    the last song tapped in, or the last rig's. Saved as the rig's own
    (Fixed) tempo, it loads on the beat; tap tempo still moves it live.
    """
    from ..rig.model import TEMPO_RANGE
    from . import rigs

    synced = [b for b in blocks if b.get("division")]
    if not synced or not tempo:
        return
    what = ", ".join(f"{b['role']} on {b['division']}" for b in synced)
    if not TEMPO_RANGE[0] <= tempo <= TEMPO_RANGE[1]:
        text = f"the song's {tempo:.0f} BPM is outside the rig tempos the Core stores - tap it in for the {what}"
    else:
        rigs.set_tempo(draft_id, round(float(tempo), 2))
        text = f"the rig carries the song's tempo ({tempo:.1f} BPM, Fixed), so the {what} land on the beat - tap tempo still moves it live"
    structure = structure or {}
    if structure.get("tempo_source") != "hand" and float(structure.get("tempo_confidence", 1.0)) < SURE_TEMPO:
        text += "; the tempo was a guess - check it on the song's page (half or double is the usual mistake)"
    if run is not None:
        run.note(text)
    if notes is not None:
        notes.append(text)


#: The longest scene name a song rig gives.
SCENE_NAME_LENGTH = 20


def _unique_scene_name(label: str, taken: list[str]) -> str:
    """A part's label as a scene name no other scene in the rig has.

    Section names repeat - a song has two choruses - so two chosen parts can
    share a label, and a scene is found by its name: the second would have
    overwritten the first, footswitch and all.
    """
    used = {name.lower() for name in taken}
    name = label[:SCENE_NAME_LENGTH]
    count = 2
    while name.lower() in used:
        suffix = f" {count}"
        name = label[: SCENE_NAME_LENGTH - len(suffix)].rstrip() + suffix
        count += 1
    return name


#: How many versions of one effect a song rig carries - the Core has two of each block.
MAX_EFFECT_VARIANTS = 2


def _same_effect(a: dict, b: dict) -> bool:
    """Two parts' versions of an effect close enough to share one block."""
    if a["model"] != b["model"]:
        return False
    if a.get("division") or b.get("division"):
        return a.get("division") == b.get("division")
    pa, pb = a.get("params") or {}, b.get("params") or {}
    for name in set(pa) & set(pb):
        x, y = pa[name], pb[name]
        if isinstance(x, bool) or isinstance(y, bool) or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            if x != y:
                return False
        elif abs(float(x) - float(y)) > 0.08:
            return False
    return True


def _effect_label(block: dict) -> str:
    if block.get("division"):
        return block["division"]
    if block.get("delay_ms"):
        return f"{block['delay_ms']:.0f} ms"
    return block["model"]


def _double_tracked(song, layer: str) -> bool:
    """Whether the song's map calls this side one take of a double-tracked part."""
    for entry in (song.layers or {}).get("layers", []):
        if entry.get("layer_id") == layer and "double-tracked" in (entry.get("role") or ""):
            return True
    return False


def render_song_rig(result: dict) -> str:
    lines = [f"song rig '{result['draft_id']}': {len(result['scenes'])} scene(s), {len(result['sounds'])} amp sound(s)"]
    lines.extend(f"  FS{s['footswitch']}  {s['scene']:<20} {s['sound']}" for s in result["scenes"])
    lines.extend(f"  note: {n}" for n in result["notes"])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# listening back
# --------------------------------------------------------------------------


def listen_back(run_id: str) -> Run:
    """Let the blind A/B decide: the draft follows the ears, and the recipe learns.

    Metrics rank; ears decide. Once the run's blind test is finished, every
    finalist's verdict goes into the song's recipe as a
    listening measurement - so research and later runs know what actually
    sounded right - and if the ears' winner is not the score's, the draft is
    rebuilt from it. A tie at the top changes nothing.
    """
    from . import matching, recipes

    require_writable("apply a listening test")
    run = get(run_id)
    if not run.ab_session:
        raise Invalid("this run has no listening test", hint="Runs set one up when two or more candidates were heard.")
    session = matching.load_ab(run.ab_session)
    if not session.complete:
        # The names stay hidden until the test is done; reading the standings
        # half way would tell the listener which is which.
        raise Invalid(
            f"the blind test is not finished ({session.answered_count} of {len(session.trials)} answered)",
            hint="Finish it in the Match panel (or 'rigsmith match ab next') - the names stay hidden until then.",
        )
    standings = session.standings()
    top_points = standings[0][1]
    leaders = [label for label, points, _ in standings if points == top_points]
    score_pick = run.results[0]["label"] if run.results else None
    progress = f"{session.answered_count} of {len(session.trials)} trials"

    if run.recipe_id:
        for label, points, seen in standings:
            try:
                recipes.record_measurement(
                    run.recipe_id, label, f"autopilot {run.run_id}", kind="listen",
                    won=(label in leaders and len(leaders) == 1), trials=seen,
                    note=f"{points:g} point(s) in {seen} blind comparison(s), {progress}",
                )
            except Exception as exc:  # the verdict stands even if the recipe is gone
                run.note(f"could not record the verdict in the recipe: {exc}")

    if len(leaders) > 1:
        run.note(f"the listening test is a tie between {' and '.join(leaders)} after {progress} - the draft keeps the score's pick")
    elif leaders[0] == score_pick:
        run.ear_pick = None
        run.note(f"your ears agreed with the score: {score_pick} ({progress})")
    else:
        run.ear_pick = leaders[0]
        _stage_rig(run, from_ears=True)
        run.note(f"your ears picked {leaders[0]} over the score's {score_pick} ({progress}); the draft now uses it")
    run.save()
    events.publish({"type": "autopilot", "run_id": run.run_id, "stage": "rig", "state": "done"})
    return run


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
        "cabs": lambda: _stage_cabs(run, progress),
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
    if len(run.part_ids) > 1 and not run.part_runs:
        _chase_other_parts(run, progress)
    run.state = "done"
    run.finished_at = _now()
    run.save()
    events.publish({"type": "autopilot", "run_id": run.run_id, "state": "done"})
    if progress:
        progress.update(1.0, "done")
    return run


def _chase_other_parts(run: Run, progress) -> None:
    """Every other chosen part gets its own run, then one rig takes them all.

    A run chases one part - its own DI, candidates, cabs and tuning - so the
    second and later chosen parts each get a run of their own (same song,
    same guitar, same bench), one after another in this job, and their
    results are composed into a song rig with a scene per part.
    """
    from dataclasses import replace

    others = run.part_ids[1:]
    if len(run.part_ids) > MAX_SONG_PARTS:
        run.note(f"{len(run.part_ids)} parts were chosen and a rig has {MAX_SONG_PARTS} footswitches; the first {MAX_SONG_PARTS} were chased")
        others = run.part_ids[1:MAX_SONG_PARTS]
    for position, part_id in enumerate(others, start=1):
        if progress:
            progress.check()
            progress.update(None, f"part {position + 1} of {len(others) + 1}: {part_id}")
        child = Run(
            run_id=f"{run.run_id}-{position + 1}",
            request=replace(run.request, parts=[part_id], song_id=run.song_id, mix=None, stem=None),
        )
        child.save()
        child = execute(child.run_id, progress=progress)
        run.part_runs.append(child.run_id)
        run.save()
        if child.state != "done":
            run.note(f"the run for {part_id} did not finish ({child.state}) - it is left out of the song rig")
    finished = [run.run_id] + [r for r in run.part_runs if get(r).state == "done"]
    if len(finished) > 1:
        try:
            run.song_rig = song_rig(finished)
            run.note(f"song rig '{run.song_rig['draft_id']}': a scene per part on footswitches 1-{len(finished)}")
        except Exception as exc:
            run.note(f"the song rig could not be built: {exc}")

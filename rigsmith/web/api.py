"""HTTP API over the core service layer.

Routes translate JSON to core calls and core results back to JSON. Core
errors carry their own HTTP status, so the mapping is one handler rather
than per-route try/except.

Imports sit at module scope on purpose: FastAPI resolves route annotations
against module globals, and this file uses ``from __future__ import
annotations``. With them function-local, every annotation degrades silently
into a query parameter.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..core import captures, devices, matching, recipes, rigs, workspace
from ..core.errors import Invalid, NotFound, RigsmithError
from ..core.jobs import get_manager
from ..rig.codec import rig_to_dict

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------------
# request bodies
# --------------------------------------------------------------------------


class CreateDraft(BaseModel):
    name: str
    signal_path: str = "straight"
    notes: str | None = None


class AddBlock(BaseModel):
    category: str
    model_id: str
    slot: int | None = None
    params: dict[str, Any] | None = None


class MoveBlock(BaseModel):
    to_slot: int


class PatchBlock(BaseModel):
    name: str | None = None
    value: Any = None
    enabled: bool | None = None
    doubling: str | None = None
    second_model_id: str | None = None


class SceneBody(BaseModel):
    name: str
    on_slots: list[int] = []
    off_slots: list[int] = []
    no_change_slots: list[int] = []
    footswitch: int | None = None
    reset: bool = False


class RigSettings(BaseModel):
    signal_path: str | None = None
    rig_width: int | None = None
    name: str | None = None


class PruneBody(BaseModel):
    areas: list[str] | None = None
    older_than_days: float | None = None
    apply: bool = False


class CreateRecipe(BaseModel):
    title: str
    artist: str | None = None
    album: str | None = None
    year: int | None = None
    summary: str | None = None
    guitar: str | None = None
    tuning: str | None = None


class GearBody(BaseModel):
    role: str
    name: str
    category: str = "effect"
    confidence: str = "guessed"
    make: str | None = None
    model: str | None = None
    settings: dict[str, Any] = {}
    source_title: str | None = None
    source_url: str | None = None
    source_note: str | None = None
    alternatives: list[str] = []
    note: str | None = None


class RecipeSceneBody(BaseModel):
    name: str
    roles_on: list[str] = []
    roles_off: list[str] = []
    footswitch: int | None = None


class QuestionBody(BaseModel):
    question: str


class MeasurementBody(BaseModel):
    label: str
    target: str
    kind: str = "score"
    mode: str | None = None
    composite: float | None = None
    region: str | None = None
    won: bool | None = None
    trials: int | None = None
    note: str | None = None


class ResolveBody(BaseModel):
    role: str
    model_id: str


class ScoreBody(BaseModel):
    target_path: str
    candidate_paths: dict[str, str]
    mode: str = "distribution"
    # [start, end] in seconds, or null for the whole file. Separate because
    # the passage you care about rarely sits at the same place in both.
    target_region: list[float] | None = None
    candidate_region: list[float] | None = None


class BandsBody(BaseModel):
    target_path: str
    candidate_path: str
    target_region: list[float] | None = None
    candidate_region: list[float] | None = None


class DownloadCapture(BaseModel):
    tone_id: str
    architecture: str = "2"
    limit: int = 4
    any_size: bool = False
    install_to_device: bool = False


class OpenAudio(BaseModel):
    path: str


class IsolateBody(BaseModel):
    audio_path: str
    output_dir: str | None = None


class RenderBody(BaseModel):
    di_path: str
    nam_path: str | None = None
    ir_path: str | None = None
    label: str = "candidate"


class SearchBody(BaseModel):
    di_path: str
    target_path: str
    captures: dict[str, str]
    ir_path: str | None = None
    mode: str = "distribution"
    target_region: list[float] | None = None
    candidate_region: list[float] | None = None


class CaptureDiBody(BaseModel):
    duration_s: float = 15.0
    name: str = "take"


class ReampBody(BaseModel):
    di_path: str
    label: str = "candidate"


class MidiBody(BaseModel):
    port: str | None = None
    channel: int = 0


class MidiSelect(MidiBody):
    program: int


class MidiFootswitch(MidiBody):
    footswitch: int


class MidiTap(MidiBody):
    bpm: float
    taps: int = 4


class MidiBlock(MidiBody):
    slot: int
    enabled: bool


class MidiVerify(MidiBody):
    draft_id: str
    program: int | None = None


class AbCreate(BaseModel):
    session_id: str
    target_label: str
    candidates: dict[str, str]
    pairs: int | None = None


class AbVerdict(BaseModel):
    trial_id: int
    verdict: str
    confidence: int | None = None
    comment: str | None = None


# --------------------------------------------------------------------------
# error handling
# --------------------------------------------------------------------------


def install_error_handler(app) -> None:
    """Map every core error onto its HTTP status, once."""

    @app.exception_handler(RigsmithError)
    async def _handle(_request: Request, exc: RigsmithError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.message, "hint": exc.hint},
        )


# --------------------------------------------------------------------------
# meta
# --------------------------------------------------------------------------


@router.get("/health")
def health() -> dict:
    from .. import __version__
    from ..config import get_settings

    settings = get_settings()
    return {
        "version": __version__,
        "readonly": settings.readonly,
        "workspace": str(settings.workspace),
    }


@router.get("/capabilities")
def capabilities() -> dict:
    """What this install can actually do, so the UI can grey things out."""

    def available(module: str) -> bool:
        try:
            __import__(module)
            return True
        except ImportError:
            return False

    from ..rig.catalog import get_catalog

    catalog = get_catalog()
    return {
        "midi": available("mido"),
        "audio": available("sounddevice") and available("soundfile"),
        "separation": available("demucs"),
        "offline_nam": available("nam"),
        "scipy": available("scipy"),
        "tone3000": captures.auth_status().to_dict(),
        "catalog": {
            "source": catalog.source,
            "models": len(catalog.models),
            "verified": catalog.is_verified,
        },
    }


# --------------------------------------------------------------------------
# device
# --------------------------------------------------------------------------


@router.get("/device/discover")
async def device_discover(sweep: bool = True) -> dict:
    from dataclasses import asdict

    found = await devices.discover(include_sweep=sweep)
    return {"devices": [asdict(d) for d in found]}


@router.get("/device/storage")
def device_storage() -> dict:
    volume = devices.storage_status()
    return {
        "root": str(volume.root),
        "folders": {
            name: len([p for p in volume.folder(name).rglob("*") if p.is_file()])
            for name in ("Rigs", "Setlists", "Blocks", "Impulse Responses",
                         "Amp Clones", "NAM")
            if volume.exists(name)
        },
        "describe": volume.describe(),
    }


@router.post("/device/backup")
def device_backup() -> dict:
    result = devices.backup()
    return {"describe": result.describe(), "files": result.file_count}


@router.get("/device/midi")
def device_midi() -> dict:
    """List MIDI ports.

    Asking what exists is a capability question, not an action, so a missing
    backend comes back as an empty list with a reason rather than a 503. The
    panel can then say what to install in place of the port list, instead of
    throwing a toast over an otherwise blank section.
    """
    from ..device.midi import MidiUnavailable

    try:
        outputs, inputs = devices.midi_ports()
    except MidiUnavailable as exc:
        return {"outputs": [], "inputs": [], "error": exc.message, "hint": exc.hint}
    return {"outputs": outputs, "inputs": inputs, "error": None}


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


@router.post("/device/midi/select")
def midi_select(body: MidiSelect) -> dict:
    return {"message": devices.select_rig(
        body.program, port=body.port, channel=body.channel
    )}


@router.post("/device/midi/footswitch")
def midi_footswitch(body: MidiFootswitch) -> dict:
    """Press a footswitch - which is also how a scene is activated.

    There is no scene-select CC on this device, so a scene is reached by
    pressing the switch it is assigned to. That is the whole mechanism, and
    it is why scenes carry a footswitch number in the rig model.
    """
    return {"message": devices.press_footswitch(
        body.footswitch, port=body.port, channel=body.channel
    )}


@router.post("/device/midi/tap")
def midi_tap(body: MidiTap) -> dict:
    """Set the tempo by tapping it, because there is no set-BPM message."""
    return {"message": devices.tap_tempo(
        body.bpm, taps=body.taps, port=body.port, channel=body.channel
    )}


@router.post("/device/midi/block")
def midi_block(body: MidiBlock) -> dict:
    return {"message": devices.set_block(
        body.slot, body.enabled, port=body.port, channel=body.channel
    )}


@router.post("/device/midi/verify")
def midi_verify(body: MidiVerify) -> dict:
    """Drive a draft's rig over MIDI on a channel independent of the write.

    Not a read-back: the device reports no block state over MIDI, so this
    shows the rig loads and responds, not that its contents match.
    """
    result = devices.verify_rig(
        body.draft_id, program=body.program, port=body.port, channel=body.channel
    )
    return {**result.to_dict(), "rendered": result.render()}


@router.get("/catalog/search")
def catalog_search(q: str = "", category: str | None = None, limit: int = 60) -> dict:
    from ..rig.catalog import get_catalog

    cat = rigs.parse_category(category) if category else None
    results = get_catalog().search(q, category=cat, limit=limit)
    return {
        "verified": get_catalog().is_verified,
        "models": [
            {
                "id": m.id,
                "name": m.name,
                "category": m.category.value,
                "family": m.family,
                "subcategory": m.subcategory,
                "verified": m.verified,
                "params": [
                    {
                        "name": p.name,
                        "kind": p.kind,
                        "min": p.minimum,
                        "max": p.maximum,
                        "default": p.default,
                        "unit": p.unit,
                        "choices": p.choices,
                    }
                    for p in m.params
                ],
            }
            for m in results
        ],
    }


# --------------------------------------------------------------------------
# drafts
# --------------------------------------------------------------------------


def _draft_payload(draft) -> dict:
    result = rigs.validate(draft.draft_id)
    return {
        "draft_id": draft.draft_id,
        "updated_at": draft.updated_at,
        "rig": rig_to_dict(draft.rig),
        "chain": draft.rig.signal_chain(),
        "stereo": draft.rig.is_stereo,
        "free_slots": draft.rig.free_slots,
        "validation": {
            "ok": result.ok,
            "estimated_cpu": result.estimated_cpu,
            "findings": [
                {"severity": f.severity, "message": f.message,
                 "slot": f.slot, "scene": f.scene}
                for f in result.findings
            ],
        },
    }


@router.get("/drafts")
def list_drafts() -> dict:
    return {
        "drafts": [
            {
                "draft_id": d.draft_id,
                "name": d.rig.name,
                "blocks": len(d.rig.blocks),
                "scenes": len(d.rig.scenes),
                "updated_at": d.updated_at,
            }
            for d in rigs.list_drafts()
        ]
    }


@router.post("/drafts")
def create_draft(body: CreateDraft) -> dict:
    result = rigs.create(body.name, signal_path=body.signal_path, notes=body.notes)
    return _draft_payload(result.draft)


@router.get("/drafts/{draft_id}")
def get_draft(draft_id: str) -> dict:
    return _draft_payload(rigs.get(draft_id))


@router.delete("/drafts/{draft_id}")
def delete_draft(draft_id: str) -> dict:
    return {"message": rigs.delete(draft_id)}


@router.post("/drafts/{draft_id}/duplicate")
def duplicate_draft(draft_id: str) -> dict:
    return _draft_payload(rigs.duplicate(draft_id).draft)


@router.patch("/drafts/{draft_id}")
def update_rig(draft_id: str, body: RigSettings) -> dict:
    if body.signal_path is not None:
        rigs.set_signal_path(draft_id, body.signal_path)
    if body.rig_width is not None:
        rigs.set_rig_width(draft_id, body.rig_width)
    if body.name is not None:
        rigs.rename(draft_id, body.name)
    return _draft_payload(rigs.get(draft_id))


@router.post("/drafts/{draft_id}/blocks")
def add_block(draft_id: str, body: AddBlock) -> dict:
    result = rigs.add_block(
        draft_id, body.category, body.model_id, slot=body.slot, params=body.params
    )
    return _draft_payload(result.draft)


@router.delete("/drafts/{draft_id}/blocks/{slot}")
def remove_block(draft_id: str, slot: int) -> dict:
    return _draft_payload(rigs.remove_block(draft_id, slot).draft)


@router.post("/drafts/{draft_id}/blocks/{slot}/move")
def move_block(draft_id: str, slot: int, body: MoveBlock) -> dict:
    return _draft_payload(rigs.move_block(draft_id, slot, body.to_slot).draft)


@router.patch("/drafts/{draft_id}/blocks/{slot}")
def patch_block(draft_id: str, slot: int, body: PatchBlock) -> dict:
    if body.enabled is not None:
        rigs.toggle_block(draft_id, slot, body.enabled)
    if body.doubling is not None:
        rigs.set_doubling(draft_id, slot, body.doubling, body.second_model_id)
    if body.name is not None:
        rigs.set_param(draft_id, slot, body.name, body.value)
    return _draft_payload(rigs.get(draft_id))


@router.post("/drafts/{draft_id}/scenes")
def configure_scene(draft_id: str, body: SceneBody) -> dict:
    result = rigs.configure_scene(
        draft_id,
        body.name,
        on_slots=body.on_slots,
        off_slots=body.off_slots,
        no_change_slots=body.no_change_slots,
        footswitch=body.footswitch,
        reset=body.reset,
    )
    return _draft_payload(result.draft)


@router.delete("/drafts/{draft_id}/scenes/{name}")
def delete_scene(draft_id: str, name: str) -> dict:
    return _draft_payload(rigs.delete_scene(draft_id, name).draft)


@router.get("/drafts/{draft_id}/preview")
def preview_draft(draft_id: str) -> dict:
    difference = rigs.preview(draft_id)
    result = rigs.validate(draft_id)
    return {
        "changes": [
            {"kind": c.kind, "description": c.description, "slot": c.slot}
            for c in difference.changes
        ],
        "summary": difference.summary(),
        "rendered": difference.render(),
        "ok": result.ok,
        "estimated_cpu": result.estimated_cpu,
    }


@router.post("/drafts/{draft_id}/export")
def export_draft(draft_id: str) -> dict:
    return {"path": str(rigs.export(draft_id))}


@router.post("/drafts/{draft_id}/write")
def write_draft(
    draft_id: str, confirm: bool = False, allow_without_backup: bool = False
) -> dict:
    """Preview a write, or perform one.

    The preview is synchronous - it touches nothing but the draft. The real
    write is a job, because finding the device can take ten seconds or more
    when nothing answers, and a page that simply stops for that long looks
    broken rather than busy.
    """
    if not confirm:
        return rigs.write(draft_id, confirm=False).to_dict()

    job = get_manager().submit(
        f"write '{draft_id}' to the device",
        lambda progress: (
            progress.update(0.1, "looking for the device"),
            rigs.write(
                draft_id,
                confirm=True,
                allow_without_backup=allow_without_backup,
            ).to_dict(),
        )[1],
    )
    return {"job_id": job.id, "name": job.name}


# --------------------------------------------------------------------------
# captures
# --------------------------------------------------------------------------


@router.get("/captures/scan")
def scan_captures(directory: str) -> dict:
    models = captures.scan(directory)
    return {"captures": [m.to_dict() for m in models]}


@router.get("/captures/inspect")
def inspect_capture(path: str) -> dict:
    model = captures.inspect(path)
    return {**model.to_dict(), "rendered": captures.render_inspection(model)}


@router.get("/tone3000/status")
def tone3000_status() -> dict:
    return captures.auth_status().to_dict()


@router.post("/tone3000/login")
def tone3000_login() -> dict:
    """Start the OAuth flow, and hand back the URL as well as opening it.

    A job rather than a request: the flow waits on a person deciding something
    in another tab, which is minutes at the outer edge and is not a thing to
    hold a connection open for. The URL comes back in the job's log because a
    browser that refuses to open - or opens somewhere the user is not looking
    - should not be a dead end.
    """
    def run(progress):
        import asyncio

        captures.login(
            open_browser=True,
            on_url=lambda url: progress.update(None, url),
        )
        # Ask who that was, so the panel can say more than "authenticated".
        return asyncio.run(captures.identify()).to_dict()

    job = get_manager().submit("TONE3000 sign-in", run)
    return {"job_id": job.id, "name": job.name}


@router.post("/tone3000/download")
def tone3000_download(body: DownloadCapture) -> dict:
    """Download a tone's captures as a job, and check each against its file.

    A job because a handful of models is tens of megabytes over somebody
    else's rate limit, and because the page should show it happening.
    """
    import asyncio

    def run(progress):
        results = asyncio.run(
            captures.download(
                body.tone_id,
                architecture=body.architecture,
                limit=body.limit,
                loadable_only=not body.any_size,
                progress=progress,
            )
        )
        installed = []
        if body.install_to_device:
            for result in results:
                if result.loadable is False:
                    installed.append(
                        f"skipped {result.path.name} - the Core will not load it"
                    )
                else:
                    installed.append(devices.install_capture(result.path))
        return {
            "downloaded": [r.to_dict() for r in results],
            "installed": installed,
            "rendered": captures.render_download(results),
        }

    job = get_manager().submit(f"download tone {body.tone_id}", run)
    return {"job_id": job.id, "name": job.name}


@router.get("/tone3000/search")
async def tone3000_search(
    q: str = "",
    make: str | None = None,
    architecture: str = "2",
    calibrated: bool = False,
    limit: int = 15,
) -> dict:
    tones = await captures.search(
        q, make=make, architecture=architecture, calibrated=calibrated, limit=limit
    )
    return {
        "tones": [
            {
                "id": t.id,
                "title": t.title,
                "makes": t.makes,
                "tags": t.tags,
                "sizes": t.sizes,
                "a1": t.a1_models_count,
                "a2": t.a2_models_count,
                "downloads": t.downloads_count,
                "creator": t.creator,
                "url": t.url,
            }
            for t in tones
        ]
    }


# --------------------------------------------------------------------------
# recipes
# --------------------------------------------------------------------------


def _recipe_payload(recipe_id: str, recipe) -> dict:
    return {
        "recipe_id": recipe_id,
        "confidence": recipe.confidence,
        "unsupported": [g.role for g in recipe.unsupported],
        "verified": recipe.verified,
        "rendered": recipe.render(),
        **recipe.to_dict(),
    }


@router.get("/recipes")
def list_recipes() -> dict:
    return {
        "recipes": [
            {
                "recipe_id": recipe_id,
                "title": recipe.title,
                "artist": recipe.artist,
                "gear": len(recipe.gear),
                "scenes": len(recipe.scenes),
                "confidence": recipe.confidence,
                "questions": len(recipe.open_questions),
                "measurements": len(recipe.measurements),
                "verified": recipe.verified,
            }
            for recipe_id, recipe in recipes.list_recipes()
        ]
    }


@router.post("/recipes")
def create_recipe(body: CreateRecipe) -> dict:
    recipe_id, recipe = recipes.create(
        body.title, artist=body.artist, album=body.album, year=body.year,
        summary=body.summary, guitar=body.guitar, tuning=body.tuning,
    )
    return _recipe_payload(recipe_id, recipe)


@router.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: str) -> dict:
    return _recipe_payload(recipe_id, recipes.get(recipe_id))


@router.delete("/recipes/{recipe_id}")
def delete_recipe(recipe_id: str) -> dict:
    return {"message": recipes.delete(recipe_id)}


@router.post("/recipes/{recipe_id}/gear")
def add_gear(recipe_id: str, body: GearBody) -> dict:
    recipe = recipes.add_gear(
        recipe_id, body.role, body.name, category=body.category,
        make=body.make, model=body.model, settings=body.settings,
        confidence=body.confidence, source_title=body.source_title,
        source_url=body.source_url, source_note=body.source_note,
        alternatives=body.alternatives, note=body.note,
    )
    return _recipe_payload(recipe_id, recipe)


@router.post("/recipes/{recipe_id}/scenes")
def add_recipe_scene(recipe_id: str, body: RecipeSceneBody) -> dict:
    recipe = recipes.add_scene(
        recipe_id, body.name, roles_on=body.roles_on,
        roles_off=body.roles_off, footswitch=body.footswitch,
    )
    return _recipe_payload(recipe_id, recipe)


@router.post("/recipes/{recipe_id}/questions")
def add_question(recipe_id: str, body: QuestionBody) -> dict:
    return _recipe_payload(recipe_id, recipes.add_question(recipe_id, body.question))


@router.post("/recipes/{recipe_id}/resolve")
def resolve_gear(recipe_id: str, body: ResolveBody) -> dict:
    recipe = recipes.resolve_gear(recipe_id, body.role, body.model_id)
    return _recipe_payload(recipe_id, recipe)


@router.post("/recipes/{recipe_id}/measurements")
def add_measurement(recipe_id: str, body: MeasurementBody) -> dict:
    """Record what testing this recipe produced, not just what was believed."""
    recipe = recipes.record_measurement(
        recipe_id,
        body.label,
        body.target,
        kind=body.kind,
        mode=body.mode,
        composite=body.composite,
        region=body.region,
        won=body.won,
        trials=body.trials,
        note=body.note,
    )
    return _recipe_payload(recipe_id, recipe)


@router.post("/recipes/{recipe_id}/compile")
def compile_recipe(recipe_id: str) -> dict:
    result = recipes.compile_to_draft(recipe_id)
    return {**result.to_dict(), "rendered": result.render()}


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------


@router.get("/match/devices")
def match_devices() -> dict:
    from ..match.render_device import AudioUnavailable

    try:
        found = matching.audio_devices()
    except AudioUnavailable as exc:
        return {"devices": [], "error": str(exc)}
    return {
        "devices": [
            {
                "index": d.index,
                "name": d.name,
                "inputs": d.max_input_channels,
                "outputs": d.max_output_channels,
                "headrush": d.looks_like_headrush,
                "full_layout": d.has_full_channel_layout,
            }
            for d in found
        ]
    }


@router.post("/match/score")
def match_score(body: ScoreBody) -> dict:
    _allow_audio(body.target_path, *body.candidate_paths.values())
    scores = matching.score(
        body.target_path,
        body.candidate_paths,
        mode=body.mode,
        target_region=body.target_region,
        candidate_region=body.candidate_region,
    )
    return {
        "mode": body.mode,
        "target_region": body.target_region,
        "candidate_region": body.candidate_region,
        "ranking": [
            {
                "label": s.label,
                "composite": s.composite,
                "metrics": [
                    {"name": m.name, "value": m.value, "mode": m.mode.value,
                     "interpretation": m.interpretation}
                    for m in s.metrics
                ],
                "notes": s.notes,
            }
            for s in scores
        ],
        "rendered": matching.render_ranking(scores),
    }


@router.post("/match/isolate")
def match_isolate(body: IsolateBody) -> dict:
    """Pull a guitar stem out of a mixed recording. Minutes, so it is a job."""
    _allow_audio(body.audio_path)
    job = get_manager().submit(
        f"isolate {Path(body.audio_path).name}",
        lambda progress: matching.isolate_guitar(
            body.audio_path, out_dir=body.output_dir, progress=progress
        ),
    )
    return {"job_id": job.id, "name": job.name}


@router.post("/match/render")
def match_render(body: RenderBody) -> dict:
    """Render one DI through a capture and a cab, in Python. No hardware."""
    _allow_audio(body.di_path)
    rendered = matching.render_offline(
        body.di_path,
        nam_path=body.nam_path,
        ir_path=body.ir_path,
        label=body.label,
    )
    _allow_audio(rendered.path)
    return {**rendered.to_dict(), "url": _audio_url(rendered.path)}


@router.post("/match/search")
def match_search(body: SearchBody) -> dict:
    """The whole Layer 1 loop: one DI, every capture, one ranking.

    A job rather than a request, because rendering twenty captures is minutes
    of work and the page should show it happening rather than hang.
    """
    _allow_audio(body.di_path, body.target_path)
    job = get_manager().submit(
        f"render and rank {len(body.captures)} captures",
        lambda progress: matching.run_offline_search(
            body.di_path,
            body.target_path,
            body.captures,
            ir_path=body.ir_path,
            mode=body.mode,
            target_region=body.target_region,
            candidate_region=body.candidate_region,
            progress=progress,
        ).to_dict(),
    )
    return {"job_id": job.id, "name": job.name}


@router.post("/match/capture-di")
def match_capture_di(body: CaptureDiBody) -> dict:
    """Record one performance: the wet tone and the dry DI together."""
    capture, paths = matching.capture_di(body.duration_s, name=body.name)
    return {
        "describe": capture.describe(),
        "latency_measured": capture.latency_measured,
        "safe_for_aligned_scoring": capture.safe_for_aligned_scoring,
        "paths": {name: str(path) for name, path in paths.items()},
        "urls": {name: _audio_url(path) for name, path in paths.items()},
    }


@router.post("/match/reamp")
def match_reamp(body: ReampBody) -> dict:
    """Play a DI through the Core's currently loaded rig and record it."""
    _allow_audio(body.di_path)
    rendered = matching.reamp(body.di_path, label=body.label)
    _allow_audio(rendered.path)
    return {**rendered.to_dict(), "url": _audio_url(rendered.path)}


@router.post("/match/bands")
def match_bands(body: BandsBody) -> dict:
    """Where a candidate differs from the target, in the terms knobs are in."""
    _allow_audio(body.target_path, body.candidate_path)
    differences = matching.compare_bands(
        body.target_path,
        body.candidate_path,
        target_region=body.target_region,
        candidate_region=body.candidate_region,
    )
    label = Path(body.candidate_path).stem
    return {
        "bands": [
            {
                "name": d.name,
                "low": d.low,
                "high": d.high,
                "delta_db": d.delta_db,
                "audible": d.audible,
                "meaning": d.meaning,
            }
            for d in differences
        ],
        "rendered": matching.render_bands(differences, label=label),
    }


@router.get("/match/spectrum")
def match_spectrum(
    path: str,
    bands: int = 96,
    start: float | None = None,
    end: float | None = None,
) -> dict:
    """Average log spectrum of a file, for the comparison plot.

    Takes the same region as scoring does, so the curve you are looking at is
    the one the numbers came from rather than the whole track's average.
    """
    import numpy as np

    from ..core.matching import Region
    from ..match.metrics import _stft_magnitude, normalise_rms, to_mono
    from ..match.render_offline import load_audio

    _allow_audio(path)
    audio, sample_rate = load_audio(path)
    region = Region.parse(None if start is None and end is None else (start or 0.0, end))
    if region:
        audio = region.slice(audio, sample_rate, label=Path(path).name)
    audio = normalise_rms(to_mono(audio))

    n_fft = 2048
    magnitude = _stft_magnitude(audio, n_fft, n_fft // 4).mean(axis=0)
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)

    # Log-spaced bands: a linear FFT plot wastes most of its width on the
    # top octave, where guitar tone differences are least legible.
    low, high = 40.0, min(16000.0, sample_rate / 2)
    edges = np.logspace(np.log10(low), np.log10(high), bands + 1)
    centres, values = [], []
    for start, end in zip(edges[:-1], edges[1:]):
        mask = (freqs >= start) & (freqs < end)
        if not mask.any():
            continue
        centres.append(float((start + end) / 2))
        values.append(float(20 * np.log10(magnitude[mask].mean() + 1e-12)))

    return {
        "path": path,
        "freqs": centres,
        "db": values,
        "region": None if region is None else [region.start, region.end],
    }


# --------------------------------------------------------------------------
# blind A/B
# --------------------------------------------------------------------------


def _audio_url(path) -> str:
    """A URL for a file the caller already named.

    Quoted, because a Windows path is full of backslashes and spaces, and
    named, because rendered clips routinely live outside the workspace.
    """
    if not path:
        return ""
    _allow_audio(path)
    return f"/api/audio?path={quote(str(path))}"


def _blind_url(session_id: str, blinded: str) -> str:
    """A URL that says nothing about which candidate is on the other end.

    The clips are already copied to opaque filenames when a session is
    created, but a path-carrying URL would still put the directory - and on a
    re-used session, the original name - in the page's DOM and the browser's
    history. This resolves server-side instead.
    """
    return f"/api/ab/{session_id}/audio/{blinded}" if blinded else ""


def _session_payload(session) -> dict:
    trial = session.next_trial()
    return {
        "session_id": session.session_id,
        "target_label": session.target_label,
        "answered": session.answered_count,
        "total": len(session.trials),
        "complete": session.complete,
        # The blinded ids are all the client gets until the session is
        # scored. Sending the mapping early would let the UI leak which
        # candidate is which, which is the whole thing a blind test prevents.
        "trial": (
            {
                "trial_id": trial.trial_id,
                "left": trial.left,
                "right": trial.right,
                "left_audio": _blind_url(session.session_id, trial.left),
                "right_audio": _blind_url(session.session_id, trial.right),
            }
            if trial
            else None
        ),
    }


@router.get("/ab")
def list_ab() -> dict:
    return {"sessions": matching.list_ab_sessions()}


@router.post("/ab")
def create_ab(body: AbCreate) -> dict:
    session = matching.create_ab(
        body.session_id, body.target_label, body.candidates, pairs=body.pairs
    )
    # A clip that could not be copied to a blinded name still identifies
    # itself. The page says so rather than presenting a compromised test as
    # a clean one.
    return {**_session_payload(session), "leaks": matching.ab_leaks(session)}


@router.get("/ab/{session_id}")
def get_ab(session_id: str) -> dict:
    return _session_payload(matching.load_ab(session_id))


@router.post("/ab/{session_id}/verdict")
def answer_ab(session_id: str, body: AbVerdict) -> dict:
    session = matching.answer_ab(
        session_id, body.trial_id, body.verdict,
        confidence=body.confidence, comment=body.comment,
    )
    payload = _session_payload(session)
    if session.complete:
        payload["standings"] = [
            {"label": label, "points": points, "trials": seen}
            for label, points, seen in session.standings()
        ]
    return payload


@router.get("/ab/{session_id}/audio/{blinded}")
def ab_audio(session_id: str, blinded: str) -> FileResponse:
    """Serve one side of a trial without naming it."""
    session = matching.load_ab(session_id)
    path = session.audio_paths.get(blinded)
    if not path:
        raise NotFound(f"session '{session_id}' has no clip '{blinded}'")
    target = Path(path)
    if not target.is_file():
        raise NotFound(f"the clip for '{blinded}' is no longer on disk")
    # Named by the blinded id, so a download or a devtools panel reveals
    # nothing either.
    return FileResponse(target, filename=f"{blinded}{target.suffix}")


@router.get("/ab/{session_id}/results")
def ab_results(session_id: str) -> dict:
    session = matching.load_ab(session_id)
    return {
        "session_id": session_id,
        "complete": session.complete,
        "standings": [
            {"label": label, "points": points, "trials": seen}
            for label, points, seen in session.standings()
        ],
        "rendered": session.render_standings(),
    }


# --------------------------------------------------------------------------
# workspace, jobs, audio
# --------------------------------------------------------------------------


@router.get("/workspace")
def get_workspace() -> dict:
    return workspace.inspect().to_dict()


@router.post("/workspace/prune")
def prune_workspace(body: PruneBody) -> dict:
    return workspace.prune(
        body.areas, older_than_days=body.older_than_days, dry_run=not body.apply
    ).to_dict()


@router.get("/jobs")
def list_jobs() -> dict:
    return {"jobs": [j.to_dict() for j in get_manager().list_jobs()]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = get_manager().get(job_id)
    payload = job.to_dict(include_events=True)
    if job.result is not None:
        if hasattr(job.result, "to_dict"):
            payload["result"] = job.result.to_dict()
        elif isinstance(job.result, (dict, list, int, float, bool)):
            # A job that already returned JSON-shaped data - str() here would
            # hand the page a Python repr to parse, which it cannot.
            payload["result"] = job.result
        else:
            payload["result"] = str(job.result)
    return payload


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    return get_manager().cancel(job_id).to_dict()


# A GET with a path parameter that reaches anywhere on disk is a file-read
# primitive, and loopback is not the boundary people assume it is - any page
# in any tab can issue requests to 127.0.0.1. So the workspace is the default
# limit, and anything outside it has to be named first through a POST the
# user actually initiated (scoring, or opening a file in the waveform view).
# That keeps targets on a music drive usable without leaving the door open.
_NAMED_AUDIO: set[Path] = set()


def reset_audio_allowance() -> None:
    """Forget every externally named file. Called when an app is built."""
    _NAMED_AUDIO.clear()


def _allow_audio(*paths: str | Path) -> None:
    for path in paths:
        if path:
            _NAMED_AUDIO.add(Path(path).resolve())


def _audio_is_allowed(target: Path) -> bool:
    from ..config import get_settings

    if target in _NAMED_AUDIO:
        return True
    try:
        target.relative_to(get_settings().workspace.resolve())
    except ValueError:
        return False
    return True


@router.get("/browse")
def browse(path: str | None = None, kind: str = "audio") -> dict:
    """List a directory, so paths can be picked instead of typed.

    Every input in this tool is a path, and a browser cannot hand the page an
    absolute one - `<input type=file>` deliberately withholds it. So the
    server, which is already local and already reads these files, lists them.

    It returns names and types only, never contents: reading a file still
    goes through the audio route and its allowance. Hidden entries and
    directories that cannot be read are skipped rather than raising, because
    one unreadable folder should not make a drive unbrowsable.
    """
    from ..config import get_settings

    suffixes = {
        "audio": {".wav", ".flac", ".aiff", ".aif", ".mp3", ".ogg", ".m4a"},
        "capture": {".nam"},
        "ir": {".wav", ".aiff", ".aif"},
        "any": set(),
    }.get(kind)
    if suffixes is None:
        raise Invalid(
            f"unknown kind '{kind}'",
            hint="Use one of: audio, capture, ir, any.",
        )

    here = Path(path).expanduser() if path else get_settings().workspace
    here = here.resolve()
    if not here.is_dir():
        # A file was passed: show the folder it lives in, with it selected.
        here = here.parent if here.parent.is_dir() else Path.home()

    directories, files = [], []
    try:
        entries = sorted(here.iterdir(), key=lambda e: e.name.lower())
    except OSError as exc:
        raise NotFound(f"cannot read {here}: {exc}") from None

    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if entry.is_dir():
                directories.append({"name": entry.name, "path": str(entry)})
            elif not suffixes or entry.suffix.lower() in suffixes:
                files.append(
                    {
                        "name": entry.name,
                        "path": str(entry),
                        "size": entry.stat().st_size,
                    }
                )
        except OSError:
            continue

    return {
        "path": str(here),
        "parent": str(here.parent) if here.parent != here else None,
        "roots": _browse_roots(),
        "directories": directories,
        "files": files,
        "kind": kind,
    }


def _browse_roots() -> list[dict]:
    """Somewhere to start, each with a name short enough to read.

    Labelled rather than shown as full paths: a row of absolute paths is a
    wall of text, and the one you want is usually "workspace" or "home".
    """
    from ..config import get_settings

    roots = [
        {"name": "workspace", "path": str(get_settings().workspace)},
        {"name": "home", "path": str(Path.home())},
    ]
    if os.name == "nt":
        roots += [
            {"name": f"{letter}:", "path": f"{letter}:/"}
            for letter in "CDEFGH"
            if Path(f"{letter}:/").exists()
        ]
    else:
        roots.append({"name": "/", "path": "/"})

    seen, unique = set(), []
    for root in roots:
        if root["path"] not in seen:
            seen.add(root["path"])
            unique.append(root)
    return unique


@router.post("/match/open")
def open_audio(body: OpenAudio) -> dict:
    """Name a file for the waveform view, and report what is in it.

    The duration comes back from the header rather than from decoding, so
    pointing at an hour-long file costs nothing until you actually play it.
    """
    from ..core.matching import MIN_REGION_SECONDS

    target = Path(body.path).resolve()
    if not target.is_file():
        raise NotFound(f"no audio file at {body.path}")

    try:
        import soundfile as sf

        info = sf.info(str(target))
        duration, sample_rate, channels = (
            float(info.duration),
            int(info.samplerate),
            int(info.channels),
        )
    except ImportError:
        duration, sample_rate, channels = 0.0, 0, 0
    except Exception as exc:  # a real file that soundfile cannot read
        raise HTTPException(400, f"could not read {target.name}: {exc}") from exc

    _allow_audio(target)
    return {
        "path": str(target),
        "name": target.name,
        "url": f"/api/audio?path={quote(str(target))}",
        "duration": duration,
        "sample_rate": sample_rate,
        "channels": channels,
        "min_region": MIN_REGION_SECONDS,
    }


@router.get("/audio")
def serve_audio(path: str) -> FileResponse:
    """Serve an audio file for the player and the waveform view."""
    target = Path(path).resolve()
    if not _audio_is_allowed(target):
        raise HTTPException(
            403,
            "audio outside the rigsmith workspace has to be opened first - "
            "POST /api/match/open with the path",
        )
    if not target.is_file():
        raise NotFound(f"no audio file at {path}")
    return FileResponse(target)

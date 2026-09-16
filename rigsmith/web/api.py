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

from pathlib import Path
from typing import Any

from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..core import captures, devices, matching, recipes, rigs, workspace
from ..core.errors import NotFound, RigsmithError
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


class ResolveBody(BaseModel):
    role: str
    model_id: str


class ScoreBody(BaseModel):
    target_path: str
    candidate_paths: dict[str, str]
    mode: str = "distribution"


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
    outputs, inputs = devices.midi_ports()
    return {"outputs": outputs, "inputs": inputs}


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


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
def write_draft(draft_id: str, confirm: bool = False) -> dict:
    return rigs.write(draft_id, confirm=confirm).to_dict()


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
    scores = matching.score(body.target_path, body.candidate_paths, mode=body.mode)
    return {
        "mode": body.mode,
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


@router.get("/match/spectrum")
def match_spectrum(path: str, bands: int = 96) -> dict:
    """Average log spectrum of a file, for the comparison plot."""
    import numpy as np

    from ..match.metrics import _stft_magnitude, normalise_rms, to_mono
    from ..match.render_offline import load_audio

    audio, sample_rate = load_audio(path)
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

    return {"path": path, "freqs": centres, "db": values}


# --------------------------------------------------------------------------
# blind A/B
# --------------------------------------------------------------------------


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
                "left_audio": f"/api/audio?path={session.audio_paths.get(trial.left, '')}",
                "right_audio": f"/api/audio?path={session.audio_paths.get(trial.right, '')}",
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
    return _session_payload(session)


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
    if job.result is not None and hasattr(job.result, "to_dict"):
        payload["result"] = job.result.to_dict()
    elif job.result is not None:
        payload["result"] = str(job.result)
    return payload


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict:
    return get_manager().cancel(job_id).to_dict()


@router.get("/audio")
def serve_audio(path: str) -> FileResponse:
    """Serve an audio file from the workspace.

    Confined to the workspace: the UI is loopback-only, but a path parameter
    that reaches anywhere on disk is a bad idea regardless of who can reach it.
    """
    from ..config import get_settings

    root = get_settings().workspace.resolve()
    target = Path(path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise HTTPException(403, "audio must live inside the rigsmith workspace")
    if not target.is_file():
        raise NotFound(f"no audio file at {path}")
    return FileResponse(target)

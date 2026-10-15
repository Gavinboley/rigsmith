"""Recipe storage and the research-to-rig workflow.

Recipes live in the workspace as JSON so they outlive the conversation that
produced them. The research an agent does is the expensive part; losing it
when the session ends means doing it again.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import get_settings, require_writable
from ..tone.recipe import (
    CompileResult,
    Confidence,
    GearChoice,
    Measurement,
    SceneSpec,
    Source,
    ToneRecipe,
    compile_recipe,
)
from .errors import Conflict, Invalid, NotFound

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    return _SLUG.sub("-", name.lower()).strip("-") or "recipe"


def _dir() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    directory = settings.research_dir / "recipes"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def path_for(recipe_id: str) -> Path:
    return _dir() / f"{recipe_id}.json"


def list_recipes() -> list[tuple[str, ToneRecipe]]:
    """Every saved recipe, newest first."""
    found: list[tuple[str, ToneRecipe]] = []
    for path in sorted(_dir().glob("*.json")):
        try:
            found.append((path.stem, ToneRecipe.load(path)))
        except (Invalid, OSError):
            continue
    found.sort(key=lambda pair: pair[1].created_at, reverse=True)
    return found


def get(recipe_id: str) -> ToneRecipe:
    path = path_for(recipe_id)
    if not path.exists():
        known = ", ".join(rid for rid, _ in list_recipes()) or "none"
        raise NotFound(f"no recipe '{recipe_id}'", hint=f"Saved recipes: {known}")
    return ToneRecipe.load(path)


def save(recipe: ToneRecipe, recipe_id: str | None = None, *, overwrite: bool = True) -> str:
    """Persist a recipe. Returns its id."""
    require_writable("save a recipe")
    recipe_id = recipe_id or slugify(recipe.title)
    path = path_for(recipe_id)
    if path.exists() and not overwrite:
        raise Conflict(
            f"recipe '{recipe_id}' already exists",
            hint="Pass a different id, or allow overwriting.",
        )
    recipe.save(path)
    return recipe_id


def delete(recipe_id: str) -> str:
    require_writable("delete a recipe")
    path = path_for(recipe_id)
    if not path.exists():
        raise NotFound(f"no recipe '{recipe_id}'")
    path.unlink()
    return f"Deleted recipe '{recipe_id}'."


def create(
    title: str,
    *,
    artist: str | None = None,
    song: str | None = None,
    album: str | None = None,
    year: int | None = None,
    summary: str | None = None,
    guitar: str | None = None,
    tuning: str | None = None,
    signal_path: str = "straight",
    recipe_id: str | None = None,
) -> tuple[str, ToneRecipe]:
    """Start a recipe from what is known about the recording."""
    if not title or not title.strip():
        raise Invalid("a recipe needs a title")
    recipe = ToneRecipe(
        title=title.strip(),
        artist=artist,
        song=song,
        album=album,
        year=year,
        summary=summary,
        guitar=guitar,
        tuning=tuning,
        signal_path=signal_path,
    )
    return save(recipe, recipe_id), recipe


def add_gear(
    recipe_id: str,
    role: str,
    name: str,
    *,
    category: str = "effect",
    make: str | None = None,
    model: str | None = None,
    settings: dict | None = None,
    confidence: str = "guessed",
    source_title: str | None = None,
    source_url: str | None = None,
    source_kind: str = "web",
    source_note: str | None = None,
    alternatives: list[str] | None = None,
    note: str | None = None,
    part_of: str | None = None,
) -> ToneRecipe:
    """Record one element of the chain, with the evidence for it.

    ``confidence`` is deliberately required to be chosen rather than defaulted
    to something flattering: ``stated`` means someone who was there said so,
    and everything else is weaker than that.
    """
    try:
        level = Confidence(confidence.lower())
    except ValueError:
        options = ", ".join(c.value for c in Confidence)
        raise Invalid(
            f"unknown confidence '{confidence}'", hint=f"Options: {options}"
        ) from None

    recipe = get(recipe_id)
    sources = []
    if source_title:
        sources.append(
            Source(
                title=source_title,
                url=source_url,
                kind=source_kind,
                note=source_note,
            )
        )
    elif level in (Confidence.STATED, Confidence.REPORTED):
        raise Invalid(
            f"confidence '{level.value}' claims this is documented, but no "
            "source was given",
            hint=(
                "Pass source_title (and ideally source_url and a short quote), "
                "or lower the confidence to 'inferred' or 'guessed'."
            ),
        )

    existing = recipe.role(role)
    if existing is not None:
        recipe.gear.remove(existing)

    recipe.gear.append(
        GearChoice(
            role=role,
            name=name,
            category=category,
            make=make,
            model=model,
            settings=dict(settings or {}),
            confidence=level,
            sources=sources,
            alternatives=list(alternatives or []),
            note=note,
            part_of=part_of,
        )
    )
    save(recipe, recipe_id)
    return recipe


def resolve_gear(recipe_id: str, role: str, model_id: str) -> ToneRecipe:
    """Bind a researched gear choice to a real device model or capture."""
    recipe = get(recipe_id)
    choice = recipe.role(role)
    if choice is None:
        known = ", ".join(g.role for g in recipe.gear) or "none"
        raise NotFound(f"no gear with role '{role}'", hint=f"Roles: {known}")
    choice.resolved_model_id = model_id
    save(recipe, recipe_id)
    return recipe


def add_scene(
    recipe_id: str,
    name: str,
    *,
    roles_on: list[str] | None = None,
    roles_off: list[str] | None = None,
    footswitch: int | None = None,
    note: str | None = None,
) -> ToneRecipe:
    """Describe a part of the song in terms of which gear is active."""
    recipe = get(recipe_id)
    existing = next((s for s in recipe.scenes if s.name.lower() == name.lower()), None)
    if existing is not None:
        recipe.scenes.remove(existing)
    recipe.scenes.append(
        SceneSpec(
            name=name,
            roles_on=list(roles_on or []),
            roles_off=list(roles_off or []),
            footswitch=footswitch,
            note=note,
        )
    )
    save(recipe, recipe_id)
    return recipe


def add_question(recipe_id: str, question: str) -> ToneRecipe:
    """Record something still unknown.

    Worth its own call: a recipe that lists what it does not know is far more
    useful than one that reads as settled because the gaps went unwritten.
    """
    recipe = get(recipe_id)
    if question not in recipe.open_questions:
        recipe.open_questions.append(question)
    save(recipe, recipe_id)
    return recipe


def set_instrument(
    recipe_id: str, *, guitar: str | None = None, pickup: str | None = None, tuning: str | None = None
) -> ToneRecipe:
    """Record the guitar, pickup and tuning the part was played with.

    Not gear in the chain, but it decides what a DI should sound like: a
    Strat record chased with a humbucker DI starts every amp off wrong.
    Autopilot reads ``pickup`` (then ``guitar``) to move the part DI there.
    """
    recipe = get(recipe_id)
    if guitar is not None:
        recipe.guitar = guitar.strip() or None
    if pickup is not None:
        recipe.pickup = pickup.strip() or None
    if tuning is not None:
        recipe.tuning = tuning.strip() or None
    save(recipe, recipe_id)
    return recipe


KINDS = ("score", "listen")


def record_measurement(
    recipe_id: str,
    label: str,
    target: str,
    *,
    kind: str = "score",
    mode: str | None = None,
    composite: float | None = None,
    region: str | None = None,
    won: bool | None = None,
    trials: int | None = None,
    note: str | None = None,
) -> ToneRecipe:
    """Record what testing this recipe against the record actually produced.

    This is the half of the loop that research alone never closes. Confidence
    says how well-sourced a claim is; a measurement says whether acting on it
    worked. They are different axes, and a recipe carrying only the first has
    been reasoned about rather than checked.

    Args:
        label: The candidate tested, usually a capture or rig name.
        target: What it was compared against.
        kind: ``score`` for a metric result, ``listen`` for a blind verdict.
        won: Whether it came first - for a blind test, whether ears chose it.
    """
    if kind not in KINDS:
        raise Invalid(
            f"unknown measurement kind '{kind}'",
            hint=f"Use one of: {', '.join(KINDS)}.",
        )
    if kind == "score" and composite is None:
        raise Invalid(
            "a score measurement needs a composite value",
            hint="Pass kind='listen' to record a blind verdict instead.",
        )

    recipe = get(recipe_id)
    recipe.measurements.append(
        Measurement(
            label=label,
            target=target,
            kind=kind,
            mode=mode,
            composite=composite,
            region=region,
            won=won,
            trials=trials,
            note=note,
        )
    )
    save(recipe, recipe_id)
    return recipe


def compile_to_draft(recipe_id: str, *, name: str | None = None) -> CompileResult:
    """Build a draft rig from a recipe."""
    return compile_recipe(get(recipe_id), name=name)

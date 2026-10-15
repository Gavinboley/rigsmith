"""Tone recipe tools: record the research, then compile it into a rig.

This is where an agent's findings go. Everything it works out about a tone -
what gear, from what source, how confident - lands here rather than
evaporating into the conversation, and compiles into a draft rig on demand.
"""

from __future__ import annotations

from typing import Any, Literal

from ...core import recipes
from .._shared import tool

ConfidenceName = Literal["stated", "reported", "inferred", "guessed"]
CategoryName = Literal["amp", "cab", "ir", "clone", "nam", "effect"]


@tool
def create_recipe(
    title: str,
    artist: str | None = None,
    song: str | None = None,
    album: str | None = None,
    year: int | None = None,
    summary: str | None = None,
    guitar: str | None = None,
    tuning: str | None = None,
) -> str:
    """Start a tone recipe for a specific recording.

    A recipe is where your research goes: what gear the tone used, where each
    claim came from, and how confident it is. It compiles into a rig later,
    and survives the end of this conversation.

    Args:
        title: What to call it, usually the song.
        summary: What the tone actually sounds like, in a sentence or two -
            the thing no measurement captures. Worth writing down.
    """
    recipe_id, recipe = recipes.create(
        title,
        artist=artist,
        song=song,
        album=album,
        year=year,
        summary=summary,
        guitar=guitar,
        tuning=tuning,
    )
    return (
        f"Created recipe '{recipe_id}' for {recipe.title}.\n"
        f"Add gear with add_gear(recipe_id='{recipe_id}', ...), and record what "
        "you could not establish with add_open_question."
    )


@tool
def list_recipes() -> str:
    """List saved tone recipes, newest first.

    Recipes persist in the workspace, so research from an earlier session is
    still here. The confidence figure is the mean across gear choices - a low
    one means most of the chain is inference rather than anything documented.
    """
    found = recipes.list_recipes()
    if not found:
        return "No recipes yet. Start one with create_recipe."
    lines = [f"{len(found)} recipe(s):"]
    for recipe_id, recipe in found:
        credit = recipe.artist or ""
        lines.append(
            f"  {recipe_id:28} {recipe.title:28} {credit:24} "
            f"{len(recipe.gear)} gear, confidence {recipe.confidence:.0%}"
        )
    return "\n".join(lines)


@tool
def show_recipe(recipe_id: str) -> str:
    """Show a recipe in full: chain, sources, confidence, open questions."""
    return recipes.get(recipe_id).render()


@tool
def delete_recipe(recipe_id: str) -> str:
    """Delete a saved recipe. Any rig already compiled from it is unaffected."""
    return recipes.delete(recipe_id)


@tool
def add_gear(
    recipe_id: str,
    role: str,
    name: str,
    category: CategoryName = "effect",
    confidence: ConfidenceName = "guessed",
    make: str | None = None,
    model: str | None = None,
    settings: dict[str, Any] | None = None,
    source_title: str | None = None,
    source_url: str | None = None,
    source_kind: str = "web",
    source_note: str | None = None,
    alternatives: list[str] | None = None,
    note: str | None = None,
) -> str:
    """Record one element of the chain and the evidence for it.

    **Choose the confidence honestly.** It is the most useful field here,
    because it is what lets a reader tell your findings from your reasoning:

    - `stated`   - the player, producer, or an equipment credit says so
    - `reported` - second-hand but specific: a rig rundown, a photograph
    - `inferred` - reasoned from the era, the rest of the rig, the sound
    - `guessed`  - a plausible starting point and nothing more

    `stated` and `reported` require a source, and are refused without one.

    Args:
        role: What it does here - drive, amp, cab, delay. Scenes reference
            these, so keep them short and stable.
        name: What it is, as a person would say it.
        source_note: What the source actually says, in a sentence. A bare URL
            rots; a quote survives the link going dead.
        alternatives: Other things it might have been. Keeping the runners-up
            makes the next iteration cheap.
    """
    recipe = recipes.add_gear(
        recipe_id,
        role,
        name,
        category=category,
        make=make,
        model=model,
        settings=settings,
        confidence=confidence,
        source_title=source_title,
        source_url=source_url,
        source_kind=source_kind,
        source_note=source_note,
        alternatives=alternatives,
        note=note,
    )
    choice = recipe.role(role)
    return f"{choice.render()}\n\n{recipe.confidence_report()}"


@tool
def set_recipe_guitar(
    recipe_id: str,
    guitar: str | None = None,
    pickup: str | None = None,
    tuning: str | None = None,
) -> str:
    """Record the guitar, pickup and tuning a recording was played with.

    Write what the sources say, as specifically as they say it -
    "Fender Stratocaster, bridge single coil", "Gibson Les Paul Custom with
    EMG 81s", "drop C". Autopilot reads the pickup (or the guitar) to make the
    user's DI sound like the record's guitar before any amp is heard, so it
    matters more than it looks. Leave a field out to keep what is there.
    """
    from ...learn.pickup import named

    recipe = recipes.set_instrument(recipe_id, guitar=guitar, pickup=pickup, tuning=tuning)
    target = named(recipe.pickup or "") or named(recipe.guitar or "")
    heard = f" - a DI will be moved to {target.label} ({target.f0_hz / 1000:.1f} kHz)" if target else ""
    return f"{recipe.title}: guitar {recipe.guitar or '?'}, pickup {recipe.pickup or '?'}, tuning {recipe.tuning or '?'}{heard}"


@tool
def resolve_gear(recipe_id: str, role: str, model_id: str) -> str:
    """Bind a researched gear choice to a real device model or NAM capture.

    Until you do, compiling produces placeholder blocks named from the
    research rather than anything the device can actually load.
    """
    recipes.resolve_gear(recipe_id, role, model_id)
    return f"'{role}' now resolves to {model_id}."


@tool
def add_recipe_scene(
    recipe_id: str,
    name: str,
    roles_on: list[str] | None = None,
    roles_off: list[str] | None = None,
    footswitch: int | None = None,
    note: str | None = None,
) -> str:
    """Describe a part of the song by which gear is active.

    Scenes reference gear by role rather than slot, because slots do not
    exist until the recipe is compiled - and the chain order can still change
    before then.
    """
    recipe = recipes.add_scene(
        recipe_id,
        name,
        roles_on=roles_on,
        roles_off=roles_off,
        footswitch=footswitch,
        note=note,
    )
    scene = next(s for s in recipe.scenes if s.name == name)
    return scene.render()


@tool
def add_open_question(recipe_id: str, question: str) -> str:
    """Record something you could not establish.

    A recipe that lists its gaps is far more useful than one that reads as
    settled because the gaps went unwritten. Use this whenever the research
    ran out rather than quietly picking something.
    """
    recipe = recipes.add_question(recipe_id, question)
    return (
        f"Noted. {len(recipe.open_questions)} open question(s) on this recipe."
    )


@tool
def compile_recipe(recipe_id: str, name: str | None = None) -> str:
    """Build a draft rig from a recipe.

    Gear is placed in conventional signal-chain order - drives before the
    amp, cab after it, time effects last - regardless of the order it was
    researched in. Scenes resolve their roles to the slots the gear landed in.

    Anything not bound with resolve_gear becomes a placeholder block named
    from the research, which the device cannot load. The result says which.
    """
    return recipes.compile_to_draft(recipe_id, name=name).render()


@tool
def record_measurement(
    recipe_id: str,
    label: str,
    target: str,
    kind: Literal["score", "listen"] = "score",
    mode: str | None = None,
    composite: float | None = None,
    region: str | None = None,
    won: bool | None = None,
    trials: int | None = None,
    note: str | None = None,
) -> str:
    """Record what testing this recipe against the record actually produced.

    Call this after score_candidates or after a blind A/B finishes. It is the
    half of the loop that research alone never closes: confidence says how
    well-sourced a claim is, a measurement says whether acting on it worked,
    and a recipe carrying only the first has been reasoned about rather than
    checked.

    Args:
        label: The candidate tested - usually a capture or rig name.
        target: What it was compared against.
        kind: 'score' for a metric result, 'listen' for a blind A/B verdict.
            Only a listening verdict marks a recipe verified; a metric result
            is a ranking, not a decision.
        composite: The composite score. Required when kind is 'score'.
        region: The passage compared, if it was not the whole file.
        won: Whether it came first - for a blind test, whether ears chose it.
        trials: How many A/B trials informed a listening verdict.
    """
    recipe = recipes.record_measurement(
        recipe_id,
        label,
        target,
        kind=kind,
        mode=mode,
        composite=composite,
        region=region,
        won=won,
        trials=trials,
        note=note,
    )
    latest = recipe.measurements[-1]
    verdict = (
        "This recipe is now verified by ear."
        if recipe.verified
        else "Still unverified - only a blind listening verdict settles it."
    )
    return f"Recorded: {latest.describe()}\n\n{verdict}"

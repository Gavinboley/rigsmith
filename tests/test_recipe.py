"""Tests for tone recipes.

A recipe's job is to keep the reasoning behind a rig, and to keep findings
distinguishable from guesses. Most of what is asserted here is that second
part, because it is the one that quietly degrades: an agent that presents a
hunch in the same voice as a documented fact is the failure this whole
structure exists to prevent.
"""

from __future__ import annotations

import pytest

from rigsmith.core import recipes
from rigsmith.core.errors import Invalid, NotFound
from rigsmith.tone.recipe import (
    Confidence,
    GearChoice,
    SceneSpec,
    Source,
    ToneRecipe,
    compile_recipe,
)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    from rigsmith import config
    from rigsmith.core import drafts

    config.set_settings(config.Settings())
    drafts.set_store(drafts.DraftStore(tmp_path / "drafts"))
    yield tmp_path
    config.set_settings(None)
    drafts.set_store(None)


def full_recipe() -> ToneRecipe:
    """Deliberately out of chain order, to prove compilation reorders it."""
    recipe = ToneRecipe(title="Cherub Rock", artist="Smashing Pumpkins", year=1993)
    recipe.gear = [
        GearChoice(role="delay", name="Analog delay", settings={"mix": 0.15}),
        GearChoice(
            role="amp",
            name="Marshall JMP",
            category="nam",
            confidence=Confidence.REPORTED,
            sources=[Source(title="Interview", kind="interview")],
        ),
        GearChoice(
            role="fuzz",
            name="Big Muff",
            confidence=Confidence.STATED,
            sources=[Source(title="Liner notes", kind="liner-notes")],
        ),
        GearChoice(role="cab", name="1960A", category="ir",
                   confidence=Confidence.INFERRED),
    ]
    recipe.scenes = [
        SceneSpec(name="Clean verse", roles_off=["fuzz", "delay"], footswitch=1),
        SceneSpec(name="Big chorus", roles_on=["fuzz"], footswitch=2),
    ]
    return recipe


# --------------------------------------------------------------------------
# confidence
# --------------------------------------------------------------------------


def test_confidence_orders_from_documented_to_guessed():
    assert Confidence.STATED.weight > Confidence.REPORTED.weight
    assert Confidence.REPORTED.weight > Confidence.INFERRED.weight
    assert Confidence.INFERRED.weight > Confidence.GUESSED.weight


def test_overall_confidence_is_the_mean():
    recipe = full_recipe()
    assert 0 < recipe.confidence < 1
    assert recipe.confidence == pytest.approx(
        sum(g.confidence.weight for g in recipe.gear) / 4
    )


def test_unsourced_choices_are_identified():
    recipe = full_recipe()
    unsourced = {g.role for g in recipe.unsupported}
    assert unsourced == {"delay", "cab"}


def test_inferred_without_a_source_counts_as_unsupported():
    """Reasoning is not evidence, however good the reasoning."""
    choice = GearChoice(role="cab", name="4x12", confidence=Confidence.INFERRED)
    assert not choice.supported


def test_reported_counts_as_supported_even_without_an_attached_source():
    choice = GearChoice(role="amp", name="JMP", confidence=Confidence.REPORTED)
    assert choice.supported


def test_confidence_report_names_the_gaps():
    report = full_recipe().confidence_report()
    assert "Unsourced" in report
    assert "delay" in report


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def test_round_trips_through_json(workspace, tmp_path):
    original = full_recipe()
    original.open_questions = ["Which Big Muff revision?"]
    path = original.save(tmp_path / "r.json")

    restored = ToneRecipe.load(path)
    assert restored.title == original.title
    assert len(restored.gear) == len(original.gear)
    assert restored.gear[1].confidence is Confidence.REPORTED
    assert restored.gear[1].sources[0].title == "Interview"
    assert restored.open_questions == original.open_questions
    assert restored.scenes[0].roles_off == ["fuzz", "delay"]


def test_create_and_get(workspace):
    recipe_id, _ = recipes.create("Cherub Rock", artist="Smashing Pumpkins")
    assert recipe_id == "cherub-rock"
    assert recipes.get(recipe_id).artist == "Smashing Pumpkins"


def test_unknown_recipe_lists_the_real_ones(workspace):
    recipes.create("Real Tone")
    with pytest.raises(NotFound) as excinfo:
        recipes.get("imaginary")
    assert "real-tone" in str(excinfo.value)


def test_untitled_recipe_is_rejected(workspace):
    with pytest.raises(Invalid):
        recipes.create("   ")


def test_delete(workspace):
    recipe_id, _ = recipes.create("Doomed")
    recipes.delete(recipe_id)
    with pytest.raises(NotFound):
        recipes.get(recipe_id)


# --------------------------------------------------------------------------
# the honesty rule
# --------------------------------------------------------------------------


def test_claiming_stated_without_a_source_is_refused(workspace):
    """The whole point of the confidence field is that it can be checked."""
    recipe_id, _ = recipes.create("T")
    with pytest.raises(Invalid) as excinfo:
        recipes.add_gear(recipe_id, "amp", "JMP", confidence="stated")

    message = str(excinfo.value)
    assert "no source was given" in message
    assert "inferred" in message, "the error should offer the honest alternative"


def test_claiming_reported_without_a_source_is_refused(workspace):
    recipe_id, _ = recipes.create("T")
    with pytest.raises(Invalid):
        recipes.add_gear(recipe_id, "amp", "JMP", confidence="reported")


def test_guessed_without_a_source_is_fine(workspace):
    recipe_id, _ = recipes.create("T")
    recipe = recipes.add_gear(recipe_id, "amp", "JMP", confidence="guessed")
    assert recipe.role("amp").confidence is Confidence.GUESSED


def test_stated_with_a_source_is_accepted(workspace):
    recipe_id, _ = recipes.create("T")
    recipe = recipes.add_gear(
        recipe_id, "fuzz", "Big Muff", confidence="stated",
        source_title="Liner notes", source_note="Listed in the credits.",
    )
    assert recipe.role("fuzz").supported
    assert recipe.role("fuzz").sources[0].note == "Listed in the credits."


def test_unknown_confidence_lists_the_options(workspace):
    recipe_id, _ = recipes.create("T")
    with pytest.raises(Invalid) as excinfo:
        recipes.add_gear(recipe_id, "amp", "X", confidence="pretty-sure")
    assert "stated" in str(excinfo.value)


def test_adding_the_same_role_twice_replaces_rather_than_duplicates(workspace):
    recipe_id, _ = recipes.create("T")
    recipes.add_gear(recipe_id, "amp", "First")
    recipe = recipes.add_gear(recipe_id, "amp", "Second")
    assert len([g for g in recipe.gear if g.role == "amp"]) == 1
    assert recipe.role("amp").name == "Second"


# --------------------------------------------------------------------------
# compilation
# --------------------------------------------------------------------------


def test_compiles_into_conventional_chain_order(workspace):
    """Research arrives in whatever order it was found; a chain has an order."""
    result = compile_recipe(full_recipe())
    roles = [choice.role for _, choice in result.placed]
    assert roles == ["fuzz", "amp", "cab", "delay"]


def test_compiled_slots_are_sequential(workspace):
    result = compile_recipe(full_recipe())
    assert [slot for slot, _ in result.placed] == [1, 2, 3, 4]


def test_scenes_resolve_roles_to_the_slots_the_gear_landed_in(workspace):
    from rigsmith.core import rigs
    from rigsmith.rig.model import SceneBlockState

    result = compile_recipe(full_recipe())
    rig = rigs.get(result.draft_id).rig
    slots = {b.model_name: b.slot for b in rig.blocks}

    verse = next(s for s in rig.scenes if s.name == "Clean verse")
    assert verse.states[slots["Big Muff"]] is SceneBlockState.OFF
    assert verse.states[slots["Analog delay"]] is SceneBlockState.OFF
    assert verse.footswitch == 1


def test_a_scene_naming_missing_gear_warns(workspace):
    recipe = full_recipe()
    recipe.scenes.append(SceneSpec(name="Solo", roles_on=["wah"]))
    result = compile_recipe(recipe)
    assert any("no gear" in w for w in result.warnings)


def test_unresolved_gear_compiles_as_a_flagged_placeholder(workspace):
    result = compile_recipe(full_recipe())
    assert all(not choice.resolved_model_id for _, choice in result.placed)
    assert "placeholder" in result.render()


def test_resolved_gear_uses_the_real_model_id(workspace):
    recipe_id, _ = recipes.create("T")
    recipes.add_gear(recipe_id, "amp", "Marshall JMP", category="nam")
    recipes.resolve_gear(recipe_id, "amp", "jmp-1959-a2-lite")

    result = recipes.compile_to_draft(recipe_id)
    from rigsmith.core import rigs

    rig = rigs.get(result.draft_id).rig
    assert rig.blocks[0].model_id == "jmp-1959-a2-lite"
    assert "placeholder" not in result.render()


def test_resolving_an_unknown_role_lists_the_real_ones(workspace):
    recipe_id, _ = recipes.create("T")
    recipes.add_gear(recipe_id, "amp", "JMP")
    with pytest.raises(NotFound) as excinfo:
        recipes.resolve_gear(recipe_id, "nonexistent", "x")
    assert "amp" in str(excinfo.value)


def test_a_recipe_with_no_tone_source_warns(workspace):
    recipe = ToneRecipe(title="Pedals only")
    recipe.gear = [GearChoice(role="delay", name="Delay")]
    result = compile_recipe(recipe)
    assert any("no tone source" in w for w in result.warnings)


def test_unsourced_gear_is_warned_about_at_compile_time(workspace):
    result = compile_recipe(full_recipe())
    assert any("a guess, not a finding" in w for w in result.warnings)


def test_overfull_recipe_reports_what_did_not_fit(workspace):
    recipe = ToneRecipe(title="Too much")
    recipe.gear = [
        GearChoice(role=f"fx{i}", name=f"Pedal {i}") for i in range(20)
    ]
    result = compile_recipe(recipe)
    assert len(result.placed) == 14
    assert len(result.unplaced) == 6
    assert "rig is full" in result.unplaced[0]


def test_open_questions_survive_into_the_compiled_report(workspace):
    recipe_id, _ = recipes.create("T")
    recipes.add_gear(recipe_id, "amp", "JMP", category="nam")
    recipes.add_question(recipe_id, "Which revision?")
    assert "1 open question" in recipes.compile_to_draft(recipe_id).render()

"""Ear habits: the knobs you keep turning the same way after autopilot."""

from __future__ import annotations

import pytest

from rigsmith import config


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def finished_run(name: str, *, bench: str = "device", treble: float = 0.5, now: float | None = None):
    """A run that built an amp with ``treble``, its draft since turned to ``now``."""
    from rigsmith.core import autopilot, habits, rigs

    draft_id = rigs.create(name).draft.draft_id
    rigs.add_block(draft_id, "amp", "Some Amp", params={"Treble": treble, "Bass": 0.5})
    run = autopilot.Run(run_id=f"run-{name}", request=autopilot.Request(title=name, bench=bench), state="done", draft_id=draft_id)
    run.built_amp = habits.built("Some Amp", {"Treble": treble, "Bass": 0.5})
    run.save()
    if now is not None:
        rigs.set_param(draft_id, 1, "Treble", now)
    return run


def test_a_knob_turned_the_same_way_on_most_rigs_is_a_habit(workspace):
    from rigsmith.core import habits

    finished_run("one", treble=0.6, now=0.5)
    finished_run("two", treble=0.5, now=0.42)
    finished_run("three", treble=0.7, now=0.64)
    finished_run("four", treble=0.5)
    found = habits.habits("device")
    assert [h.control for h in found] == ["Treble"], "Bass was never turned"
    treble = found[0]
    assert (treble.rigs, treble.of) == (3, 4) and treble.move == pytest.approx(-0.08)
    assert treble.describe() == "Treble down on 3 of 4 rigs, typically -8%"
    assert habits.habits("virtual") == [], "a simulated amp's knobs are not the Core's"


def test_two_rigs_or_a_split_are_not_a_habit(workspace):
    from rigsmith.core import habits

    finished_run("a", treble=0.6, now=0.5)
    finished_run("b", treble=0.6, now=0.5)
    assert habits.habits("device") == []
    finished_run("c", treble=0.4, now=0.5)
    finished_run("d", treble=0.4, now=0.5)
    assert habits.habits("device") == [], "two down and two up says nothing"


def test_an_undone_turn_does_not_count(workspace):
    from rigsmith.core import habits, rigs

    for name in ("x", "y", "z"):
        finished_run(name, treble=0.6, now=0.5)
    assert habits.habits("device")
    rigs.undo("z")
    rigs.undo("y")
    assert habits.habits("device") == [], "what the drafts hold now is what counts"


def test_a_run_can_start_from_them_as_one_undoable_edit(workspace):
    from rigsmith.core import autopilot, habits, rigs

    for name in ("p", "q", "r"):
        finished_run(name, treble=0.6, now=0.5)
    moved, said = habits.apply({"Treble": 0.05}, "Some Amp", habits.habits("device"))
    assert moved["Treble"] == 0.0 and said == ["Treble 5% -> 0%"], "kept inside the knob's travel"

    run = finished_run("new", treble=0.6)
    autopilot._start_from_habits(run, "Some Amp")
    assert rigs.get(run.draft_id).rig.blocks[0].params["Treble"] == 0.5
    assert any("started from your ear habits (Treble 60% -> 50%)" in n for n in run.notes)
    rigs.undo(run.draft_id)
    assert rigs.get(run.draft_id).rig.blocks[0].params["Treble"] == 0.6, "one undo back to what measured closest"


def test_the_page_is_offered_the_habits_for_its_bench(workspace):
    from fastapi.testclient import TestClient

    from rigsmith.web.server import build_app

    for name in ("e", "f", "g"):
        finished_run(name, treble=0.4, now=0.5)
    found = TestClient(build_app()).get("/api/autopilot/habits", params={"bench": "device"}).json()
    assert found["rigs"] == 3 and found["habits"][0]["describe"] == "Treble up on 3 of 3 rigs, typically +10%"

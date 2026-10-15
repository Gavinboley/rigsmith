"""Autopilot, start to finish, on the simulated bench.

A synthetic song whose rhythm guitar was made with a known amp voicing goes
in; out should come a mapped song, a part DI on a learned guitar, a ranking
of real Core amp models, tuned settings, a draft rig and a listening test -
with every stage's decision recorded, and nothing written to a device.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

from rigsmith import config  # noqa: E402
from rigsmith.core import autopilot, profiles, rigs  # noqa: E402
from rigsmith.core.errors import Invalid  # noqa: E402
from rigsmith.match.render_offline import save_audio  # noqa: E402
from rigsmith.tune import candidates  # noqa: E402

sys.path.insert(0, str(Path(__file__).parent))
from test_song import song, with_drums  # noqa: E402

RATE = 48000


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    monkeypatch.setenv("RIGSMITH_MODE", "local")  # no network in tests
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def test_the_measured_gain_picks_a_sensible_field_of_amps():
    clean = candidates.candidates(0.05, count=4)
    modern = candidates.candidates(0.9, count=4)
    assert clean[0].tier == "clean" and "Treadplate" not in " ".join(c.model for c in clean)
    assert modern[0].tier == "modern"
    # A step either side gets a look-in, both directions.
    middle = {c.tier for c in candidates.candidates(0.7, count=5)}
    assert {"crunch", "high", "modern"} <= middle


def test_research_names_go_first():
    found = candidates.candidates(0.7, researched=["64 Fox AC30"], count=3)
    assert found[0].model == "64 Fox AC30" and found[0].reason == "named by the research"


def test_the_sketch_adds_the_blocks_it_implies():
    blocks = candidates.surrounding_blocks({"gain": 0.8, "gated": True, "delay_ms": 375.0, "delay_note": "dotted eighth", "space": "a room"})
    roles = [b["role"] for b in blocks]
    assert roles == ["gate", "boost", "delay", "reverb"]


def test_a_whole_run_ends_in_a_draft_and_a_listening_test(workspace):
    stem, _ = song(seconds_per_section=10.0)
    mix_path = save_audio(workspace / "mix.wav", with_drums(stem), RATE)
    stem_path = save_audio(workspace / "stem.wav", stem, RATE)
    take, _ = profiles.demo_take("quick")
    profiles.create("demo")
    profiles.learn("demo", str(take), protocol_id="quick", fit_voice=False)

    request = autopilot.Request(
        title="Fixture", artist="Synthetic", mix=str(mix_path), stem=str(stem_path),
        profile_id="demo", research=False, candidates=4, tune_budget=8, like="strat",
    )
    run = autopilot.Run(run_id="t1", request=request)
    run.save()
    run = autopilot.execute("t1")

    assert run.state == "done", run.describe()
    assert run.stages["research"].state == "skipped"
    # The best amp was heard through other cabs, each pinned to its own sim cab.
    assert run.stages["cabs"].state == "done", run.stages["cabs"].summary
    cabbed = [r for r in run.results if " + " in r["label"]]
    assert cabbed and all(r["cab_voicing"] is not None for r in run.results)
    assert run.mode == "performance"
    assert run.results and run.results[0]["composite"] <= run.results[-1]["composite"]
    assert any("(tuned)" in r["label"] for r in run.results)
    assert run.draft_id and rigs.get(run.draft_id).rig.blocks
    assert run.ab_session
    assert any("rehearsal" in n for n in run.all_notes())
    # What is still different, band by band, with something to try.
    assert {r["band"] for r in run.remaining} >= {"low end", "mids", "presence"}
    assert all(r["try"] for r in run.remaining)
    assert any("pickup was moved toward strat" in n for n in run.all_notes()), run.all_notes()
    # Research was off: the gear measured from the record is not "research".
    assert not any("named by the research" in str(r.get("reason")) for r in run.results), run.results

    # Re-running the last stage replaces the draft, it does not add another.
    before = len(rigs.list_drafts())
    rerun = autopilot.resume("t1", from_stage="rig", background=False)
    assert rerun.state == "done"
    assert len(rigs.list_drafts()) == before

    # Ears decide: a blind test that prefers the score's runner-up moves the draft.
    from rigsmith.core import matching, recipes

    with pytest.raises(Invalid):
        autopilot.listen_back("t1")  # nothing answered: the names stay hidden
    session = matching.load_ab(rerun.ab_session)
    favourite = rerun.results[1]["label"]
    favoured = next(blind for blind, label in session.blinded.items() if label == favourite)
    for trial in session.trials:
        verdict = "left" if trial.left == favoured else "right" if trial.right == favoured else "tie"
        matching.answer_ab(rerun.ab_session, trial.trial_id, verdict)
    after = autopilot.listen_back("t1")
    assert after.ear_pick == favourite
    picked_model = rerun.results[1]["model"]
    assert any(b.model_id == picked_model for b in rigs.get(after.draft_id).rig.blocks)
    assert any("your ears picked" in n for n in after.notes)
    heard = [m for m in recipes.get(after.recipe_id).measurements if m.kind == "listen"]
    assert heard and any(m.won and m.label == favourite for m in heard)


def test_a_song_with_no_audio_stops_at_the_first_stage(workspace):
    run = autopilot.Run(run_id="t2", request=autopilot.Request(title="Nothing"))
    run.save()
    run = autopilot.execute("t2")
    assert run.state == "failed"
    assert run.stages["song"].state == "failed"
    assert run.stages["map"].state == "waiting"


def test_an_unknown_bench_is_refused(workspace):
    with pytest.raises(Invalid):
        autopilot.start(autopilot.Request(title="x", bench="cloud"))


def test_the_researched_guitar_is_what_the_di_is_moved_to(workspace):
    from rigsmith.core import recipes

    recipe_id, _ = recipes.create("Fixture")
    recipes.set_instrument(recipe_id, guitar="Fender Stratocaster", tuning="E standard")
    run = autopilot.Run(run_id="t3", request=autopilot.Request(title="Fixture"), recipe_id=recipe_id)
    assert autopilot._researched_guitar(run) == "Fender Stratocaster"
    recipes.set_instrument(recipe_id, pickup="bridge P-90")
    assert autopilot._researched_guitar(run) == "bridge P-90"


def test_each_model_is_switched_by_its_own_selector():
    from rigsmith.tune.candidates import Candidate, cab_options, with_cab

    revalver = Candidate("Angel P-Ball", "modern", "4x12 Classic 30W", "named by the research", device_index=7, selector="ReValver Amp.Type", cab_index=10, cab_selector="Cab.CabType")
    assert revalver.device_values() == {"ReValver_Amp": {"Type": 7}, "Cab": {"CabType": 10}}
    plain = Candidate("82 Lead 800 100W", "high", "4x12 Green 20W", "x", device_index=23)
    assert plain.device_values() == {"Amp": {"Type": 23}}
    through = with_cab(plain, "4x12 Classic 30W", why="V30s", index=10, selector="Cab.CabType")
    assert through.cab_voicing == 2 and through.device_values()["Cab"] == {"CabType": 10}
    options = [name for name, *_ in cab_options("high", researched=["2x12 B30"])]
    assert options[0] == "2x12 B30" and "4x12 Green 20W" in options


def test_the_device_bench_switches_amp_and_cab_through_the_loop(workspace):
    from rigsmith.core import loop

    stem, _ = song(seconds_per_section=6.0)
    mix_path = save_audio(workspace / "mix.wav", with_drums(stem), RATE)
    stem_path = save_audio(workspace / "stem.wav", stem, RATE)
    routes = loop.demo_bench()
    analog = next(r for r in routes if r.kind == "analog")
    request = autopilot.Request(
        title="Fixture", mix=str(mix_path), stem=str(stem_path), research=False,
        bench="device", route_id=analog.route_id, candidates=3, tune_budget=6, match_guitar=False,
    )
    run = autopilot.Run(run_id="t4", request=request)
    run.save()
    run = autopilot.execute("t4")
    assert run.state == "done", run.describe()
    assert run.stages["cabs"].state in ("done", "skipped")
    assert run.results[0]["composite"] <= run.results[-1]["composite"]


def test_a_fitted_eq_goes_in_only_when_it_scores_closer(workspace):
    from rigsmith.loop.virtual import SimAmp
    from rigsmith.match.metrics import ComparisonMode
    from rigsmith.match.score import score_candidate
    from rigsmith.tune import eq as match_eq

    stem, _ = song(seconds_per_section=6.0)
    candidate = SimAmp(Type=2, Gain=0.6).process(stem[: RATE * 6, 0])
    target = match_eq.apply_graphic(candidate, RATE, [0, 3, 0, -5, 0, 6, 0, -4])
    path = save_audio(workspace / "tuned.wav", candidate, RATE)
    plan = match_eq.match(target, candidate, RATE).to_dict()
    composite = score_candidate(target, candidate, RATE, mode=ComparisonMode.PERFORMANCE).composite
    entry = {"label": "Amp (tuned)", "composite": composite, "path": str(path), "eq": plan, "untuned": composite}

    (equalised,) = autopilot._with_eq([entry], target, RATE, ComparisonMode.PERFORMANCE)
    assert equalised["label"] == "Amp (tuned + G EQ)" and equalised["composite"] < composite * 0.98
    assert set(equalised["geq"]) == set(match_eq.GEQ_NAMES) and all(0 <= v <= 1 for v in equalised["geq"].values())
    # Nothing to fix: no EQ entry at all.
    own = score_candidate(candidate, candidate, RATE, mode=ComparisonMode.PERFORMANCE).composite
    flat = {**entry, "composite": own, "eq": match_eq.match(candidate, candidate, RATE).to_dict()}
    assert autopilot._with_eq([flat], candidate, RATE, ComparisonMode.PERFORMANCE) == []


def test_the_device_bench_checks_the_loaded_rig_first(workspace, monkeypatch):
    from rigsmith.core import loop, tuning
    from rigsmith.loop.routes import LoopRoute

    table = ["(empty)", "Amp", "Amp 2", "Cab", "Cab 2", "ReValver Amp", "Delay"]
    check = tuning.assess_chain({"ModuleType1": 2, "ModuleType2": 3, "ModuleType3": 6}, table)
    assert check.modules == ["Amp 2", "Cab", "Delay"] and not check.ok  # Amp 2 does not hear "Amp"
    assert "no amp" in check.describe()
    assert tuning.assess_chain({"ModuleType1": 1, "ModuleType4": 3}, table).ok

    loop.store().put(LoopRoute("real", output_device="X", input_device="X", backend="sounddevice"), make_default=True)
    run = autopilot.Run(run_id="t6", request=autopilot.Request(title="x", bench="device", route_id="real"))
    monkeypatch.setattr(tuning, "bench_check", lambda: check)
    with pytest.raises(Invalid) as caught:
        autopilot._device_hosts(run)
    assert "bench-rig" in caught.value.hint
    monkeypatch.setattr(tuning, "bench_check", lambda: tuning.assess_chain({"ModuleType1": 1, "ModuleType2": 3}, table))
    assert autopilot._device_hosts(run) == {"Amp", "Cab"}

    draft_id = tuning.bench_rig_draft()
    assert tuning.bench_rig_draft() == draft_id  # made once
    assert [b.model_id for b in rigs.get(draft_id).rig.blocks] == [tuning.BENCH_AMP, tuning.BENCH_CAB]


def test_what_the_ears_chose_before_is_heard_first(workspace):
    from rigsmith.core import recipes

    recipe_id, _ = recipes.create("Fixture")
    recipes.record_measurement(recipe_id, "69 Plexiglas 100W + 4x12 Green 20W (tuned)", "autopilot x", kind="listen", won=True, trials=3)
    recipes.record_measurement(recipe_id, "92 Treadplate Modern", "autopilot x", kind="listen", won=False, trials=3)
    run = autopilot.Run(run_id="t7", request=autopilot.Request(title="Fixture"), recipe_id=recipe_id)
    assert autopilot._ear_winners(run) == ["69 Plexiglas 100W"]
    field_ = candidates.candidates(0.9, favoured=["69 Plexiglas 100W"], researched=["64 Fox AC30"], count=4)
    assert [c.model for c in field_[:2]] == ["69 Plexiglas 100W", "64 Fox AC30"]
    assert field_[0].reason == "your ears chose it last time" and field_[0].tier == "crunch"
    assert field_[1].tier == "edge"  # the research's amp keeps its own tier's cab and voicing


def test_several_chosen_parts_become_one_rig_with_a_scene_each(workspace):
    from rigsmith.core import songs

    stem, _ = song(seconds_per_section=6.0)
    mix_path = save_audio(workspace / "mix.wav", with_drums(stem), RATE)
    stem_path = save_audio(workspace / "stem.wav", stem, RATE)
    made = songs.create("Fixture", mix=str(mix_path), stem=str(stem_path))
    mapped = songs.analyse(made.song_id)
    by_gain = sorted((p for p in mapped.parts if p.layer != "stem" and p.end - p.start >= 4), key=lambda p: (p.sketch or {}).get("gain", 0))
    chosen = [by_gain[0].part_id, by_gain[-1].part_id]  # the cleanest and the most driven

    request = autopilot.Request(title="Fixture", song_id=made.song_id, parts=chosen, research=False, candidates=3, tune_budget=6)
    run = autopilot.Run(run_id="t8", request=request)
    run.save()
    run = autopilot.execute("t8")

    assert run.state == "done", run.describe()
    assert run.part_runs == ["t8-2"] and autopilot.get("t8-2").part_ids == [chosen[1]]
    rig = run.song_rig
    assert [s["footswitch"] for s in rig["scenes"]] == [1, 2]
    assert [s["part"] for s in rig["scenes"]] == chosen
    draft = rigs.get(rig["draft_id"]).rig
    amps = [b for b in draft.blocks if b.category.value == "amp"]
    assert 1 <= len(amps) <= 2
    first, second = (draft.scene_named(s["scene"]) for s in rig["scenes"])
    if len(amps) == 2:  # two sounds: each scene turns its own on and the other off
        assert {first.state_for(a.slot).value for a in amps} == {"on", "off"}
        assert first.state_for(amps[0].slot) != second.state_for(amps[0].slot)
    assert "song rig" in run.describe()

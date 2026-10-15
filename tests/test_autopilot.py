"""Autopilot, start to finish, on the simulated bench.

A synthetic song whose rhythm guitar was made with a known amp voicing goes
in; out should come a mapped song, a part DI on a learned guitar, a ranking
of real Core amp models, tuned settings, a draft rig and a listening test -
with every stage's decision recorded, and nothing written to a device.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
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
    # The measured numbers are set: a dotted eighth is the Core's 3/16, and
    # the milliseconds are there for when sync is off.
    delay = blocks[2]["params"]
    assert delay["Sync"] is True and candidates.DELAY_DIVISIONS[delay["DelayTime_Synced"]] == "3/16"
    assert abs(delay["DelayTime"] - (375 - 20) / 780) < 1e-3 and 0.2 <= delay["Mix"] <= 0.45
    hall = candidates.surrounding_blocks({"space": "a plate or hall", "modulation": "tremolo", "modulation_hz": 5.0})
    assert hall[0]["params"]["Rate"] == round((5.0 - 0.1) / 11.9, 4)
    assert hall[1]["params"]["Mode"] == 1  # Hall


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


def test_a_title_is_enough_when_the_song_is_in_your_library(workspace):
    from rigsmith.core import appsettings

    stem, _ = song(seconds_per_section=6.0)
    album = workspace / "Music" / "Synthetic Band" / "Fixtures"
    album.mkdir(parents=True)
    save_audio(album / "01 Fixture Song.wav", with_drums(stem), RATE)
    appsettings.update(music_dirs=[str(workspace / "Music")])

    request = autopilot.Request(title="Fixture Song", artist="Synthetic Band", research=False, candidates=3, tune_budget=6)
    run = autopilot.Run(run_id="t9", request=request)
    run.save()
    run = autopilot.execute("t9")
    assert run.stages["song"].state == "done", run.describe()
    assert any("your library's 01 Fixture Song.wav" in n for n in run.all_notes())
    assert run.state == "done"


def test_your_own_take_of_the_part_is_scored_frame_by_frame(workspace):
    import librosa

    from rigsmith.core import songs
    from rigsmith.match.render_offline import load_audio

    stem, _ = song(seconds_per_section=6.0)
    mix_path = save_audio(workspace / "mix.wav", with_drums(stem), RATE)
    stem_path = save_audio(workspace / "stem.wav", stem, RATE)
    made = songs.create("Fixture", mix=str(mix_path), stem=str(stem_path))
    mapped = songs.analyse(made.song_id)
    part = max((p for p in mapped.parts if p.layer == "left"), key=lambda p: p.end - p.start)
    target, _ = load_audio(songs.part_audio(made.song_id, part.part_id))
    # Played along, a little slow and late - as a take is.
    take = np.concatenate([np.zeros(int(0.3 * RATE)), librosa.effects.time_stretch(target, rate=1 / 1.02)])
    take_path = save_audio(workspace / "my-take.wav", take, RATE)

    request = autopilot.Request(title="Fixture", song_id=made.song_id, parts=[part.part_id], di=str(take_path), research=False, candidates=3, tune_budget=6)
    run = autopilot.Run(run_id="t10", request=request)
    run.save()
    run = autopilot.execute("t10")
    assert run.state == "done", run.describe()
    assert run.mode == "performance" and "on the record's clock" in run.stages["di"].summary


def test_a_tremolo_on_the_beat_is_synced():
    # 6 Hz at 90 BPM is a cycle every quarter of a beat: the Core's 1/16.
    tremolo = candidates.surrounding_blocks({"modulation": "tremolo", "modulation_hz": 6.0}, tempo=90.0)[0]
    assert tremolo["division"] == "1/16" and tremolo["params"]["Sync"] is True
    assert candidates.RATE_DIVISIONS[tremolo["params"]["Rate_Synced"]] == "1/16"
    assert candidates.RATE_DIVISIONS[0] == "Bar", "the modulation list runs the other way from the delay's"
    # 5.3 Hz at 90 BPM falls between divisions, so it keeps its own rate.
    loose = candidates.surrounding_blocks({"modulation": "tremolo", "modulation_hz": 5.3}, tempo=90.0)[0]
    assert "Sync" not in loose["params"] and loose["division"] is None
    assert candidates.synced_rate(6.0, None) is None


def test_synced_blocks_give_the_draft_the_songs_tempo(workspace):
    from rigsmith.core import rigs

    draft_id = rigs.create("tempo").draft.draft_id
    notes: list[str] = []
    autopilot._carry_tempo(None, draft_id, [{"role": "delay", "division": None}], 99.0, notes)
    assert rigs.get(draft_id).rig.tempo is None and not notes, "nothing synced, nothing to carry"
    autopilot._carry_tempo(None, draft_id, [{"role": "delay", "division": "3/16"}], 128.437, notes)
    assert rigs.get(draft_id).rig.tempo == 128.44 and "Fixed" in notes[0] and "delay on 3/16" in notes[0]
    notes.clear()
    other = rigs.create("fast").draft.draft_id
    autopilot._carry_tempo(None, other, [{"role": "tremolo", "division": "1/8"}], 300.0, notes)
    assert rigs.get(other).rig.tempo is None and "tap it in" in notes[0]
    notes.clear()
    autopilot._carry_tempo(None, other, [{"role": "delay", "division": "1/8"}], 70.0, notes, structure={"tempo_confidence": 0.2})
    assert "was a guess" in notes[0]
    notes.clear()
    autopilot._carry_tempo(None, other, [{"role": "delay", "division": "1/8"}], 70.0, notes, structure={"tempo_confidence": 0.2, "tempo_source": "hand"})
    assert "guess" not in notes[0], "a tempo set by hand is not a guess"
    with pytest.raises(Exception):
        rigs.set_tempo(other, 20.0)


def test_a_drafts_tone_sheet_says_where_it_came_from(workspace):
    from rigsmith.core import sheets

    draft_id = rigs.create("Fixture - verse").draft.draft_id
    run = autopilot.Run(run_id="sheet-run", request=autopilot.Request(title="Fixture", artist="Synthetic"))
    run.draft_id = draft_id
    run.state = "done"
    run.results = [
        {"label": "92 Treadplate Modern", "composite": 0.21, "model": "92 Treadplate Modern"},
        {"label": "82 Lead 800 100W", "composite": 0.25, "model": "82 Lead 800 100W"},
    ]
    run.ear_pick = "92 Treadplate Modern"
    run.remaining = [{"band": "presence", "low": 2000, "high": 5000, "delta_db": -3.2, "audible": True, "meaning": "duller than the record", "try": "Presence up"}]
    run.notes = ["the rig carries the song's tempo"]
    run.save()

    text = sheets.tone_sheet(draft_id).markdown()
    assert "**Song:** Fixture - Synthetic" in text
    assert "by your ears in the blind test" in text and "82 Lead 800 100W (0.250)" in text
    assert "presence -3.2 dB - duller than the record; try Presence up" in text
    assert "the rig carries the song's tempo" in text
    assert sheets.origin(rigs.create("Hand made").draft.draft_id) == {}, "a draft nobody's run built has no history"

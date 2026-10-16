"""Tuning knobs by listening.

The simulated amp makes these exact: a target is rendered at known settings,
and the search has to find its way back to them from somewhere else, through
a different performance of the same notes.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

from rigsmith import config  # noqa: E402
from rigsmith.core import loop, tuning  # noqa: E402
from rigsmith.core.errors import Invalid  # noqa: E402
from rigsmith.learn.pluck import render_notes  # noqa: E402
from rigsmith.loop import virtual  # noqa: E402
from rigsmith.loop.virtual import SimAmp, _biquad, _filter  # noqa: E402
from rigsmith.match.metrics import ComparisonMode  # noqa: E402
from rigsmith.match.render_offline import save_audio  # noqa: E402
from rigsmith.match.score import score_candidate  # noqa: E402
from rigsmith.tune import eq  # noqa: E402
from rigsmith.tune.optimize import optimise  # noqa: E402
from rigsmith.tune.space import Space, virtual_amp  # noqa: E402

RATE = 48000
TRUTH = {"GainA": 0.72, "Bass": 0.35, "Mid": 0.25, "Treble": 0.7, "Presence": 0.6}


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def human(notes, seed):
    rng = np.random.default_rng(seed)
    return [(t + rng.normal(0, 0.008), d, m, float(np.clip(v + rng.normal(0, 0.06), 0.1, 1))) for t, d, m, v in notes]


NOTES = [(0.2 + i * 0.25, 0.22, [40, 43, 45, 47][i % 4], 0.8) for i in range(24)]


def target_and_di():
    amp = SimAmp(Type=3)
    amp.set(**TRUTH)
    target = amp.process(render_notes(human(NOTES, 1), seed=1))
    di = render_notes(human(NOTES, 2), seed=9)
    return target, di


def test_the_search_finds_its_way_back_to_the_settings_that_made_the_target():
    target, di = target_and_di()

    def evaluate(values):
        amp = SimAmp(Type=3)
        amp.set(**{k.split(".")[1]: v for k, v in values.items()})
        return score_candidate(target, amp.process(di), RATE, mode=ComparisonMode.PERFORMANCE).composite, {}

    result = optimise(virtual_amp(), evaluate, budget=40)
    found = {k.split(".")[1]: v for k, v in result.best.values.items()}
    assert result.improvement > 0.3
    assert abs(found["GainA"] - TRUTH["GainA"]) < 0.15
    assert abs(found["Treble"] - TRUTH["Treble"]) < 0.2


def test_the_budget_is_a_ceiling_and_every_try_is_kept():
    calls = []

    def evaluate(values):
        calls.append(values)
        return sum((v - 0.3) ** 2 for v in values.values()), {}

    result = optimise(virtual_amp(), evaluate, budget=12)
    assert len(result.trials) == len(calls) <= 12
    assert result.stopped in ("budget spent", "converged")


def test_a_failed_render_is_a_data_point_not_the_end():
    count = {"n": 0}

    def evaluate(values):
        count["n"] += 1
        if count["n"] == 3:
            raise RuntimeError("the cable fell out")
        return sum(values.values()), {}

    result = optimise(virtual_amp(), evaluate, budget=10)
    assert any(t.info.get("error") for t in result.trials)
    assert result.best is not None


def test_an_empty_space_is_refused():
    with pytest.raises(Invalid):
        optimise(Space([]), lambda v: (0.0, {}), budget=5)


def test_match_eq_recovers_a_known_difference():
    _, di = target_and_di()
    candidate = SimAmp(Type=3, Gain=0.7).process(di)
    target = _filter(_filter(candidate, *_biquad("peak", 800, RATE, q=1.0, gain_db=-6)), *_biquad("peak", 3000, RATE, q=1.2, gain_db=5))
    plan = eq.match(target, candidate, RATE)
    assert plan.graphic_db["Gain800Hz"] == pytest.approx(-6, abs=1.5)
    assert plan.graphic_db["Gain3_2kHz"] > 3
    assert plan.after_db < plan.before_db / 3
    assert plan.slot["SltEQF"] == pytest.approx(800, rel=0.25)


def test_a_tuning_run_is_kept_and_listable(workspace):
    target, di = target_and_di()
    t = save_audio(workspace / "t.wav", target, RATE)
    d = save_audio(workspace / "d.wav", di, RATE)
    run = tuning.tune(str(t), str(d), renderer="virtual", mode="performance", budget=10)
    assert run.best_audio and run.eq
    assert tuning.get_run(run.run_id).run_id == run.run_id
    assert run.run_id in [r.run_id for r in tuning.list_runs()]


def test_tuning_through_the_simulated_loop_moves_the_simulated_core(workspace):
    target, di = target_and_di()
    t = save_audio(workspace / "t.wav", target, RATE)
    d = save_audio(workspace / "d.wav", di, RATE)
    loop.demo_bench()
    virtual.world().rig.amp.set(Type=3)
    run = tuning.tune(str(t), str(d), renderer="loop", route_id="virtual-analog", mode="performance", budget=8)
    assert run.result["best"] is not None
    # The bench's amp was actually turned: it no longer sits at its defaults.
    amp = virtual.world().rig.amp
    assert (amp.Gain, amp.Bass, amp.Treble) != (0.5, 0.5, 0.5)


def test_live_tuning_refuses_knobs_on_blocks_the_loaded_rig_lacks(tmp_path, monkeypatch):
    from rigsmith.loop.routes import LoopRoute

    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    try:
        loop.store().put(LoopRoute("real", output_device="X", input_device="X", backend="sounddevice"), make_default=True)
        monkeypatch.setattr(tuning, "bench_check", lambda: tuning.assess_chain({"ModuleType1": 2}, ["", "Amp", "Cab"]))
        clip = save_audio(tmp_path / "clip.wav", np.zeros(48000), 48000)
        with pytest.raises(Invalid) as caught:
            tuning.tune(str(clip), str(clip), renderer="loop", route_id="real", knobs=["Amp.GainA"], budget=4)
        assert "no Amp block" in str(caught.value)
    finally:
        config.set_settings(None)


def test_the_search_starts_where_the_untuned_sound_says_to_turn():
    from rigsmith.match.metrics import band_differences
    from rigsmith.tune.eq import informed_start

    di = render_notes([(0.2 + i * 0.25, 0.22, 40 + (i * 3) % 12, 0.8) for i in range(32)], seed=2)
    target = SimAmp(Type=2, Gain=0.6, Bass=0.15, Treble=0.85).process(di)
    space = virtual_amp(("GainA", "Bass", "Mid", "Treble", "Presence"))
    untuned = SimAmp(Type=2, Gain=0.5).process(di)
    start = informed_start(space, band_differences(target, untuned, 48000))
    assert start["Amp.Bass"] < 0.4 and start["Amp.Treble"] > 0.6
    assert start["Amp.GainA"] == 0.5  # no band rule for gain: left where it was
    assert all(abs(start[k] - 0.5) <= 0.3 + 1e-9 for k in start)


def test_the_core_is_given_time_to_settle_before_it_is_heard(monkeypatch):
    import time as time_module

    from rigsmith.device import client as client_module

    sent, slept = [], []

    class Fake:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def call(self, operation, **kwargs):
            sent.append(kwargs["node"])

    async def connect(*args, **kwargs):
        return Fake()

    monkeypatch.setattr(client_module, "connect", connect)
    monkeypatch.setattr(time_module, "sleep", slept.append)
    sink = tuning.DeviceSink()
    sink.apply({"Amp": {"Type": 23}, "Cab": {"CabType": 12}})
    sink.apply({"Amp": {"Bass": 0.4}})
    assert sent == ["/Evil/Engine/Patch/Amp", "/Evil/Engine/Patch/Cab", "/Evil/Engine/Patch/Amp"]
    assert slept == [tuning.MODEL_SETTLE_S, tuning.KNOB_SETTLE_S]


def test_settings_as_people_write_them_become_knob_positions():
    from rigsmith.tune.settings import amp_controls, position

    assert position(7) == 0.7 and position("7/10") == 0.7 and position("65%") == 0.65 and position(80) == 0.8
    assert position("noon") == 0.5 and position("dimed") == 1.0 and position("off") == 0.0
    assert position("2 o'clock") == 0.7 and position("7 o'clock") == 0.0 and position("5 oclock") == 1.0
    assert position("a bit") is None and position(-3) is None
    controls = amp_controls({"Gain": "7", "Mids": "noon", "treble": "2 o'clock", "reverb": 3, "bass": "lots"})
    assert controls == {"GainA": 0.7, "Mid": 0.5, "Treble": 0.7}, "unknown names and unreadable values are left out"


def test_a_silent_candidate_gets_a_flat_eq_not_a_nan_one():
    target, _ = target_and_di()
    plan = eq.match(target, np.zeros_like(target), RATE)
    assert all(np.isfinite(v) and v == 0.0 for v in plan.graphic_db.values())
    assert np.isfinite(plan.before_db) and np.isfinite(plan.after_db)


def test_words_become_knob_moves():
    from rigsmith.tune import nudge

    asked = nudge.parse("a bit brighter, a lot tighter and purple")
    assert asked.moves == {"Treble": 0.04, "Presence": 0.04, "Bass": -0.16}
    assert asked.unknown == ["purple"]
    assert nudge.parse("less gain").moves == {"GainA": -0.08}, "the longer direction wins over its tail"
    assert nudge.apply({"Treble": 0.98}, {"Treble": 0.08}) == {"Treble": (0.98, 1.0)}, "kept inside the knob's travel"


def test_a_complaint_is_read_as_its_cure():
    from rigsmith.tune import nudge

    asked = nudge.parse("a bit too bright, way too much bass and not enough gain")
    assert asked.moves == {"Treble": -0.04, "Presence": -0.04, "Bass": -0.16, "GainA": 0.08}
    assert asked.understood == ["a bit too bright", "way too much bass", "not enough gain"], "said back as it was said"
    assert nudge.parse("too dark").moves == {"Treble": 0.08, "Presence": 0.08}, "not read as 'darker'"
    assert nudge.parse("too muddy").moves == {"Bass": -0.08, "Mid": -0.04}
    assert nudge.parse("too purple").unknown == ["too purple"]


def test_a_draft_is_nudged_on_its_amp(workspace):
    from rigsmith.core import rigs

    draft_id = rigs.create("Nudge").draft.draft_id
    rigs.add_block(draft_id, "effect", "Green JRC-OD")
    rigs.add_block(draft_id, "amp", "Some Amp", params={"Treble": 0.6})
    result = rigs.nudge(draft_id, "a lot brighter")
    amp = next(b for b in result.draft.rig.blocks if b.category.value == "amp")
    assert amp.params["Treble"] == 0.76 and amp.params["Presence"] == 0.66, "an unset control starts from noon"
    assert "Treble 60% -> 76%" in result.summary
    with pytest.raises(Invalid):
        rigs.nudge(draft_id, "purple")
    empty = rigs.create("No amp").draft.draft_id
    with pytest.raises(Invalid):
        rigs.nudge(empty, "brighter")

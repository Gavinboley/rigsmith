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

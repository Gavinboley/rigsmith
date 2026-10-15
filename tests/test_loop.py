"""The hardware loop, exercised on the simulated bench.

The bench has the faults real loops have - a different latency every stream,
two clocks drifting apart, an inverted return, a send far hotter than a
guitar - so every test here is a test that the loop undoes one of them.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

from rigsmith import config  # noqa: E402
from rigsmith.core import loop, matching, takes  # noqa: E402
from rigsmith.core.errors import NotFound  # noqa: E402
from rigsmith.learn.pluck import render_notes  # noqa: E402
from rigsmith.loop import virtual  # noqa: E402
from rigsmith.loop.accuracy import assess  # noqa: E402
from rigsmith.loop.backend import VirtualBackend  # noqa: E402
from rigsmith.loop.calibrate import calibrate, level_statistic  # noqa: E402
from rigsmith.loop.routes import LoopRoute, suggest  # noqa: E402
from rigsmith.loop.run import reamp  # noqa: E402
from rigsmith.loop.signals import gcc_phat, sync_mark  # noqa: E402

RATE = 48000


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def di(count=12):
    return render_notes([(0.2 + i * 0.25, 0.22, [40, 43, 45, 47][i % 4], 0.8) for i in range(count)], seed=1)


def bench(**wiring):
    world = virtual.VirtualWorld(wiring=virtual.Wiring(seed=5, **wiring))
    return world, VirtualBackend(world)


def routes(backend):
    return {r.kind: r for r in suggest(backend.devices())}


def expected(world, route, signal):
    gain = world.rig.usb_in_level_db if route.kind == "usb-reamp" else world.wiring.cable_gain_db
    return world.rig.process(signal * 10 ** (gain / 20))


@pytest.mark.parametrize("kind", ["analog", "usb-reamp", "hybrid"])
@pytest.mark.parametrize("voicing", [0, 3])
def test_every_route_brings_the_rig_back_sample_aligned(kind, voicing):
    world, backend = bench(polarity=-1)
    world.rig.amp.set(Type=voicing, Gain=0.7)
    route = routes(backend)[kind]
    signal = di()
    capture = reamp(route, signal, backend=backend)
    assert capture.aligned
    assert capture.polarity == -1
    result = assess(expected(world, route, signal)[: len(signal)], capture.wet[: len(signal)], RATE)
    assert result.esr < 0.01, result.render()


def test_two_clocks_are_measured_and_undone():
    world, backend = bench(core_drift_ppm=60.0)
    capture = reamp(routes(backend)["hybrid"], di(), backend=backend)
    assert capture.drift_ppm == pytest.approx(60.0, abs=1.5)


def test_a_reamp_on_the_bypass_channel_is_caught_twice():
    """Channels 1-2 to the Core skip the rig: the route says so, and so does the audio."""
    world, backend = bench()
    world.rig.amp.set(Type=3, Gain=0.9)
    good = routes(backend)["usb-reamp"]
    wrong = LoopRoute(**{**good.__dict__, "route_id": "oops", "output_channels": [1]})
    assert any("channel 3" in problem for problem in wrong.check())

    signal = di()
    capture = reamp(wrong, signal, backend=backend)
    result = assess(expected(world, good, signal)[: len(signal)], capture.wet[: len(signal)], RATE)
    assert result.esr > 0.3


def test_a_knob_that_moved_is_not_indistinguishable():
    world, backend = bench()
    route = routes(backend)["analog"]
    signal = di()
    before = reamp(route, signal, backend=backend).wet
    world.rig.amp.set(Treble=0.85)
    after = reamp(route, signal, backend=backend).wet
    assert assess(before, after, RATE).esr > 0.05


def test_the_sync_mark_survives_heavy_distortion():
    mark = sync_mark(RATE)
    padded = np.concatenate([np.zeros(777), mark, np.zeros(2000)])
    crushed = np.tanh(padded * 400)
    delay, peak, sharpness = gcc_phat(crushed, mark, max_lag=5000)
    assert abs(delay - 777) < 1.0
    assert sharpness > 8


def test_calibration_takes_the_hot_send_back_out():
    world, backend = bench()
    world.rig.amp.set(Type=0, Gain=0.2)
    route = routes(backend)["hybrid"]
    signal = di(20)
    # The guitar played straight in arrives at its own level - which is the
    # DI's level, on the bench.
    result = calibrate(route, backend=backend, di=signal, direct=signal, direct_is_reference=True)
    assert result.level_method == "reference"
    assert result.send_gain_db == pytest.approx(-world.wiring.cable_gain_db, abs=0.5)


def test_level_statistic_ignores_one_thump():
    signal = di(20)
    thumped = signal.copy()
    thumped[RATE] = 0.99
    assert level_statistic(thumped, RATE) == pytest.approx(level_statistic(signal, RATE), abs=0.5)


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


def test_the_demo_bench_is_a_working_default(workspace):
    made = loop.demo_bench()
    assert {r.kind for r in made} == {"analog", "usb-reamp", "hybrid"}
    assert loop.store().default_id() == "virtual-analog"
    assert loop.test_tone()["connected"]


def test_a_reamp_is_filed_as_a_simulated_take(workspace):
    from rigsmith.match.render_offline import save_audio

    loop.demo_bench()
    path = save_audio(workspace / "di.wav", di(), RATE)
    result = loop.reamp(str(path), label="pass one", file_as_take=True)
    assert result.simulated
    take = takes.get(result.take_id)
    assert take.kind == "wet" and take.origin == "loop-return"
    assert "simulated" in take.tags
    # And it says the send was never calibrated.
    assert any("calibrated" in note for note in result.notes)


def test_match_reamp_goes_through_the_loop_once_there_is_one(workspace):
    from rigsmith.match.render_offline import save_audio

    loop.demo_bench()
    path = save_audio(workspace / "di.wav", di(), RATE)
    rendered = matching.reamp(str(path), label="via-loop")
    assert rendered.layer == "hardware"
    assert rendered.stages == ["device rig via virtual-analog"]
    assert any("simulated" in w for w in rendered.warnings)


def test_recording_an_input_files_a_di(workspace):
    take = loop.record_input(1.0, device="Virtual HeadRush Core", channel=5, backend="virtual", take_id="quiet", guitar="LP")
    assert take.kind == "di" and take.guitar == "LP"
    again = loop.record_input(1.0, device="Virtual HeadRush Core", channel=5, backend="virtual", take_id="quiet")
    assert again.take_id == "quiet-2"


def test_no_route_says_how_to_make_one(workspace):
    with pytest.raises(NotFound) as caught:
        loop.get_route()
    assert "loop suggest" in caught.value.hint


def test_a_box_windows_splits_in_two_is_still_one_suggestion():
    from rigsmith.loop.backend import DeviceInfo
    from rigsmith.loop.routes import hardware

    listed = []
    for api in ("MME", "Windows WASAPI", "Windows WDM-KS"):
        for name, ins, outs in (("Analog 1/2", 2, 0), ("Loopback 1/2", 2, 0), ("Playback 1/2", 0, 2), ("Playback 3/4", 0, 2)):
            label = f"{name} ({name})" if api == "Windows WDM-KS" else f"{name} (E2x2)"
            listed.append(DeviceInfo(label, len(listed), ins, outs, 44100.0, api))
    listed.insert(0, DeviceInfo("Microsoft Sound Mapper - Input", 99, 2, 0, 44100.0, "MME"))

    (route,) = suggest(listed)
    assert route.kind == "analog"
    assert (route.output_device, route.input_device) == ("Playback 1/2 (E2x2)", "Analog 1/2 (E2x2)")
    assert route.hostapi == "Windows WASAPI" and route.sample_rate == 44100
    assert route.same_clock and not any("drift" in p for p in route.check())

    (box,) = hardware(listed)
    assert box["name"] == "E2x2" and box["inputs"] == ["Analog 1/2", "Loopback 1/2"]
    assert box["hostapis"][0] == "Windows WASAPI"

    # A Core that Windows splits the same way gets its USB reamp route.
    listed += [DeviceInfo("Input 1-6 (HeadRush Core)", 200, 6, 0, 48000.0, "Windows WASAPI"), DeviceInfo("Output 1-4 (HeadRush Core)", 201, 0, 4, 48000.0, "Windows WASAPI")]
    kinds = {r.kind: r for r in suggest(listed)}
    assert kinds["usb-reamp"].output_device == "Output 1-4 (HeadRush Core)"
    assert "same rate" in kinds["hybrid"].note

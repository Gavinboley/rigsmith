"""Tests for the audio render pipeline and the rig codec.

These modules had no coverage at all, which is uncomfortable given that
`render_offline` decides which capture gets recommended and `codec` is what
persists every draft. Most of it needs no hardware and no heavy dependencies
- IR convolution, resampling, and serialisation are all testable directly.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from rigsmith.match.render_offline import convolve_ir, render_chain, resample
from rigsmith.rig.codec import (
    FORMAT_VERSION,
    load_rig,
    rig_from_dict,
    rig_to_dict,
    save_rig,
)
from rigsmith.rig.model import (
    Block,
    BlockCategory,
    Doubling,
    EqBand,
    Rig,
    Scene,
    SceneBlockState,
    SignalPath,
)

SR = 48000


def impulse(length: int = 512) -> np.ndarray:
    """A unit impulse - convolving with it must change nothing."""
    ir = np.zeros(length)
    ir[0] = 1.0
    return ir


def tone(seconds: float = 0.25, freq: float = 220.0) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return np.sin(2 * np.pi * freq * t)


# --------------------------------------------------------------------------
# IR convolution
# --------------------------------------------------------------------------


def test_unit_impulse_is_a_no_op():
    signal = tone()
    assert np.allclose(convolve_ir(signal, impulse()), signal, atol=1e-9)


def test_output_length_matches_the_input_by_default():
    """A cab block does not lengthen the signal it processes."""
    signal = tone()
    assert len(convolve_ir(signal, np.ones(256) / 256)) == len(signal)


def test_full_convolution_keeps_the_tail_when_asked():
    signal = tone(0.05)
    ir = np.ones(256) / 256
    wet = convolve_ir(signal, ir, trim_to_input=False)
    assert len(wet) == len(signal) + len(ir) - 1


def test_truncation_shortens_the_impulse():
    """Mirrors the device's 1024-sample mode, which costs less DSP."""
    signal = tone(0.05)
    long_ir = np.random.default_rng(0).standard_normal(4096) * 0.01

    full = convolve_ir(signal, long_ir)
    short = convolve_ir(signal, long_ir, truncate_samples=1024)

    assert len(full) == len(short) == len(signal)
    assert not np.allclose(full, short), "truncation should change the result"


def test_truncation_fades_rather_than_cutting():
    """A hard cut puts a step in the impulse, which rings as a click."""
    ir = np.ones(4096)
    truncated_signal = convolve_ir(np.zeros(10), ir, truncate_samples=1024)
    assert truncated_signal.shape == (10,)

    # The faded tail means the truncated IR does not end at full amplitude.
    from rigsmith.match.render_offline import convolve_ir as convolve

    probe = np.zeros(2048)
    probe[0] = 1.0
    tail = convolve(probe, ir, truncate_samples=1024, trim_to_input=False)[:1024]
    assert tail[-1] < tail[0], "the end of the truncated IR should be faded"


def test_a_short_ir_is_accepted():
    assert len(convolve_ir(tone(0.05), np.array([1.0, 0.5]))) == len(tone(0.05))


# --------------------------------------------------------------------------
# resampling
# --------------------------------------------------------------------------


def test_resampling_to_the_same_rate_is_a_no_op():
    signal = tone()
    assert resample(signal, SR, SR) is signal


def test_resampling_changes_length_proportionally():
    signal = tone(1.0)  # one second
    assert len(resample(signal, 48000, 44100)) == pytest.approx(44100, rel=0.01)
    assert len(resample(signal, 48000, 96000)) == pytest.approx(96000, rel=0.01)


def test_resampling_preserves_the_signal_roughly():
    """Down and back should land near where it started."""
    signal = tone(0.2, freq=220.0)
    round_trip = resample(resample(signal, 48000, 24000), 24000, 48000)
    length = min(len(signal), len(round_trip))
    # Ignore edges, where any resampler rings.
    a, b = signal[1000:length - 1000], round_trip[1000:length - 1000]
    assert np.corrcoef(a, b)[0, 1] > 0.99


# --------------------------------------------------------------------------
# render_chain
# --------------------------------------------------------------------------


def test_render_with_nothing_to_do_says_so():
    result = render_chain(tone(0.05), SR)
    assert any("nothing was rendered" in w for w in result.warnings)
    assert not result.exact


def test_render_applies_an_ir(tmp_path):
    pytest.importorskip("soundfile")
    import soundfile as sf

    ir_path = tmp_path / "cab.wav"
    sf.write(ir_path, np.ones(256, dtype=np.float32) / 256, SR)

    result = render_chain(tone(0.1), SR, ir_path=ir_path)
    assert result.stages, "the IR stage should have run"
    assert "IR(cab)" in result.stages[0]
    assert result.exact


def test_render_reports_a_missing_nam_rather_than_pretending(tmp_path):
    """A skipped stage must be visible, not silently passed through."""
    result = render_chain(tone(0.05), SR, nam_path=tmp_path / "absent.nam")
    assert result.warnings
    assert any("NAM" in w for w in result.warnings)
    assert not result.exact


def test_render_applies_output_gain():
    quiet = render_chain(tone(0.05), SR, ir_path=None, output_gain_db=0, normalise=False)
    loud = render_chain(tone(0.05), SR, ir_path=None, output_gain_db=6, normalise=False)
    assert np.max(np.abs(loud.audio)) > np.max(np.abs(quiet.audio))


def test_render_normalises_by_default():
    from rigsmith.match.metrics import normalise_rms

    loud = tone(0.1) * 10
    result = render_chain(loud, SR)
    assert np.allclose(result.audio, normalise_rms(loud))


# --------------------------------------------------------------------------
# codec
# --------------------------------------------------------------------------


def rich_rig() -> Rig:
    """A rig using every field the codec is supposed to carry."""
    rig = Rig(
        name="Everything",
        signal_path=SignalPath.MIDDLE_SPLIT_3_4_3,
        rig_width=60,
        setlist="Live set",
        slot_number=12,
        notes="the kitchen sink",
        source_recipe="cherub-rock",
    )
    rig.add_block(
        Block(
            slot=1,
            category=BlockCategory.EFFECT,
            model_id="big-muff",
            model_name="Big Muff Pi",
            params={"drive": 0.72, "tone": 0.4},
            input_gain_db=-2.0,
            output_gain_db=1.5,
            eq=[
                EqBand(frequency=100.0, gain_db=-3.0, q=0.7, mode="shelf"),
                EqBand(frequency=3000.0, gain_db=2.0, q=1.4),
            ],
            led_color="red",
        )
    )
    rig.add_block(
        Block(
            slot=5,
            category=BlockCategory.NAM,
            model_id="jmp-1959",
            params={"size": "A2 Lite"},
            doubling=Doubling.STEREO,
            doubled_model_id="jcm800",
            lane=1,
        )
    )
    rig.scenes.append(
        Scene(
            name="Verse",
            states={1: SceneBlockState.OFF, 5: SceneBlockState.ON},
            presets={1: "soft"},
            footswitch=2,
            led_color="blue",
        )
    )
    return rig


def test_round_trip_preserves_every_field():
    original = rich_rig()
    restored = rig_from_dict(rig_to_dict(original))

    assert restored.name == original.name
    assert restored.signal_path is original.signal_path
    assert restored.rig_width == 60
    assert restored.setlist == "Live set"
    assert restored.slot_number == 12
    assert restored.notes == original.notes
    assert restored.source_recipe == "cherub-rock"


def test_round_trip_preserves_block_detail():
    restored = rig_from_dict(rig_to_dict(rich_rig()))
    block = restored.block_at(1)

    assert block.model_name == "Big Muff Pi"
    assert block.params == {"drive": 0.72, "tone": 0.4}
    assert block.input_gain_db == -2.0
    assert block.output_gain_db == 1.5
    assert block.led_color == "red"
    assert len(block.eq) == 2
    assert block.eq[0].frequency == 100.0
    assert block.eq[0].mode == "shelf"
    assert block.eq[1].q == 1.4


def test_round_trip_preserves_doubling_and_lane():
    restored = rig_from_dict(rig_to_dict(rich_rig()))
    block = restored.block_at(5)
    assert block.doubling is Doubling.STEREO
    assert block.doubled_model_id == "jcm800"
    assert block.lane == 1


def test_round_trip_preserves_scenes_including_presets():
    restored = rig_from_dict(rig_to_dict(rich_rig()))
    scene = restored.scenes[0]
    assert scene.states[1] is SceneBlockState.OFF
    assert scene.states[5] is SceneBlockState.ON
    assert scene.presets[1] == "soft"
    assert scene.footswitch == 2
    assert scene.led_color == "blue"


def test_slot_gaps_survive_a_round_trip():
    """Blocks at 1 and 5 must not be compacted to 1 and 2."""
    restored = rig_from_dict(rig_to_dict(rich_rig()))
    assert sorted(b.slot for b in restored.blocks) == [1, 5]


def test_scene_slot_keys_survive_json_string_conversion():
    """JSON object keys are strings; scene slots are ints."""
    data = json.loads(json.dumps(rig_to_dict(rich_rig())))
    restored = rig_from_dict(data)
    assert all(isinstance(slot, int) for slot in restored.scenes[0].states)


def test_file_round_trip(tmp_path):
    path = save_rig(rich_rig(), tmp_path / "nested" / "rig.json")
    assert path.exists()
    assert load_rig(path).block_at(1).model_name == "Big Muff Pi"


def test_a_newer_format_version_is_refused():
    """Better to fail loudly than to write a misread rig to hardware."""
    data = rig_to_dict(rich_rig())
    data["format_version"] = FORMAT_VERSION + 1
    with pytest.raises(ValueError, match="Upgrade rigsmith"):
        rig_from_dict(data)


def test_defaults_are_omitted_from_the_written_form():
    """Keeps a hand-edited rig file readable."""
    plain = Rig(name="Plain")
    plain.add_block(
        Block(slot=1, category=BlockCategory.EFFECT, model_id="x")
    )
    written = rig_to_dict(plain)["blocks"][0]
    assert "eq" not in written
    assert "doubling" not in written
    assert "input_gain_db" not in written


def test_rendered_audio_is_not_clipped_on_the_way_to_disk(tmp_path):
    """soundfile's WAV default is PCM_16, which clips past +/-1.0.

    Every candidate this tool measures has been through save_audio, and
    clipping adds precisely the harmonic content the metrics look for - so a
    candidate that merely rendered hot would read as brighter and more
    distorted than it is, and the artefact would look like a tone difference.
    """
    pytest.importorskip("soundfile")
    import soundfile as sf

    from rigsmith.match.render_offline import load_audio, save_audio

    hot = np.array([0.5, 1.8, -1.8, 0.0], dtype=np.float32)
    path = save_audio(tmp_path / "hot.wav", hot, 48000)

    assert sf.info(str(path)).subtype == "FLOAT"
    restored, rate = load_audio(path)
    assert rate == 48000
    assert restored == pytest.approx(hot, abs=1e-6)


def test_a_caller_can_still_ask_for_sixteen_bit(tmp_path):
    """For anything that leaves this tool and has to be widely readable."""
    pytest.importorskip("soundfile")
    import soundfile as sf

    from rigsmith.match.render_offline import save_audio

    path = save_audio(
        tmp_path / "share.wav", np.zeros(8, dtype=np.float32), 48000, subtype="PCM_16"
    )
    assert sf.info(str(path)).subtype == "PCM_16"

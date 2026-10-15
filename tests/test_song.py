"""Mapping a song into the parts worth chasing.

The fixtures are synthetic songs whose structure is known exactly: where the
sections change, which guitar is doubled, which part has a delay on it. Real
records are messier; these tests pin the behaviour that has to survive the
mess - a riff is not an echo, a double track is not two parts, a silent
channel is not a gate.
"""

from __future__ import annotations

import functools

import numpy as np
import pytest

pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

from rigsmith import config  # noqa: E402
from rigsmith.core import appsettings, songs  # noqa: E402
from rigsmith.core.errors import NotFound, Unavailable  # noqa: E402
from rigsmith.learn.pluck import render_notes  # noqa: E402
from rigsmith.loop.virtual import SimAmp  # noqa: E402
from rigsmith.song import describe, layers, structure  # noqa: E402

RATE = 48000


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    monkeypatch.delenv("RIGSMITH_MODE", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def human(notes, seed=0):
    rng = np.random.default_rng(seed)
    return [(t + rng.normal(0, 0.008), d, m, float(np.clip(v + rng.normal(0, 0.06), 0.1, 1))) for t, d, m, v in notes]


@functools.lru_cache(maxsize=4)
def _song(seconds_per_section, seed):
    return _build_song(seconds_per_section, seed)


def song(seconds_per_section=16.0, seed=0):
    """A copy of the cached synthetic song - it takes seconds to render."""
    stem, truth = _song(seconds_per_section, seed)
    return stem.copy(), list(truth)


def _build_song(seconds_per_section=16.0, seed=0):
    """Clean intro, riff verse, chord chorus, lead solo; rhythm double-tracked."""
    rng = np.random.default_rng(seed)
    beat = 0.5
    plan = [("intro", "clean"), ("verse", "riff"), ("chorus", "chords"), ("solo", "lead"), ("chorus", "chords")]
    left, right, lead, clean, truth = [], [], [], [], []
    t = 0.0
    beats = int(seconds_per_section / beat)
    for name, kind in plan:
        truth.append((name, t))
        for b in range(beats):
            when = t + b * beat
            if kind == "clean":
                clean.append((when, 0.45, [52, 55, 59, 64][b % 4], 0.5))
            elif kind == "riff":
                for k in range(2):
                    m = [40, 40, 43, 45][b % 4]
                    left.append((when + k * beat / 2, beat / 2 * 0.9, m, 0.85))
                    right.append((when + k * beat / 2 + rng.normal(0, 0.012) + 0.01, beat / 2 * 0.9, m + rng.normal(0, 0.06), 0.85))
            elif kind == "chords":
                root = [40, 48, 43, 50][(b // 4) % 4]
                for m in (root, root + 7, root + 12):
                    left.append((when, beat * 0.95, m, 0.8))
                    right.append((when + rng.normal(0, 0.012) + 0.01, beat * 0.95, m + rng.normal(0, 0.06), 0.8))
            else:
                lead.append((when, beat * 0.9, [64, 67, 69, 71, 74, 71, 69, 67][b % 8], 0.9))
                root = [40, 48, 43, 50][(b // 4) % 4]
                left.append((when, beat * 0.95, root, 0.6))
                right.append((when + rng.normal(0, 0.012) + 0.01, beat * 0.95, root, 0.6))
        t += beats * beat
    total = int((t + 1) * RATE)

    def play(notes, amp, s):
        audio = render_notes(notes, seed=s)
        return amp.process(np.pad(audio, (0, max(0, total - len(audio))))[:total])

    rhythm = SimAmp(Type=3, Gain=0.75)
    L = play(left, rhythm, seed + 1)
    R = play(right, rhythm, seed + 2)
    C = play(lead, SimAmp(Type=2, Gain=0.6, Mid=0.7), seed + 3)
    # The intro is its own guitar through a clean amp, centred and quieter.
    K = play(clean, SimAmp(Type=0, Gain=0.1, Level=0.3), seed + 4) if clean else 0
    stem = np.stack([L + 0.7 * C + K, R + 0.7 * C + K], axis=1)
    peak = float(np.abs(stem).max())
    return stem / (peak * 1.1), truth


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def with_drums(stem):
    """A kick on every beat - structure is mapped from the mix, not the stem."""
    beat = int(0.5 * RATE)
    kick = np.sin(2 * np.pi * 60 * np.arange(400) / RATE) * np.exp(-np.arange(400) / 120) * 0.4
    drums = np.zeros(len(stem))
    for start in range(0, len(stem) - 400, beat):
        drums[start : start + 400] += kick
    return stem + drums[:, None]


def test_tempo_and_most_section_changes_are_found():
    stem, truth = song()
    result = structure.analyse(with_drums(stem), RATE)
    assert result.tempo == pytest.approx(120, abs=2)
    found = [s.start for s in result.sections]
    boundaries = [start for _, start in truth[1:]]
    hits = sum(any(abs(f - b) < 3.0 for f in found) for b in boundaries)
    # On a full mix of a real song the changes are clearer than this - drums
    # and vocals move with the sections. Guitar and a kick alone is the hard
    # case; half of them found is the floor this has to stay above.
    assert hits >= len(boundaries) // 2


def test_a_quiet_intro_is_named_an_intro_not_a_verse():
    stem, _ = song()
    result = structure.analyse(with_drums(stem), RATE)
    assert result.sections[0].name == "intro"


# --------------------------------------------------------------------------
# layers
# --------------------------------------------------------------------------


def test_a_double_tracked_rhythm_is_one_part_played_twice():
    stem, _ = song()
    result = layers.analyse(stem, RATE)
    named = {l.layer_id: l for l in result.layers}
    assert named["left"].partner == "right"
    assert named["right"].partner == "left"
    assert "double-tracked" in result.summary


def test_different_parts_left_and_right_are_not_called_a_double():
    a = SimAmp(Type=3, Gain=0.7).process(render_notes(human([(i * 0.5, 0.45, [40, 43][i % 2], 0.8) for i in range(40)]), seed=1))
    b = SimAmp(Type=2, Gain=0.5).process(render_notes(human([(0.25 + i * 0.75, 0.7, [64, 67, 71][i % 3], 0.8) for i in range(26)], 1), seed=2))
    n = min(len(a), len(b))
    result = layers.analyse(np.stack([a[:n], b[:n]], axis=1), RATE)
    named = {l.layer_id: l for l in result.layers}
    assert named["left"].partner is None
    assert result.summary.startswith("different parts")


def test_a_mono_file_in_stereo_clothing_is_one_layer():
    a = SimAmp(Type=1).process(render_notes(human([(i * 0.5, 0.45, 40, 0.8) for i in range(20)]), seed=1))
    result = layers.analyse(np.stack([a, a], axis=1), RATE)
    assert not result.stereo and len(result.layers) == 1


# --------------------------------------------------------------------------
# describing a part
# --------------------------------------------------------------------------


def riff_di(seed=1):
    return render_notes(human([(0.2 + i * 0.25, 0.22, [40, 43, 45, 47][i % 4], 0.8) for i in range(48)], seed), seed=seed)


def echo(audio, ms, feedback=0.4, mix=0.35):
    d = int(ms * RATE / 1000)
    audio = np.pad(audio, (0, 6 * d))
    out = audio.copy()
    for k in range(1, 6):
        shifted = np.zeros_like(audio)
        shifted[d * k :] = audio[: -d * k]
        out += mix * feedback ** (k - 1) * shifted
    return out


def test_gain_reads_in_order_from_clean_to_saturated():
    di = riff_di()
    readings = [describe.sketch(SimAmp(Type=t, Gain=g).process(di), RATE).gain for t, g in ((0, 0.1), (1, 0.5), (3, 0.8))]
    assert readings == sorted(readings)
    assert describe.sketch(SimAmp(Type=0, Gain=0.1).process(di), RATE).gain_label == "clean"


def test_a_steady_riff_is_not_an_echo():
    assert describe.sketch(SimAmp(Type=1, Gain=0.5).process(riff_di()), RATE, tempo=120).delay_ms is None


def test_a_dotted_eighth_is_found_and_named():
    lead = SimAmp(Type=2, Gain=0.6).process(render_notes(human([(0.2 + i * 0.5, 0.45, [64, 67, 69, 71][i % 4], 0.8) for i in range(24)], 4), seed=2))
    result = describe.sketch(echo(lead, 375), RATE, tempo=120)
    assert result.delay_ms == pytest.approx(375, abs=3)
    assert result.delay_note == "dotted eighth"


def test_a_mid_scoop_is_seen():
    result = describe.sketch(SimAmp(Type=3, Gain=0.8, Mid=0.1).process(riff_di()), RATE)
    assert result.mid_shape == "scooped mids"


# --------------------------------------------------------------------------
# modes
# --------------------------------------------------------------------------


def test_local_mode_refuses_the_internet(workspace):
    appsettings.update(mode="local")
    with pytest.raises(Unavailable) as caught:
        songs.lookup("going under")
    assert "local mode" in str(caught.value)


def test_the_mode_can_be_forced_from_the_environment(workspace, monkeypatch):
    appsettings.update(mode="accuracy")
    monkeypatch.setenv("RIGSMITH_MODE", "local")
    assert appsettings.load().local


def test_tone3000_asks_before_going_online(workspace):
    import asyncio

    from rigsmith.tone.tone3000 import Tone3000Client

    appsettings.update(mode="local")
    with pytest.raises(Unavailable):
        asyncio.run(Tone3000Client().search("jcm800"))


# --------------------------------------------------------------------------
# the service
# --------------------------------------------------------------------------


def test_a_song_maps_into_parts_and_remembers_choices(workspace):
    from rigsmith.match.render_offline import save_audio

    stem, _ = song(seconds_per_section=12.0)
    path = save_audio(workspace / "stem.wav", stem, RATE)
    made = songs.create("Fixture", artist="Synthetic", stem=str(path))
    mapped = songs.analyse(made.song_id)
    assert mapped.parts and mapped.structure["tempo"] > 0
    assert set(mapped.layer_files) >= {"left", "right"}

    first = mapped.parts[0].part_id
    songs.choose(made.song_id, [first])
    remapped = songs.analyse(made.song_id)
    assert remapped.part(first).chosen

    target = songs.part_audio(made.song_id, first)
    assert target.is_file()
    assert "Research the gear" in songs.brief(made.song_id)

    part = songs.record_result(made.song_id, first, label="a", composite=0.3, mode="distribution")
    part = songs.record_result(made.song_id, first, label="b", composite=0.2, mode="distribution")
    assert part.best["label"] == "b"


def test_an_unknown_part_lists_the_real_ones(workspace):
    from rigsmith.match.render_offline import save_audio

    stem, _ = song(seconds_per_section=8.0)
    path = save_audio(workspace / "stem.wav", stem, RATE)
    made = songs.analyse(songs.create("Fixture", stem=str(path)).song_id)
    with pytest.raises(NotFound) as caught:
        made.part("nope")
    assert made.parts[0].part_id in caught.value.hint

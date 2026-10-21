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


def test_a_section_starts_where_the_music_changes():
    """The 2 s smoothing that keeps a fill from reading as a section smeared
    the change: the demo song's clean intro ended up to 0.8 s late, its part
    holding the start of the driven verse."""
    from rigsmith.core import demo

    mix, _, sections = demo.render(seconds_per_section=12.0)
    found = [s.start for s in structure.analyse(mix, demo.RATE).sections[1:]]
    for change in (s["start"] for s in sections[1:]):
        assert min(abs(f - change) for f in found) <= 0.3, (change, found)


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
    # The same music twice in a row is one section, not a chorus cut in two
    # wherever its chord cycle happened to light the curve up.
    assert all(a.label != b.label for a, b in zip(result.sections, result.sections[1:]))
    assert all(a.end == b.start for a, b in zip(result.sections, result.sections[1:])), "no gaps"


@pytest.mark.parametrize("letters, names", [
    ("ABCDC", ["intro", "verse", "chorus", "solo", "chorus"]),
    ("ABECDC", ["intro", "verse", "pre-chorus", "chorus", "solo", "chorus"]),
    ("ABCBCDC", ["intro", "verse", "chorus", "verse", "chorus", "solo", "chorus"]),
])
def test_music_heard_once_on_the_way_to_the_first_chorus_is_a_verse(letters, names):
    """A bridge comes after a chorus: the demo song's verse, heard once
    between its intro and its first chorus, was called one."""
    loud = {"A": -30.0, "B": -21.0, "E": -20.0, "C": -18.0, "D": -18.5}
    sections = [structure.Section(start=12.0 * i, end=12.0 * (i + 1), label=letter, energy_db=loud[letter]) for i, letter in enumerate(letters)]
    structure._name(sections)
    assert [s.name for s in sections] == names


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


def test_a_lead_between_a_double_track_is_split_from_it(tmp_path):
    """The centre is what both sides share: two takes of one riff share
    nothing, so a lead between them comes out with less of them in it - and
    each side with less of the lead. The pan-and-coherence mask it replaced
    left the left take further from itself (1.9 dB) than the raw channel."""
    from rigsmith.match.render_offline import load_audio

    def clarity(estimate, truth):
        scale = np.dot(estimate, truth) / np.dot(truth, truth)
        return 10 * np.log10(np.sum((scale * truth) ** 2) / np.sum((estimate - scale * truth) ** 2))

    riff = [(i * 0.25, 0.22, [40, 40, 43, 45][i % 4], 0.85) for i in range(48)]
    left = SimAmp(Type=3, Gain=0.75).process(render_notes(human(riff, 1), seed=1))
    right = SimAmp(Type=3, Gain=0.75).process(render_notes(human(riff, 2), seed=2))
    melody = [(i * 0.5, 0.45, [64, 67, 69, 71, 74, 71, 69, 67][i % 8], 0.9) for i in range(24)]
    lead = 0.7 * SimAmp(Type=2, Gain=0.6).process(render_notes(human(melody, 3), seed=3))
    n = min(len(left), len(right), len(lead))
    left, right, lead = left[:n], right[:n], lead[:n]
    result = layers.analyse(np.stack([left + lead, right + lead], axis=1), RATE, extract_to=tmp_path)
    got = {layer.layer_id: load_audio(layer.path)[0][:n] for layer in result.layers}
    assert {"centre", "left", "right"} <= set(got) and "double-tracked" in result.summary
    assert clarity(got["centre"], lead) > 1.5  # the plain mid: -1.0 dB; the old mask 0.7
    assert clarity(got["left"], left) > 5.0 > clarity(left + lead, left)  # the old mask 1.9


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


def sustained(seed=1):
    """Clean notes long enough to hear a chorus move them."""
    rng = np.random.default_rng(seed)
    return render_notes([(1.6 * i, 1.5, int(m), 0.8) for i, m in enumerate(rng.integers(45, 70, size=10))], seed=seed)


def modulated_delay(audio, rate_hz, depth_ms, base_ms, mix):
    t = np.arange(len(audio)) / RATE
    delay = (base_ms + depth_ms * np.sin(2 * np.pi * rate_hz * t)) / 1000 * RATE
    wet = np.interp(np.arange(len(audio)) - delay, np.arange(len(audio)), audio, left=0, right=0)
    return (1 - mix) * audio + mix * wet


def test_a_chorus_is_heard_in_the_pitch_and_a_dry_part_is_not():
    clean = SimAmp(Type=0, Gain=0.1)
    dry = clean.process(sustained())
    assert describe.sketch(dry, RATE, from_stem=False).modulation == ""
    chorused = describe.sketch(clean.process(modulated_delay(sustained(), 1.5, 3.0, 12.0, 0.45)), RATE, from_stem=False)
    assert chorused.modulation == "chorus", chorused.describe()
    # Its sweeping notch dips the held notes; that is not a room.
    assert chorused.space not in ("a plate or hall", "a long wash"), chorused.describe()
    assert 1.2 < chorused.modulation_hz < 1.8 and chorused.wobble_cents > 5
    assert any("chorus" in s for s in chorused.suggestions)
    from rigsmith.tune.candidates import surrounding_blocks

    assert "chorus" in [b["role"] for b in surrounding_blocks(chorused.to_dict())]


def test_vibrato_on_every_note_is_a_block_and_a_double_is_not_a_chorus():
    vibrato = describe.sketch(modulated_delay(sustained(), 5.5, 0.6, 2.0, 1.0), RATE, from_stem=False)
    assert vibrato.modulation == "vibrato" and 5.0 < vibrato.modulation_hz < 6.0
    # Two takes of the same notes, one per side: steady on each side, so no
    # chorus - even though summed they would beat like one.
    left, right = sustained(seed=1), sustained(seed=1)
    right = np.interp(np.arange(len(left)) * 1.0007, np.arange(len(left)), right)  # a few cents apart
    double = describe.sketch(np.stack([left, right], axis=1), RATE, from_stem=False)
    assert double.modulation == "", double.describe()


def test_tremolo_is_a_level_wobble_inside_notes_not_the_rhythm():
    clean = SimAmp(Type=0, Gain=0.1).process(sustained())
    t = np.arange(len(clean)) / RATE
    wobbling = describe.sketch(clean * (1 - 0.35 * (1 + np.sin(2 * np.pi * 5.0 * t)) / 2), RATE, from_stem=False)
    assert wobbling.modulation == "tremolo" and 4.6 < wobbling.modulation_hz < 5.4, wobbling.describe()
    assert abs(wobbling.modulation_depth - 0.35) < 0.05, wobbling.modulation_depth
    # Steady eighth notes put their own pulse in the level; that is not tremolo.
    assert describe.sketch(SimAmp(Type=0, Gain=0.1).process(riff_di()), RATE, from_stem=False).modulation == ""


def test_gain_reads_the_same_drive_alike_for_a_lead_a_riff_and_chords():
    rng = np.random.default_rng(3)
    lead = render_notes([(0.2 + i * 0.3, 0.28, int(m), 0.8) for i, m in enumerate(rng.integers(62, 79, size=40))], seed=2)
    chords = render_notes(
        [(0.2 + i * 0.5 + k * 0.005, 0.45, 40 + [0, 5, 7, 3][i % 4] + interval, 0.7) for i in range(24) for k, interval in enumerate((0, 7, 12))],
        seed=4,
    )
    order = ["clean", "edge", "crunch", "high", "modern"]
    from rigsmith.tune.candidates import tier_for

    for voicing, drive in ((0, 0.1), (1, 0.3), (1, 0.6), (2, 0.6), (3, 0.9)):
        tiers = [order.index(tier_for(describe.sketch(SimAmp(Type=voicing, Gain=drive).process(src), RATE, from_stem=False).gain)) for src in (lead, riff_di(), chords)]
        assert max(tiers) - min(tiers) <= 1, (voicing, drive, tiers)
    # A lead through a high-gain amp reads as high gain, not crunch.
    assert describe.sketch(SimAmp(Type=2, Gain=0.6).process(lead), RATE, from_stem=False).gain >= 0.58


def test_a_song_mapped_by_an_older_analyser_says_so(workspace):
    made = songs.create("Old Map")
    made.structure = {"tempo": 120, "sections": []}
    made.analysis_version = 1
    made.save()
    old = songs.get(made.song_id)
    assert old.stale and old.to_dict()["stale"] and "older analyser" in old.describe()
    assert not songs.create("Never Mapped").stale


def test_your_own_copy_is_found_in_the_library_by_name(workspace, tmp_path):
    library = tmp_path / "Music"
    album = library / "Evanescence" / "Fallen"
    album.mkdir(parents=True)
    for name in ("03 Going Under.mp3", "03 Going Under.flac", "09 Going Under (Live).mp3", "04 Bring Me to Life.mp3"):
        (album / name).write_bytes(b"x")
    (library / "Other Band").mkdir()
    (library / "Other Band" / "Going Under.mp3").write_bytes(b"x")

    found = songs.find_local("Going Under", "Evanescence", roots=[library])
    assert found[0]["name"] == "03 Going Under.flac" and found[0]["artist_match"]  # lossless first
    assert all("Bring Me" not in f["name"] for f in found)
    assert {f["name"] for f in found if not f["artist_match"]} == {"Going Under.mp3"}
    assert songs.find_local("", roots=[library]) == []

    appsettings.update(music_dirs=[str(library)])
    assert songs.music_dirs() == [library]


def test_a_take_of_a_part_becomes_its_di_when_it_follows_the_record(workspace, tmp_path):
    import librosa

    from rigsmith.match.render_offline import load_audio, save_audio

    stem, _ = song(seconds_per_section=6.0)
    made = songs.create("Fixture", stem=str(save_audio(tmp_path / "stem.wav", stem, RATE)))
    mapped = songs.analyse(made.song_id)
    part = max((p for p in mapped.parts if p.layer == "left"), key=lambda p: p.end - p.start)
    target, _ = load_audio(songs.part_audio(made.song_id, part.part_id))
    take = save_audio(tmp_path / "take.wav", np.concatenate([np.zeros(RATE // 4), librosa.effects.time_stretch(target, rate=1 / 1.02)]), RATE)

    result = songs.take_for_part(made.song_id, part.part_id, str(take))
    assert result["aligned"] and songs.get(made.song_id).part(part.part_id).di_take == result["take_id"]
    noise = save_audio(tmp_path / "noise.wav", np.random.default_rng(0).normal(0, 0.05, RATE * 8), RATE)
    assert not songs.take_for_part(made.song_id, part.part_id, str(noise))["aligned"]


def test_a_corrected_tempo_retimes_each_parts_delay(workspace):
    from rigsmith.core.errors import Invalid
    from rigsmith.core.songs import Part

    made = songs.create("Tempo")
    made.structure = {"tempo": 60.0, "tempo_confidence": 0.3, "sections": []}
    # 375 ms is nothing in particular at 60 BPM, and a dotted eighth at 120.
    made.parts = [Part("verse-1-stem", 0, "verse", "stem", 0.0, 10.0, sketch=describe.ToneSketch(delay_ms=375.0).to_dict())]
    made.save()

    doubled = songs.set_tempo(made.song_id, factor=2)
    assert doubled.structure["tempo"] == 120 and doubled.structure["tempo_source"] == "hand"
    sketch = doubled.parts[0].sketch
    assert sketch["delay_note"] == "dotted eighth" and "dotted eighth" in sketch["summary"]
    assert any("dotted eighth" in s for s in sketch["suggestions"])
    assert "(set)" in doubled.describe()
    assert songs.set_tempo(made.song_id, 100.0).parts[0].sketch["delay_note"] is None
    with pytest.raises(Invalid):
        songs.set_tempo(made.song_id, 500.0)
    with pytest.raises(Invalid):
        songs.set_tempo(songs.create("Unmapped").song_id, factor=2)


def test_a_tempo_set_by_hand_survives_a_remap(workspace):
    from rigsmith.match.render_offline import save_audio

    stem, _ = song(seconds_per_section=8.0)
    made = songs.create("Remap", stem=str(save_audio(workspace / "stem.wav", stem, RATE)))
    songs.analyse(made.song_id)
    songs.set_tempo(made.song_id, 97.5)
    again = songs.analyse(made.song_id)
    assert again.structure["tempo"] == 97.5 and again.structure["tempo_source"] == "hand"


def test_a_deep_choppy_tremolo_is_heard_and_its_depth_read():
    """Its troughs used to break every note into pieces too short to measure."""
    driven = SimAmp(Type=2, Gain=0.6).process(sustained())
    t = np.arange(len(driven)) / RATE
    chopped = describe.sketch(driven * (1 - 0.8 * (1 + np.sin(2 * np.pi * 4.0 * t)) / 2), RATE, from_stem=False)
    assert chopped.modulation == "tremolo" and abs(chopped.modulation_hz - 4.0) < 0.3, chopped.describe()
    assert abs(chopped.modulation_depth - 0.8) < 0.06, chopped.modulation_depth
    assert any("depth about" in s for s in chopped.suggestions), chopped.suggestions


def test_a_fast_eighth_note_part_is_not_read_at_two_thirds():
    """155 BPM read as 103 - three eighths taken for the beat, which no
    halve or double button can fix."""
    bpm = 155.0
    beat = 60 / bpm
    notes = [(b * beat + k * beat / 2, beat / 2 * 0.85, [40, 40, 43, 45, 47, 45, 43, 40][(2 * b + k) % 8], 0.8) for b in range(48) for k in range(2)]
    audio = SimAmp(Type=3, Gain=0.6).process(render_notes(notes, seed=2))
    audio = audio / np.abs(audio).max() * 0.6
    rng = np.random.default_rng(1)
    kick = np.sin(2 * np.pi * 55 * np.arange(2000) / RATE) * np.exp(-np.arange(2000) / 400) * 0.6
    snare = rng.normal(0, 1, 3000) * np.exp(-np.arange(3000) / 500) * 0.3
    hat = rng.normal(0, 1, 600) * np.exp(-np.arange(600) / 80) * 0.08
    for b in range(48):
        for start, hit in ((int(b * beat * RATE), kick if b % 2 == 0 else snare), (int((b + 0.5) * beat * RATE), hat)):
            if start + len(hit) < len(audio):
                audio[start : start + len(hit)] += hit
    tempo, _ = structure.estimate_tempo(audio, RATE)
    assert abs(tempo / bpm - 1) < 0.02, tempo


def test_a_centred_lead_keeps_its_echo_when_its_layer_is_split_out(tmp_path, monkeypatch):
    """Split out of the stem by masking its spectrum, the lead kept its notes
    but not the copy of the waveform its dotted-eighth delay is. The stem
    still has it - in both channels and their sum, not their difference."""
    from scipy.signal import lfilter

    from rigsmith import config
    from rigsmith.core import songs
    from rigsmith.match.render_offline import save_audio

    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    try:
        beat = 0.5
        lead = [(b * beat, beat * 0.9, [64, 67, 69, 71, 74, 71, 69, 67][b % 8], 0.9) for b in range(24)]
        roots = [(b * beat, beat * 0.95, [45, 48, 43, 50][(b // 4) % 4], 0.7) for b in range(24)]
        sung = SimAmp(Type=2, Gain=0.6).process(render_notes(lead, seed=3))
        lag = int(0.375 * RATE)
        b, a = np.zeros(lag + 1), np.zeros(lag + 1)
        b[0], b[lag], a[0], a[lag] = 1.0, 0.3 - 0.35, 1.0, -0.35
        sung = lfilter(b, a, sung)
        left = SimAmp(Type=3, Gain=0.7).process(render_notes(human(roots, 1), seed=1))
        right = SimAmp(Type=3, Gain=0.7).process(render_notes(human(roots, 2), seed=2))
        n = min(len(sung), len(left), len(right))
        stem = np.stack([left[:n] + 0.7 * sung[:n], right[:n] + 0.7 * sung[:n]], axis=1)
        stem /= np.abs(stem).max() * 1.1
        made = songs.create("Echo", stem=save_audio(tmp_path / "stem.wav", stem, RATE))
        mapped = songs.analyse(made.song_id)
        centres = [p for p in mapped.parts if p.layer == "centre"]
        # (A section under ~8 s is too short to show an echo at all.)
        assert any(abs((p.sketch.get("delay_ms") or 0) - 375) < 15 for p in centres), [(p.part_id, p.sketch.get("delay_ms")) for p in centres]
        sides = [p for p in mapped.parts if p.layer in ("left", "right")]
        assert sides and not any(p.sketch.get("delay_ms") for p in sides), "the dry double-track gets no delay"
    finally:
        config.set_settings(None)


def test_a_room_is_heard_when_the_next_section_comes_in_loud():
    """A clean intro whose last half-second was the driven verse coming in:
    every stop of its own was 25 dB under that, and its room went unheard."""
    from scipy.signal import fftconvolve

    beat = 0.5
    notes = [(b * beat, 0.9 if b % 4 < 2 else 0.4, [52, 55, 59, 64][b % 4], 0.5) for b in range(24) if b % 4 != 3]
    dry = SimAmp(Type=0, Gain=0.1, Level=0.3).process(render_notes(notes, seed=4))
    rt60 = 1.6
    t = np.arange(int(rt60 * 1.2 * RATE)) / RATE
    ir = np.random.default_rng(0).normal(0, 1, len(t)) * 10 ** (-3 * t / rt60)
    wet = fftconvolve(dry, ir / np.sqrt(np.sum(ir ** 2)))[: len(dry)]
    intro = 0.7 * dry + 0.3 * wet / np.std(wet) * np.std(dry)
    verse = SimAmp(Type=3, Gain=0.7).process(render_notes([(k * 0.25, 0.22, 40, 0.9) for k in range(3)], seed=5))
    audio = np.zeros(int(12.6 * RATE))
    audio[: min(len(intro), len(audio))] += intro[: len(audio)]
    start = int(12.0 * RATE)
    audio[start : start + len(verse)] += verse[: len(audio) - start]
    read = describe.sketch(audio, RATE, from_stem=False)
    assert read.space in ("a plate or hall", "a long wash"), (read.decay_ms, read.space)
    # Each note ringing into the last one's tail wobbles the pitch the way a
    # chorus does; with a long room it is said, not made a pedal.
    from rigsmith.tune.candidates import surrounding_blocks

    assert read.modulation != "chorus", read.describe()
    assert "chorus" not in [b["role"] for b in surrounding_blocks(read.to_dict())]


def test_a_clean_part_that_is_all_attacks_reads_clean():
    """A pick's attack is broadband, and clean sixteenth-note chugs and
    arpeggios - all attacks - looked as filled-in as distortion: they read as
    crunch and edge of breakup. Their crest said clean all along."""
    beat = 60 / 110
    chugs = [(b * beat + k * beat / 4, beat / 8, 40 if b % 8 < 6 else 43, 0.85) for b in range(24) for k in range(4)]
    arpeggio = [(b * beat + k * beat / 4, beat * 0.8, root + step, 0.7) for b in range(24) for k, step in enumerate((0, 4, 7, 12)) for root in [(52, 57, 55, 50)[(b // 4) % 4]]]
    for notes in (chugs, arpeggio):
        clean = SimAmp(Type=0, Gain=0.1).process(render_notes(notes, seed=5))
        assert describe.sketch(clean, RATE, from_stem=False).gain < 0.2
    driven = SimAmp(Type=3, Gain=0.85).process(render_notes(chugs, seed=5))
    assert describe.sketch(driven, RATE, from_stem=False).gain >= 0.78, "the crest still carries a driven one"


def test_the_room_is_read_where_the_playing_stops():
    """A dry lead's own string decay read as a plate; a long reverb read as nothing."""
    from scipy.signal import fftconvolve

    beat = 0.6
    notes = [(b * beat, beat * 0.9, [64, 67, 69, 71, 74, 71, 69, 67][b % 8], 0.9) for b in range(40) if b % 16 < 14]
    dry = SimAmp(Type=2, Gain=0.5).process(render_notes(notes, seed=4))
    rng = np.random.default_rng(0)
    rt60 = 1.8
    t = np.arange(int(rt60 * 1.2 * RATE)) / RATE
    ir = rng.normal(0, 1, len(t)) * 10 ** (-3 * t / rt60)
    wet = fftconvolve(dry, ir / np.sqrt(np.sum(ir ** 2)))[: len(dry)]
    hall = 0.75 * dry + 0.25 * wet / np.std(wet) * np.std(dry)
    assert describe.sketch(dry, RATE, from_stem=False).space == "dry"
    read = describe.sketch(hall, RATE, from_stem=False)
    assert read.space == "a plate or hall", (read.decay_ms, read.space)


@pytest.mark.parametrize("amp_type, gain", [(0, 0.1), (3, 0.85)])
def test_chords_that_never_rest_are_not_read_as_a_wash(amp_type, gain):
    """Between chords on every beat, a 6 dB dip and the next chord ringing
    on at the note's own level read as a reverb tail: "a long wash" on dry
    power chords, and a plate under dry clean chords."""
    beat = 0.5
    notes = [(b * beat, beat * 0.95, m, 0.8) for b in range(24) for m in (lambda r: (r, r + 7, r + 12))((45, 48, 43, 50)[(b // 4) % 4])]
    dry = SimAmp(Type=amp_type, Gain=gain).process(render_notes(notes, seed=5))
    read = describe.sketch(dry, RATE, from_stem=False)
    assert read.space not in ("a plate or hall", "a long wash"), (read.decay_ms, read.space)


def test_a_clean_intro_keeps_its_note_spacing_when_the_verse_comes_in():
    """An arpeggio left to ring, a note every 500 ms, whose last moments are
    the loud verse coming in: measured against that, none of its own notes
    counted, it had no spacing, and its rhythm passed for a quarter-note
    delay on the demo song."""
    beat = 0.5
    notes = [(b * beat, 0.9, [52, 55, 59, 64][b % 4], 0.5) for b in range(24)]
    intro = SimAmp(Type=0, Gain=0.1, Level=0.3).process(render_notes(notes, seed=4))
    verse = [(12.0 + k * 0.25, 0.22, 40, 0.9) for k in range(4)]
    loud = SimAmp(Type=3, Gain=0.7).process(render_notes(verse, seed=5))
    audio = np.zeros(int(13.2 * RATE))
    audio[: len(intro)] += intro[: len(audio)]
    audio[: len(loud)] += loud[: len(audio)]
    assert describe._note_spacing_ms(audio[: int(12.6 * RATE)], RATE) == pytest.approx(500, abs=10)


def test_a_short_double_tracked_section_is_still_heard_as_one_part_twice():
    """Under ~4 s there was no 8 s window to judge, and the answer was 'different parts'."""
    beat = 0.5
    notes = [(i * beat / 2, beat / 2 * 0.85, [40, 43, 45, 47][i % 4], 0.8) for i in range(12)]
    left = render_notes(human(notes, 1), seed=1)
    right = render_notes(human(notes, 2), seed=2)
    n = min(len(left), len(right))
    assert layers._plays_together(left[:n], right[:n], RATE) >= 0.6


def test_a_choice_follows_its_part_when_a_re_map_merges_its_section():
    from rigsmith.core.songs import Part, carry_over

    before = {
        "chorus-3-left": Part("chorus-3-left", 2, "chorus", "left", 60.0, 72.0, chosen=True, note="the big one", di_take="t1"),
        "chorus-4-left": Part("chorus-4-left", 3, "chorus", "left", 72.0, 84.0),
        "verse-2-right": Part("verse-2-right", 1, "verse", "right", 30.0, 60.0, chosen=True, di_take="t2"),
    }
    after = [
        Part("chorus-3-left", 2, "chorus", "left", 60.0, 84.0),
        Part("verse-2-left", 1, "verse", "left", 30.0, 60.0),
        Part("verse-2-right", 1, "verse", "right", 30.0, 60.0),
    ]
    # Same id: analyse keeps it itself; here only the merged and moved ones.
    carry_over({k: v for k, v in before.items() if k != "chorus-3-left"}, after)
    assert not after[0].chosen, "nothing chosen came from the second half"
    renamed = [Part("refrain-3-left", 2, "refrain", "left", 59.8, 84.0)]
    carry_over(before, renamed)
    assert renamed[0].chosen and renamed[0].note == "the big one", "the choice follows the part that covers it"
    assert renamed[0].di_take is None, "a DI made for 60-72 s does not fit 60-84 s"
    assert not after[1].chosen, "only on its own layer"


def test_two_old_parts_merged_into_one_keep_both_notes():
    from rigsmith.core.songs import Part, carry_over

    before = {
        "chorus-3-left": Part("chorus-3-left", 2, "chorus", "left", 60.0, 72.0, chosen=True, note="first half", recipe_id="r1"),
        "chorus-4-left": Part("chorus-4-left", 3, "chorus", "left", 72.0, 84.0, note="second half", recipe_id="r2"),
    }
    merged = [Part("chorus-5-left", 4, "chorus", "left", 60.0, 84.0)]
    carry_over(before, merged)
    assert merged[0].chosen and merged[0].recipe_id == "r1"
    assert "first half" in merged[0].note and "second half" in merged[0].note and "r2" in merged[0].note


def test_a_reading_follows_its_part_through_a_re_map():
    from rigsmith.core.songs import Part, carry_over

    before = {"intro-1-left": Part("intro-1-left", 0, "intro", "left", 0.0, 12.0, reading="arpeggio")}
    renamed = [Part("verse-1-left", 0, "verse", "left", 0.0, 12.2)]
    carry_over(before, renamed)
    assert renamed[0].reading == "arpeggio"


def test_a_part_says_how_its_rendered_notes_were_heard():
    """So a wrong automatic reading is visible on the part, beside the fix."""
    from types import SimpleNamespace

    from rigsmith.core.songs import _heard_as

    assert _heard_as(SimpleNamespace(source="transcribed", method="builtin, polyphonic")) == "polyphonic"
    assert _heard_as(SimpleNamespace(source="transcribed", method="builtin, arpeggio, read down to C2")) == "arpeggio"
    assert _heard_as(SimpleNamespace(source="transcribed", method="basic-pitch, polyphonic")) == "basic-pitch"
    assert _heard_as(SimpleNamespace(source="midi", method=None)) == "midi"


def test_a_rendered_part_s_notes_can_be_read_back(workspace):
    """The notes behind a DI, to draw over the part and to fix in a tab editor."""
    import json

    from rigsmith.core import takes
    from rigsmith.core.errors import NotFound
    from rigsmith.core.songs import Part
    from rigsmith.learn import midifile
    from rigsmith.learn.synth import NoteEvent
    from rigsmith.match.render_offline import save_audio

    made = songs.create("Shown")
    made.parts = [Part("intro-1-stem", 0, "intro", "stem", 0.0, 4.0)]
    made.save()
    with pytest.raises(NotFound):
        songs.part_notes(made.song_id, "intro-1-stem")
    wav = workspace / "shown-part.wav"
    save_audio(wav, render_notes([(0.0, 0.5, 52, 0.8)], seed=1), RATE)
    events = [NoteEvent(start=0.0, duration=0.5, midi=52.0)]
    wav.with_suffix(".notes.json").write_text(json.dumps({"source": "transcribed", "events": [e.to_dict() for e in events]}), encoding="utf-8")
    midifile.write(wav.with_suffix(".mid"), events)
    take = takes.add(wav, take_id="shown", kind="di", origin="synth", copy=False)
    song = songs.get(made.song_id)
    song.part("intro-1-stem").di_take = take.take_id
    song.save()
    notes = songs.part_notes(made.song_id, "intro-1-stem")
    assert [round(e["midi"]) for e in notes["events"]] == [52]
    assert notes["duration"] == 4.0 and notes["source"] == "transcribed" and notes["midi_path"]

    # And from the command line, the notes and their MIDI to fix.
    from click.testing import CliRunner

    from rigsmith.cli import main

    out = workspace / "fix-me.mid"
    result = CliRunner().invoke(main, ["song", "notes", made.song_id, "intro-1-stem", "--midi-out", str(out)])
    assert result.exit_code == 0, result.output
    assert "1 notes from transcribed, E3 to E3" in result.output and out.is_file()


def test_a_researched_tuning_sets_the_lowest_string_the_notes_are_read_to(workspace, monkeypatch):
    """Inferred from the stem, a drop-C part of chords and chugs read as E
    standard and its low C came back an octave up; the recipe knew better."""
    from types import SimpleNamespace

    from rigsmith.core import parts as part_rendering
    from rigsmith.core import recipes
    from rigsmith.core.songs import Part

    seen = []
    monkeypatch.setattr(part_rendering, "part_di", lambda profile_id, **kwargs: seen.append(kwargs["lowest"]) or SimpleNamespace(take_id="t", source="transcribed", method="builtin, polyphonic"))
    made = songs.create("Low")
    made.parts = [Part("verse-1-stem", 0, "verse", "stem", 0.0, 10.0)]
    made.save()
    songs.render_part_di(made.song_id, "verse-1-stem", None)
    recipe_id, _ = recipes.create("Low")
    recipes.set_instrument(recipe_id, tuning="drop C")
    songs.update(made.song_id, recipe_id=recipe_id)
    songs.render_part_di(made.song_id, "verse-1-stem", None)
    recipes.set_instrument(recipe_id, tuning="something unheard of")
    songs.render_part_di(made.song_id, "verse-1-stem", None)
    assert seen == ["auto", "C2", "auto"]


def test_a_part_keeps_the_reading_it_was_corrected_to(workspace, monkeypatch):
    """Autopilot renders a part's DI afresh on every run: a part the user
    had read as an arpeggio would have gone back to chords on the next."""
    from types import SimpleNamespace

    from rigsmith.core import parts as part_rendering
    from rigsmith.core.errors import Invalid
    from rigsmith.core.songs import Part

    seen = []

    def fake_part_di(profile_id, **kwargs):
        seen.append(kwargs["mode"])
        return SimpleNamespace(take_id="rendered", source="transcribed", method=f"builtin, {'arpeggio' if kwargs['mode'] == 'arpeggio' else 'polyphonic'}")

    monkeypatch.setattr(part_rendering, "part_di", fake_part_di)
    made = songs.create("Picked")
    made.parts = [Part("intro-1-stem", 0, "intro", "stem", 0.0, 10.0)]
    made.save()
    songs.render_part_di(made.song_id, "intro-1-stem", None)
    songs.render_part_di(made.song_id, "intro-1-stem", None, reading="arpeggio")
    songs.render_part_di(made.song_id, "intro-1-stem", None)
    assert seen == ["auto", "arpeggio", "arpeggio"]
    assert songs.get(made.song_id).part("intro-1-stem").reading == "arpeggio"
    assert songs.get(made.song_id).part("intro-1-stem").heard_as == "arpeggio"
    with pytest.raises(Invalid):
        songs.render_part_di(made.song_id, "intro-1-stem", None, reading="chug")


def test_a_run_finds_its_part_again_after_a_re_map_renames_it(workspace):
    from rigsmith.core import autopilot
    from rigsmith.core.songs import Part

    made = songs.create("Renamed")
    made.parts = [Part("refrain-2-left", 1, "refrain", "left", 20.0, 50.0)]
    made.save()
    run = autopilot.Run(run_id="renamed", request=autopilot.Request(title="Renamed"), song_id=made.song_id, part_ids=["chorus-2-left"])
    run.part_spans = {"chorus-2-left": ["left", 20.5, 49.5]}
    part = autopilot._part(songs.get(made.song_id), run)
    assert part.part_id == "refrain-2-left" and run.part_ids == ["refrain-2-left"]
    assert any("re-mapped" in n for n in run.notes)
    lost = autopilot.Run(run_id="lost", request=autopilot.Request(title="Renamed"), song_id=made.song_id, part_ids=["gone-9-left"])
    with pytest.raises(NotFound):
        autopilot._part(songs.get(made.song_id), lost)

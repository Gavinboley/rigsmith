"""Learning a guitar, and playing parts on it.

Every check here runs against the synthetic plucked string, whose pitches and
timings are known exactly. That is the point of having it: a segmenter that
can only be tested with a guitar plugged in is one nobody tests.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

from rigsmith import config  # noqa: E402
from rigsmith.core import profiles  # noqa: E402
from rigsmith.core.errors import Conflict, Invalid, NotFound  # noqa: E402
from rigsmith.learn import align, instrument, midifile, protocols, voice  # noqa: E402
from rigsmith.learn.pluck import pluck, render_notes  # noqa: E402
from rigsmith.learn.segment import detect_onsets, segment  # noqa: E402
from rigsmith.learn.synth import NoteEvent, parse_events, render, shift  # noqa: E402
from rigsmith.learn.transcribe import transcribe  # noqa: E402
from rigsmith.match.pitch import _estimate_f0  # noqa: E402

RATE = 48000


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def riff(count=16, spacing=0.25, start=0.5, pitches=(40, 43, 45, 47, 45, 43, 52, 50)):
    return [(start + i * spacing, spacing * 0.9, pitches[i % len(pitches)], 0.8) for i in range(count)]


# --------------------------------------------------------------------------
# the instrument
# --------------------------------------------------------------------------


def test_tunings_put_strings_where_a_guitarist_numbers_them():
    standard = instrument.tuning("E standard")
    assert standard.open_midi(6) == 40  # low E
    assert standard.open_midi(1) == 64  # high E
    # The high E is four places on the neck below the 15th fret.
    assert instrument.Position(1, 0) in standard.positions(64)
    assert len(standard.positions(64, max_fret=15)) == 4


def test_a_tuning_can_be_written_as_notes_either_way_round():
    low_first = instrument.tuning("D2 A2 D3 G3 B3 E4")
    high_first = instrument.tuning("E4 B3 G3 D3 A2 D2")
    assert low_first.open_strings == high_first.open_strings
    assert instrument.tuning("drop d").name == "drop D"
    with pytest.raises(Invalid):
        instrument.tuning("banjo tuning")


def test_note_names_round_trip_including_flats():
    assert instrument.note_to_midi("E2") == 40
    assert instrument.note_to_midi("Bb1") == instrument.note_to_midi("A#1") == 34
    assert instrument.midi_to_note(64) == "E4"


# --------------------------------------------------------------------------
# the synthetic string
# --------------------------------------------------------------------------


@pytest.mark.parametrize("midi", [40, 45.3, 64, 76])
def test_the_synthetic_string_is_in_tune(midi):
    note = pluck(midi, 1.0, seed=1)
    hz, clarity = _estimate_f0(note[int(0.3 * RATE) : int(0.39 * RATE)], RATE)
    assert clarity > 0.8
    assert abs(instrument.cents_between(hz, midi)) < 2.0


def test_a_harder_pick_is_louder_and_brighter():
    from rigsmith.learn.segment import _centroid

    soft = pluck(52, 1.0, velocity=0.2, seed=2)
    hard = pluck(52, 1.0, velocity=1.0, seed=2)
    assert np.abs(hard).max() > np.abs(soft).max() * 1.5
    window = slice(0, int(0.05 * RATE))
    assert _centroid(hard[window], RATE) > _centroid(soft[window], RATE)


# --------------------------------------------------------------------------
# segmenting
# --------------------------------------------------------------------------


def test_every_note_of_a_run_is_found_once():
    notes = riff(14, spacing=0.8)
    found = segment(render_notes(notes, seed=3), RATE)
    assert len(found) == len(notes)
    assert [f.nearest for f in found] == [n[2] for n in notes]


def test_onsets_land_on_the_attack_not_a_frame_early():
    notes = riff(12, spacing=0.3)
    detected = np.array(detect_onsets(render_notes(notes, seed=4), RATE))
    errors = [np.min(np.abs(detected - start)) for start, *_ in notes]
    assert max(errors) < 0.003


def test_a_compressed_distorted_riff_still_shows_its_onsets():
    notes = riff(24, spacing=0.2375)
    distorted = np.tanh(render_notes(notes, seed=3) * 20) * 0.3
    assert len(detect_onsets(distorted, RATE)) >= 23


def test_clipping_is_flagged():
    loud = render_notes(riff(4, spacing=0.6), seed=5) * 20
    loud = np.clip(loud, -1, 1)
    assert any("clipped" in n.flags for n in segment(loud, RATE))


# --------------------------------------------------------------------------
# protocols
# --------------------------------------------------------------------------


def test_a_missed_step_is_missing_not_a_shift_of_every_later_label():
    protocol = protocols.get("quick")
    played = []
    t = 0.5
    for index, step in enumerate(protocol.steps):
        if index == 5:
            continue
        midi = step.midi + (2 if index == 9 else 0)
        played.append((t, 1.2, midi, 0.75))
        t += 1.4
    notes = segment(render_notes(played, seed=2), RATE)
    result = protocols.align(protocol, notes)
    assert result.missing == [5]
    assert [i for i, _ in result.wrong] == [9]
    protocols.label(protocol, notes, result)
    assert (notes[0].string, notes[0].fret) == (6, 0)
    # The note after the missed one keeps its own label.
    assert notes[5].label == protocol.steps[6].label


def test_chromatic_protocol_covers_every_fret_of_every_string():
    protocol = protocols.get("chromatic", frets=12)
    assert len(protocol.steps) == 6 * 13
    assert protocol.steps[0].label == "s6f0 E2"
    with pytest.raises(Invalid):
        protocols.get("chromatic", frets=40)


# --------------------------------------------------------------------------
# a profile, end to end
# --------------------------------------------------------------------------


def test_learning_a_protocol_builds_a_playable_profile(workspace):
    path, protocol = profiles.demo_take("quick")
    profiles.create("Demo LP", guitar="Demo LP", pickup="bridge")
    report = profiles.learn("demo-lp", str(path), protocol_id="quick")

    assert report.samples_added == len(protocol.steps)
    assert report.coverage == 1.0
    assert report.voice_fitted

    profile = profiles.get("demo-lp")
    grid = profile.coverage(frets=12)
    assert grid["filled"] == len(protocol.steps)
    assert profile.fingerprint.median_peak_db < -3
    assert abs(profile.fingerprint.tuning_cents or 0) < 5

    # The same recording twice would double its weight.
    with pytest.raises(Conflict):
        profiles.learn("demo-lp", str(path), protocol_id="quick")


def test_a_rendered_part_says_which_notes_were_really_the_guitar(workspace):
    path, _ = profiles.demo_take("quick")
    profiles.create("Demo LP")
    profiles.learn("demo-lp", str(path), protocol_id="quick")

    out, rendered = profiles.render_text("demo-lp", "0 0.4 E2\n0.5 0.4 A2\n1.0 0.5 E6")
    assert out.is_file()
    assert rendered.notes == 3
    # E2 and A2 were recorded; E6 is past anything the profile heard.
    assert rendered.voices["sample"] == 2
    assert rendered.voices["voice"] + rendered.voices["pluck"] == 1


def test_a_profile_that_does_not_exist_names_the_ones_that_do(workspace):
    profiles.create("One")
    with pytest.raises(NotFound) as caught:
        profiles.get("two")
    assert "one" in str(caught.value)


def test_rendering_without_a_profile_says_so():
    rendered = render([NoteEvent(0, 0.5, 45)], None)
    assert rendered.voices["pluck"] == 1
    assert any("synthetic" in w for w in rendered.warnings)


def test_the_riff_language_reads_sharps_and_comments():
    events = parse_events("# the intro\n0 0.5 A#2 0.9  # bend it\n0.5 0.5 64")
    assert [e.midi for e in events] == [46, 64]
    assert events[0].velocity == 0.9
    with pytest.raises(Invalid):
        parse_events("0 E2")


def test_the_quick_form_counts_beats():
    events = parse_events("# a riff\n@120 E2 G2:4 (E2+B2)x2 - A2! E2' x2")
    starts = [round(e.start, 3) for e in events]
    assert starts == [0.0, 0.25, 0.75, 0.75, 1.0, 1.0, 1.5, 1.75, 2.0]
    assert [e.midi for e in events][:4] == [40, 43, 40, 47]
    assert events[6].velocity == 0.95 and events[7].velocity == 0.4
    assert round(parse_events("@60 E2:8. G2")[1].start, 3) == 0.75
    for bad in ("E2:7", "(E2 G2", "E2 H9"):
        with pytest.raises(Invalid):
            parse_events(bad)


def test_shifting_by_resampling_moves_pitch_and_length_together():
    note = pluck(45, 1.0, seed=3)
    up = shift(note, 2.0)
    assert len(up) < len(note)
    hz, _ = _estimate_f0(up[int(0.25 * RATE) : int(0.34 * RATE)], RATE)
    assert abs(instrument.cents_between(hz, 47)) < 5


def test_the_voice_model_rebuilds_a_note_at_the_right_pitch():
    model = voice.fit([(pluck(m, 2.0, seed=m), m, 6) for m in (40, 45, 47, 52)], RATE)
    built = model.synthesize(43, 1.5, string=6, seed=1)
    hz, clarity = _estimate_f0(built[int(0.3 * RATE) : int(0.39 * RATE)], RATE)
    assert clarity > 0.8
    assert abs(instrument.cents_between(hz, 43)) < 3


# --------------------------------------------------------------------------
# from a record
# --------------------------------------------------------------------------


@pytest.mark.parametrize("drive", [None, 30])
def test_a_single_note_riff_transcribes_exactly_clean_or_distorted(drive):
    notes = riff(16)
    audio = render_notes(notes, seed=1)
    if drive:
        audio = np.tanh(audio * drive) * 0.3
    result = transcribe(audio, RATE)
    assert result.mode == "monophonic"
    found = [(round(e.start, 2), int(round(e.midi))) for e in result.events]
    expected = [(round(start, 2), midi) for start, _, midi, _ in notes]
    hits = sum(any(abs(s - es) <= 0.03 and m == em for s, m in found) for es, em in expected)
    assert hits == len(notes)
    assert len(found) == len(notes)


def test_chords_are_heard_as_chords():
    chords = []
    t = 0.5
    for shape in ([40, 47, 52], [45, 52, 57], [43, 50, 55]):
        chords.extend((t + 0.008 * k, 0.9, m, 0.75) for k, m in enumerate(shape))
        t += 1.0
    result = transcribe(render_notes(chords, seed=2), RATE)
    assert result.mode == "polyphonic"
    # Every root is found (octave doublings are physically ambiguous).
    for start, root in ((0.5, 40), (1.5, 45), (2.5, 43)):
        assert any(abs(e.start - start) < 0.06 and int(round(e.midi)) % 12 == root % 12 for e in result.events)


def test_a_midi_file_round_trips():
    events = [NoteEvent(i * 0.25, 0.2, 40 + i, 0.8) for i in range(8)]
    tmp = midifile.write(__import__("tempfile").mkdtemp() + "/riff.mid", events, tempo_bpm=100)
    back = midifile.to_events(midifile.read(tmp))
    assert [e.midi for e in back] == [e.midi for e in events]
    assert max(abs(a.start - b.start) for a, b in zip(back, events)) < 0.002


@pytest.mark.parametrize("tempo", [0.8, 1.12, 1.25])
def test_a_tab_at_the_wrong_tempo_is_put_on_the_records_clock(tempo):
    pitches = [40, 43, 45, 47, 45, 43, 52, 50, 48, 47, 45, 43, 40, 40, 47, 45]
    tab = [NoteEvent(i * 0.25, 0.22, m, 0.8) for i, m in enumerate(pitches * 2)]
    truth = [1.3 + e.start * tempo for e in tab]
    record = np.tanh(render_notes([(t, 0.24, e.midi, 0.8) for t, e in zip(truth, tab)], seed=3) * 20) * 0.3

    result = align.align_events(tab, record, RATE)
    errors = [abs(e.start - t) for e, t in zip(result.events, truth)]
    assert np.median(errors) < 0.005
    assert sum(error > 0.05 for error in errors) <= 0.2 * len(tab)
    assert result.tempo_ratio == pytest.approx(tempo, rel=0.06)


# ---------------------------------------------------------------------------
# pickups
# ---------------------------------------------------------------------------


def _line(pickup_name, seed=0):
    from rigsmith.learn.synth import NoteEvent

    rng = np.random.default_rng(seed)
    events = [NoteEvent(0.25 * i, 0.6, float(rng.integers(40, 76)), float(rng.uniform(0.5, 0.95))) for i in range(48)]
    return render(events, None, sample_rate=48000, pickup=pickup_name).audio


def test_a_pickup_resonance_is_measured_from_what_was_played():
    from rigsmith.learn import pickup
    from rigsmith.learn.pluck import PICKUPS as SYNTHETIC

    for name in ("bridge", "middle", "neck-single"):
        found = pickup.estimate(_line(name), 48000)
        truth = SYNTHETIC[name]
        assert abs(np.log2(found.f0_hz / truth.resonance_hz)) < 0.25, (name, found.describe())
    assert pickup.estimate(_line("bridge"), 48000).reads_as in ("paf", "hot-humbucker", "p90")
    assert pickup.estimate(_line("middle"), 48000).reads_as in ("strat", "tele", "filtertron")


def test_a_humbucker_di_is_moved_to_a_single_coil_and_stays_aligned():
    from rigsmith.learn import pickup
    from rigsmith.learn.pluck import PICKUPS as SYNTHETIC

    humbucker, single = _line("bridge"), _line("bridge-single")
    target = pickup.Resonance(SYNTHETIC["bridge-single"].resonance_hz, SYNTHETIC["bridge-single"].q)
    moved = pickup.swap(humbucker, 48000, pickup.estimate(humbucker, 48000), target)

    def shape(audio):
        _, levels = pickup._spectrum(audio, 48000)
        return levels - levels.max()

    before = np.sqrt(np.mean((shape(humbucker) - shape(single)) ** 2))
    after = np.sqrt(np.mean((shape(moved) - shape(single)) ** 2))
    assert after < before / 2, (before, after)
    # No delay: the swap keeps the DI on the record's clock.
    lag = np.argmax(np.correlate(moved[:4800], humbucker[:4800], "full")) - 4799
    assert lag == 0
    halfway = pickup.morph(pickup.named("paf"), pickup.named("strat"), 0.5)
    assert pickup.named("paf").f0_hz < halfway.f0_hz < pickup.named("strat").f0_hz


def test_a_recipes_words_name_a_pickup():
    from rigsmith.learn.pickup import named

    assert named("Fender Stratocaster").label == "strat"
    assert named("Gibson Les Paul Junior").label == "p90"
    assert named("Les Paul Custom with '57 Classics").label == "paf"
    assert named("ESP with EMG 81").label == "active"
    assert named("a cardboard box") is None


# ---------------------------------------------------------------------------
# the learned voice
# ---------------------------------------------------------------------------


def test_the_network_only_learns_when_it_has_enough_notes_and_then_helps(workspace):
    from rigsmith.learn import neural
    from rigsmith.learn.instrument import midi_to_hz
    from rigsmith.learn.voice import VoiceModel

    take, _ = profiles.demo_take("chromatic")
    profiles.create("full")
    profiles.learn("full", str(take), protocol_id="chromatic", fit_voice=True)
    model = profiles.voice_model("full")
    assert model.net and model.net["notes"] >= neural.MIN_NOTES
    assert neural.train(model.notes[:20]) is None  # a quick session: too few to help

    # Hold notes out one at a time: the blend predicts them closer than the
    # neighbours alone (on most notes, and on average).
    notes = [n for n in model.notes if n.string]
    plain_errors, blend_errors = [], []
    for i in range(0, len(notes), 6):
        held, rest = notes[i], notes[:i] + notes[i + 1 :]
        f0 = midi_to_hz(held.midi)
        truth = neural.on_grid(held.level_db, f0, held.inharmonicity)
        audible = truth > -50
        plain = VoiceModel(notes=rest)
        blended = VoiceModel(notes=rest, net=neural.train(rest).to_dict())
        for model_, errors in ((plain, plain_errors), (blended, blend_errors)):
            levels, _, inharm, _ = model_.parameters(held.midi, held.string)
            errors.append(np.mean(np.abs(neural.on_grid(levels, f0, inharm)[audible] - truth[audible])))
    assert np.mean(blend_errors) < np.mean(plain_errors), (np.mean(blend_errors), np.mean(plain_errors))
    # And it survives being saved with the profile.
    assert VoiceModel.from_dict(model.to_dict()).net == model.net


def test_a_take_played_along_is_put_on_the_records_clock():
    from rigsmith.learn.align import align_take, apply_take
    from rigsmith.learn.pluck import render_notes as pluck
    from rigsmith.loop.virtual import SimAmp
    from rigsmith.match.metrics import performance_distance

    rng = np.random.default_rng(1)
    notes = [(0.5 + i * 0.3, 0.27, int(rng.integers(40, 60)), 0.8) for i in range(48)]
    jitter = lambda ns: [(t + rng.normal(0, 0.01), d, m, v) for t, d, m, v in ns]  # noqa: E731
    take = pluck(jitter(notes), seed=3)
    amp = lambda x: SimAmp(Type=2, Gain=0.6).process(x)  # noqa: E731
    record = amp(pluck([(t * 1.04 + 0.37, d * 1.04, m, v) for t, d, m, v in jitter(notes)], seed=9))

    alignment = align_take(take, record, 48000)
    assert alignment.usable and abs(alignment.rate - 1.04) < 0.01 and abs(alignment.offset_s - 0.37) < 0.06
    placed = apply_take(take, 48000, alignment, len(record), record=record)
    unplaced = np.pad(take, (0, max(0, len(record) - len(take))))[: len(record)]
    assert performance_distance(record, amp(placed), 48000) < 0.75 * performance_distance(record, amp(unplaced), 48000)

    other = pluck([(0.5 + i * 0.3, 0.27, int(rng.integers(62, 76)), 0.8) for i in range(48)], seed=4)
    assert not align_take(other, record, 48000).usable  # a different part is not stretched onto it


def test_a_take_that_rushes_is_called_faster():
    """record = rate x take + offset: a rate above 1 gets through the music sooner."""
    from rigsmith.learn.align import TakeAlignment

    rushing = TakeAlignment(rate=1.1, offset_s=0.0, residual_s=0.01, agreement=0.9, contrast=0.3, tonality=2.0, level=0.5)
    dragging = TakeAlignment(rate=0.9, offset_s=0.0, residual_s=0.01, agreement=0.9, contrast=0.3, tonality=2.0, level=0.5)
    assert "10.0% faster" in rushing.describe() and "10.0% slower" in dragging.describe()


def test_a_note_that_starts_a_hair_before_zero_is_trimmed_not_a_crash():
    """Humanised timing put the first note at -5 ms; the index wrapped and broke."""
    from rigsmith.learn.pluck import render_notes

    audio = render_notes([(-0.005, 0.3, 40, 0.8), (0.5, 0.3, 45, 0.8)], sample_rate=44100)
    assert np.abs(audio[:2000]).max() > 0, "the first note is heard from the start"
    assert len(audio) > int(0.8 * 44100)


def test_a_high_gain_riff_is_transcribed_as_single_notes():
    """Heavy distortion read as polyphony 4, and the riff came out as chords."""
    from rigsmith.learn.pluck import render_notes
    from rigsmith.learn.transcribe import transcribe
    from rigsmith.loop.virtual import SimAmp

    beat = 60 / 110
    truth = [(b * beat + k * beat / 2, beat / 2 * 0.85, [40, 40, 43, 45, 47, 45, 43, 40][(2 * b + k) % 8], 0.8) for b in range(24) for k in range(2)]
    audio = SimAmp(Type=3, Gain=0.85).process(render_notes(truth, seed=5))
    result = transcribe(audio, 48000, method="builtin", match_record_pitch=False)
    assert result.mode == "monophonic", result.polyphony
    hits = sum(any(abs(e.start - t) < 0.05 and abs(e.midi - m) < 0.5 for e in result.events) for t, _, m, _ in truth)
    assert hits >= 0.95 * len(truth)


def test_strummed_triads_are_read_as_their_chords_not_a_fundamental_below():
    """The note-by-note estimate heard E-G#-B as one E an octave down."""
    from rigsmith.learn.pluck import render_notes
    from rigsmith.learn.transcribe import transcribe
    from rigsmith.loop.virtual import SimAmp

    beat = 60 / 110
    truth = []
    for b in range(0, 32, 2):
        root = [52, 57, 55, 50][(b // 2) % 4]
        truth += [(b * beat, beat * 1.8, m, 0.7) for m in (root, root + 4, root + 7)]
    audio = SimAmp(Type=2, Gain=0.5).process(render_notes(truth, seed=5))
    result = transcribe(audio, 48000, method="builtin", match_record_pitch=False)
    right = 0
    for start in sorted({t for t, _, _, _ in truth}):
        want = {m % 12 for t, _, m, _ in truth if t == start}
        got = {round(e.midi) % 12 for e in result.events if abs(e.start - start) < 0.06}
        right += bool(got) and len(want & got) / len(want | got) >= 0.66
    assert right >= 0.8 * 16, right
    assert any("pitch classes" in note for note in result.notes_text)


def test_a_driven_power_chord_is_read_from_its_root_not_its_fifth():
    """Through a crunchy amp the root rang quieter than the fifth, and half
    the chords came back a fifth up - a D5 as an A5."""
    from rigsmith.learn.pluck import render_notes
    from rigsmith.learn.transcribe import transcribe
    from rigsmith.loop.virtual import SimAmp

    beat = 60 / 110
    truth = []
    for b in range(32):
        root = [40, 43, 45, 38][(b // 2) % 4]
        truth += [(b * beat, beat * 0.9, m, 0.8) for m in (root, root + 7)]
    audio = SimAmp(Type=2, Gain=0.5).process(render_notes(truth, seed=5))
    result = transcribe(audio, 48000, method="builtin", match_record_pitch=False)
    right = 0
    for start in sorted({t for t, _, _, _ in truth}):
        root = min(m for t, _, m, _ in truth if t == start) % 12
        got = [e.midi for e in result.events if abs(e.start - start) < 0.06]
        right += bool(got) and round(min(got)) % 12 == root
    assert right >= 0.85 * 32, right


def test_palm_muted_chugs_are_single_notes_not_chords():
    """Sixteenth-note chugs on the low E came back as a chord at every
    attack - the note's harmonics read as its fifth and octave."""
    from rigsmith.learn.pluck import render_notes
    from rigsmith.learn.transcribe import transcribe
    from rigsmith.loop.virtual import SimAmp

    beat = 60 / 110
    truth = [(b * beat + k * beat / 4, beat / 8, 40 if b % 8 < 6 else 43, 0.85) for b in range(32) for k in range(4)]
    audio = SimAmp(Type=0, Gain=0.1).process(render_notes(truth, seed=5))
    result = transcribe(audio, 48000, method="builtin", match_record_pitch=False)
    single = 0
    for start, _, midi, _ in truth:
        heard = [e for e in result.events if abs(e.start - start) < 0.05]
        single += len(heard) == 1 and round(heard[0].midi) == midi
    assert single >= 0.85 * len(truth), single
    assert len(result.events) <= 1.15 * len(truth), "no chord stacked on each chug"


@pytest.mark.parametrize("gain", [0.5, 0.85])
def test_driven_chugs_on_the_low_e_are_read_as_low_es(gain):
    """The salience heard an A1 under every low E and took the guitar for a
    seven-string; the chugs then went down the chord path and came back as
    stacks. The low string has to be heard to be believed."""
    from rigsmith.learn.pluck import render_notes
    from rigsmith.learn.transcribe import transcribe
    from rigsmith.loop.virtual import SimAmp

    beat = 60 / 110
    truth = [(b * beat + k * beat / 4, beat / 8, 40 if b % 8 < 6 else 43, 0.85) for b in range(32) for k in range(4)]
    audio = SimAmp(Type=3, Gain=gain).process(render_notes(truth, seed=5))
    result = transcribe(audio, 48000, method="builtin", match_record_pitch=False)
    assert "lowest string taken as E2" in result.notes_text
    right = sum(1 for start, _, midi, _ in truth if [round(e.midi) for e in result.events if abs(e.start - start) < 0.05] == [midi])
    assert right >= 0.85 * len(truth), right


def test_a_drop_d_riff_keeps_its_low_d():
    from rigsmith.learn.pluck import render_notes
    from rigsmith.learn.transcribe import transcribe

    beat = 60 / 110
    truth = [(b * beat + k * beat / 2, beat / 2 * 0.85, [38, 38, 41, 43][b % 4], 0.85) for b in range(32) for k in range(2)]
    result = transcribe(render_notes(truth, seed=5), 48000, method="builtin", match_record_pitch=False)
    lows = sum(1 for e in result.events if round(e.midi) == 38)
    assert lows >= 0.85 * sum(1 for n in truth if n[2] == 38), "the low D is a real note, not cut off as an echo"


@pytest.mark.parametrize("intervals, driven", [
    ((0, 5, 7), False),          # a sus4: its fourth sits beside its fifth
    ((0, 5, 7), True),
    ((0, 5, 9), True),           # an inversion - C over G - with no fifth or third above the bass
    ((0, 7, 12, 16, 19), True),  # an open-position voicing
    ((0, 4, 7), False),
])
def test_a_real_chord_is_never_collapsed_to_one_note(intervals, driven):
    """The test for 'one note with harmonics' must not take real chords."""
    from rigsmith.learn.pluck import render_notes
    from rigsmith.learn.synth import NoteEvent
    from rigsmith.learn.transcribe import _collapse_single_notes
    from rigsmith.loop.virtual import SimAmp

    notes = [(0.5, 0.98, 40 + i, 0.8) for i in intervals] + [(1.5, 0.2, 40, 0.3), (3.5, 0.2, 40, 0.3)]
    audio = render_notes(notes, seed=7, tail=1.0)
    if driven:
        audio = SimAmp(Type=2, Gain=0.5).process(audio)
    events = [NoteEvent(start=t, duration=d, midi=float(m), velocity=v) for t, d, m, v in notes]
    kept, _ = _collapse_single_notes(audio, 48000, events)
    assert sorted(round(e.midi) for e in kept if abs(e.start - 0.5) < 0.01) == sorted(40 + i for i in intervals)


def test_the_voice_network_reads_frets_from_the_guitars_own_tuning():
    """It assumed standard tuning, so a drop-D low string was two frets out."""
    from rigsmith.learn import neural

    drop_d = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 38}
    assert neural.features(38, 6, drop_d)[6] == 0.0, "open low D is fret 0 in drop D"
    assert neural.features(38, 6)[6] < 0, "and below the nut in standard"


def test_synthesis_sizes_its_harmonics_for_the_real_sample_rate():
    from rigsmith.learn import voice

    assert voice.harmonic_count(82.4, 44100) <= voice.harmonic_count(82.4, 48000)
    # parameters() takes the rate synthesize() is given, not 48 kHz regardless.
    import inspect

    assert "sample_rate" in inspect.signature(voice.VoiceModel.parameters).parameters


def test_a_drop_d_profile_trains_its_network_on_drop_d():
    import types

    from rigsmith.core.profiles import _open_strings

    opens = _open_strings(types.SimpleNamespace(tuning="drop D"))
    assert opens[6] == 38 and opens[1] == 64
    assert _open_strings(types.SimpleNamespace(tuning="no such tuning")) is None


def test_free_play_counts_every_note_as_unasked_for():
    from rigsmith.learn import protocols
    from rigsmith.learn.segment import Note

    notes = [Note(start=0.5 * i, end=0.5 * i + 0.4, hz=110.0, midi=45.0, clarity=0.9, peak_db=-12.0) for i in range(3)]
    assert protocols.align(protocols.get("free"), notes).extra == [0, 1, 2]

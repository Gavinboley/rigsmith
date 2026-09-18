"""What the guitar was tuned to when a take was made.

Two separate questions. The tuning can only be answered by what was played -
if the low string never sounds, nothing can know it was there - so this side
reports and hedges. Concert pitch is the one that affects measurement: a take
recorded flat puts every harmonic slightly off where the record's sits, and a
band comparison reads that as tone.
"""

from __future__ import annotations

import numpy as np
import pytest

from rigsmith.match.pitch import (
    CENTS_TOLERANCE,
    PitchReport,
    analyse,
    note_name,
)

RATE = 48000

#: Open strings, E standard.
E2, A2, D3, G3, B3, E4 = 82.41, 110.0, 146.83, 196.0, 246.94, 329.63


def pluck(frequency: float, *, seconds: float = 0.5, cents: float = 0.0):
    """A plucked note: a few harmonics under a decaying envelope."""
    tuned = frequency * (2 ** (cents / 1200))
    t = np.arange(int(seconds * RATE)) / RATE
    partials = sum(
        (1 / (harmonic + 1)) * np.sin(2 * np.pi * tuned * (harmonic + 1) * t)
        for harmonic in range(6)
    )
    return partials * np.exp(-t * 3)


def riff(*frequencies, cents: float = 0.0):
    return np.concatenate([pluck(f, cents=cents) for f in frequencies])


def measure(signal, rate: int = RATE):
    return analyse(signal, rate)


# --------------------------------------------------------------------------
# naming a frequency
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frequency,expected",
    [
        (440.0, "A4"),
        (82.41, "E2"),
        (73.42, "D2"),
        (65.41, "C2"),
        (329.63, "E4"),
        (261.63, "C4"),
    ],
)
def test_a_frequency_gets_its_note(frequency, expected):
    name, cents = note_name(frequency)
    assert name == expected
    assert abs(cents) < 1.0, f"{frequency} Hz read as {cents:+.1f} cents off {name}"


def test_a_note_between_two_notes_reports_the_distance():
    """Halfway up a semitone is fifty cents from both neighbours."""
    halfway = 440.0 * (2 ** (0.5 / 12))
    _, cents = note_name(halfway)
    assert abs(abs(cents) - 50.0) < 1.0


def test_silence_has_no_note():
    assert note_name(0.0) == ("?", 0.0)


# --------------------------------------------------------------------------
# measuring a take
# --------------------------------------------------------------------------


def test_a_tuned_take_reads_as_being_at_concert_pitch():
    report = measure(riff(E2, A2, D3, G3, E2, A2))

    assert report.measured
    assert report.in_concert_pitch
    assert abs(report.cents_offset) < CENTS_TOLERANCE
    assert report.concerns() == []


def test_a_flat_take_says_how_flat_and_why_it_matters():
    report = measure(riff(E2, A2, D3, G3, E2, A2, cents=-30))

    assert report.measured
    assert not report.in_concert_pitch
    assert report.cents_offset == pytest.approx(-30, abs=6)

    note = " ".join(report.concerns())
    assert "flat" in note
    assert "reads that as a tone difference" in note


def test_a_sharp_take_is_called_sharp():
    report = measure(riff(E2, A2, D3, cents=+35))
    assert "sharp" in " ".join(report.concerns())


def test_the_lowest_note_is_the_lowest_played():
    assert measure(riff(E2, A2, D3, G3)).lowest_note == "E2"
    assert measure(riff(73.42, A2, D3, G3)).lowest_note == "D2"


def test_the_lowest_note_suggests_the_tunings_it_fits():
    dropped = measure(riff(73.42, A2, D3, G3))
    assert "drop D" in dropped.candidate_tunings()
    assert "E standard" not in dropped.candidate_tunings()

    standard = measure(riff(E2, A2, D3, G3))
    assert standard.candidate_tunings() == ["E standard"]


def test_a_stated_tuning_that_does_not_match_is_queried_not_asserted():
    """A lower string may simply not have been played, and saying otherwise
    would be a confident guess about somebody's guitar."""
    report = measure(riff(73.42, A2, D3, G3))
    note = " ".join(report.concerns(stated_tuning="E standard"))

    assert "lowest note here is D2" in note
    assert "not proof of a wrong label" in note


def test_a_stated_tuning_that_matches_says_nothing():
    report = measure(riff(E2, A2, D3, G3))
    assert report.concerns(stated_tuning="E standard") == []


def test_an_unknown_tuning_name_is_not_argued_with():
    """Somebody's private tuning is not a mislabel."""
    report = measure(riff(E2, A2, D3))
    assert report.concerns(stated_tuning="Nick Drake C G C F C E") == []


# --------------------------------------------------------------------------
# refusing to guess
# --------------------------------------------------------------------------


def test_silence_measures_nothing():
    report = measure(np.zeros(RATE * 2))
    assert not report.measured
    assert report.concerns() == []
    assert "not enough pitched material" in report.describe()


def test_noise_measures_nothing_worth_reporting():
    """Autocorrelation on noise finds a lag; clarity is what stops it being
    reported as a note."""
    rng = np.random.default_rng(0)
    report = measure(rng.standard_normal(RATE * 2) * 0.3)
    assert report.concerns() == [] or report.notes < 5


def test_a_clip_shorter_than_a_window_measures_nothing():
    assert not measure(np.zeros(64)).measured


def test_an_empty_report_has_nothing_to_say():
    report = PitchReport()
    assert not report.measured
    assert report.candidate_tunings() == []
    assert report.in_concert_pitch, "unknown is not a fault"


def test_a_stereo_recording_is_folded_before_measuring():
    mono = riff(E2, A2, D3)
    stereo = np.stack([mono, mono], axis=1)
    assert measure(stereo).lowest_note == "E2"


def test_the_report_serialises_for_the_page():
    payload = measure(riff(E2, A2, D3)).to_dict()
    assert payload["measured"] is True
    assert payload["lowest_note"] == "E2"
    assert payload["candidate_tunings"] == ["E standard"]
    assert "concert pitch" in payload["describe"]


# --------------------------------------------------------------------------
# on a file
# --------------------------------------------------------------------------


def test_a_long_file_is_sampled_rather_than_read_whole(tmp_path):
    """A five-minute file should not be a five-minute wait."""
    sf = pytest.importorskip("soundfile")
    from rigsmith.match.pitch import MAX_ANALYSIS_SECONDS, analyse_file

    long_take = np.tile(riff(E2, A2, D3, G3), 40)
    path = tmp_path / "long.wav"
    sf.write(str(path), long_take.astype("float32"), RATE, subtype="FLOAT")
    assert len(long_take) / RATE > MAX_ANALYSIS_SECONDS

    report = analyse_file(path)
    assert report.measured
    assert report.lowest_note == "E2"

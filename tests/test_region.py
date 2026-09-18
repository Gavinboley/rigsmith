"""Tests for region selection.

Scoring a whole track averages its clean intro together with its solo, so
narrowing the comparison to one passage is the cheapest accuracy improvement
in the tool. That makes it worth being strict about: a region that silently
selects the wrong span, or one that shifts the two sides of an aligned
comparison against each other, would make the numbers look more trustworthy
while being less so.
"""

from __future__ import annotations

import numpy as np
import pytest

from rigsmith.core.errors import Invalid
from rigsmith.core.matching import MIN_REGION_SECONDS, Region

SR = 48000


@pytest.fixture()
def audio():
    """Ten seconds whose amplitude says which second you are looking at."""
    seconds = [np.full(SR, float(index + 1)) for index in range(10)]
    return np.concatenate(seconds)


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------


def test_parse_accepts_the_forms_the_three_surfaces_use():
    assert Region.parse(None) is None
    assert Region.parse("") is None
    assert Region.parse("1.5:9") == Region(1.5, 9.0)
    assert Region.parse([2, 4]) == Region(2.0, 4.0)
    assert Region.parse(Region(0, 1)) == Region(0.0, 1.0)


def test_open_ended_region_runs_to_the_end_of_the_file():
    assert Region.parse("30:").end is None


def test_region_without_a_colon_is_refused_with_the_format():
    with pytest.raises(Invalid) as caught:
        Region.parse("41.2")
    assert "start:end" in caught.value.hint


def test_backwards_region_is_refused():
    with pytest.raises(Invalid, match="at or before it starts"):
        Region(9.0, 3.0)


def test_negative_start_is_refused():
    with pytest.raises(Invalid, match="before the file begins"):
        Region(-1.0, 3.0)


def test_region_shorter_than_a_measurement_is_refused():
    with pytest.raises(Invalid, match="too short to measure"):
        Region(1.0, 1.0 + MIN_REGION_SECONDS / 2)


# --------------------------------------------------------------------------
# slicing
# --------------------------------------------------------------------------


def test_slice_takes_the_span_it_names(audio):
    cut = Region(3.0, 5.0).slice(audio, SR)
    assert len(cut) == 2 * SR
    # Seconds 4 and 5 of the fixture, by amplitude.
    assert cut[0] == 4.0
    assert cut[-1] == 5.0


def test_open_ended_slice_runs_to_the_end(audio):
    cut = Region(8.0).slice(audio, SR)
    assert len(cut) == 2 * SR
    assert cut[-1] == 10.0


def test_slice_past_the_end_is_refused_rather_than_returning_nothing(audio):
    """An empty array would score as a silent candidate, which looks like data."""
    with pytest.raises(Invalid, match="selects nothing"):
        Region(30.0, 40.0).slice(audio, SR, label="the target")


def test_slice_clipped_to_a_sliver_is_refused(audio):
    with pytest.raises(Invalid, match="once clipped to the file"):
        Region(9.9, 12.0).slice(audio, SR)


def test_slice_beyond_the_end_is_clipped_when_enough_remains(audio):
    cut = Region(8.0, 20.0).slice(audio, SR)
    assert len(cut) == 2 * SR


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


def _write(path, audio, sample_rate=SR):
    sf = pytest.importorskip("soundfile")
    sf.write(str(path), audio.astype("float32"), sample_rate)
    return path


@pytest.fixture()
def clip_files(tmp_path):
    """A target whose two halves are different tones, and one candidate each."""
    t = np.arange(4 * SR) / SR
    clean = np.sin(2 * np.pi * 196 * t)
    bright = np.tanh(clean * 8.0)  # heavily clipped: lots of harmonics
    dark = clean * 0.6  # nearly a sine: very few

    target = np.concatenate([dark[: 2 * SR], bright[: 2 * SR]])
    return {
        "target": _write(tmp_path / "target.wav", target),
        "bright": _write(tmp_path / "bright.wav", bright),
        "dark": _write(tmp_path / "dark.wav", dark),
    }


def test_the_region_decides_which_candidate_wins(clip_files):
    """The point of the feature: a different passage, a different answer."""
    from rigsmith.core.matching import score

    candidates = {"bright": clip_files["bright"], "dark": clip_files["dark"]}

    first_half = score(clip_files["target"], candidates, target_region="0:1.9")
    second_half = score(clip_files["target"], candidates, target_region="2.1:4")

    assert first_half[0].label == "dark"
    assert second_half[0].label == "bright"


def test_scoring_the_whole_file_is_still_the_default(clip_files):
    from rigsmith.core.matching import score

    scores = score(clip_files["target"], {"bright": clip_files["bright"]})
    assert scores[0].label == "bright"


def test_aligned_mode_refuses_differing_regions(clip_files):
    """Trimming the sides differently shifts one against the other."""
    from rigsmith.core.matching import score

    with pytest.raises(Invalid) as caught:
        score(
            clip_files["target"],
            {"bright": clip_files["bright"]},
            mode="aligned",
            target_region="0:2",
            candidate_region="1:3",
        )
    assert "same region on both sides" in str(caught.value)
    assert "distribution" in caught.value.hint


def test_aligned_mode_accepts_one_region_applied_to_both(clip_files):
    from rigsmith.core.matching import score

    scores = score(
        clip_files["target"],
        {"bright": clip_files["bright"]},
        mode="aligned",
        target_region="0:2",
        candidate_region="0:2",
    )
    assert scores[0].label == "bright"


# --------------------------------------------------------------------------
# picking the passage without making anyone find it
# --------------------------------------------------------------------------


def _song(path, rate=SR):
    """Thirty seconds: silence, quiet noodling, the riff, silence."""
    sf = pytest.importorskip("soundfile")
    t = np.arange(rate * 30) / rate
    signal = np.zeros_like(t)
    signal[4 * rate : 10 * rate] = np.sin(2 * np.pi * 196 * t[: 6 * rate]) * 0.08
    signal[14 * rate : 24 * rate] = np.sin(2 * np.pi * 196 * t[: 10 * rate]) * 0.8
    sf.write(str(path), signal.astype("float32"), rate, subtype="FLOAT")
    return path


def test_the_busiest_passage_is_the_one_chosen(tmp_path):
    """Scrubbing a waveform and reading a clock is a thing people do once."""
    from rigsmith.core.matching import suggest_region

    region = suggest_region(_song(tmp_path / "song.wav"))

    assert region is not None
    assert 13.0 <= region.start <= 16.0, region.describe()
    assert region.end <= 24.5


def test_a_file_shorter_than_the_window_needs_no_region(tmp_path):
    """Not a failure - the whole thing already is the passage."""
    sf = pytest.importorskip("soundfile")
    from rigsmith.core.matching import suggest_region

    path = tmp_path / "short.wav"
    sf.write(str(path), np.zeros(SR * 3, dtype="float32"), SR, subtype="FLOAT")
    assert suggest_region(path) is None


def test_auto_resolves_against_the_file_it_applies_to(tmp_path):
    from rigsmith.core.matching import resolve_region, suggest_region

    song = _song(tmp_path / "song.wav")
    assert resolve_region("auto", song) == suggest_region(song)
    assert resolve_region("AUTO", song) is not None
    assert resolve_region("1:2", song) == Region(1.0, 2.0)
    assert resolve_region(None, song) is None


def test_auto_picks_a_different_passage_for_each_candidate(tmp_path):
    """Resolving it once and applying that to everything would compare the
    wrong seconds of every candidate after the first."""
    sf = pytest.importorskip("soundfile")
    from rigsmith.core.matching import score

    rate = SR
    t = np.arange(rate * 30) / rate
    tone = np.sin(2 * np.pi * 196 * t[: 10 * rate])

    early = np.zeros_like(t)
    early[: 10 * rate] = tone
    late = np.zeros_like(t)
    late[20 * rate : 30 * rate] = tone

    for name, signal in (("early.wav", early), ("late.wav", late)):
        sf.write(str(tmp_path / name), signal.astype("float32"), rate, subtype="FLOAT")

    # Both candidates hold the same ten seconds of audio in different places.
    # With per-candidate auto they both find it and score identically.
    scores = score(
        tmp_path / "early.wav",
        {"early": tmp_path / "early.wav", "late": tmp_path / "late.wav"},
        target_region="auto",
        candidate_region="auto",
    )
    composites = {s.label: s.composite for s in scores}
    assert composites["late"] == pytest.approx(composites["early"], abs=0.02), composites

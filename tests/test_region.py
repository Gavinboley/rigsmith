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

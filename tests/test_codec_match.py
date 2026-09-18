"""Comparing a lossless candidate against a lossy target.

This is the normal case, not an edge one: the target is an mp3 of the record
and the candidates are wavs you rendered. Measured naively it does not merely
add noise - on a three-way comparison it *inverted* the ranking, putting the
candidate that was byte-identical to the target last, because the codec's
error was larger than the difference between the candidates and was not the
same for each of them.

Putting both sides through the same encoder makes that error common to both,
so it cancels.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")

import soundfile as sf  # noqa: E402
from scipy.signal import butter, lfilter  # noqa: E402

from rigsmith import config  # noqa: E402
from rigsmith.core import matching  # noqa: E402
from rigsmith.match.source import matched_to, readable_formats  # noqa: E402

RATE = 48000

pytestmark = pytest.mark.skipif(
    "MP3" not in readable_formats(), reason="this libsndfile has no mp3 codec"
)


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def part() -> np.ndarray:
    """Continuous, distorted, broadband - the shape of a real guitar part."""
    rng = np.random.default_rng(3)
    t = np.arange(RATE * 8) / RATE
    notes = sum(np.sin(2 * np.pi * f * t) for f in (98, 147, 196, 294))
    return np.tanh(notes * 2.0) + 0.002 * rng.standard_normal(len(t))


def variant(*, bright: float = 0.0, dark: float = 0.0) -> np.ndarray:
    base = part()
    signal = base.copy()
    if bright:
        signal = signal + bright * lfilter(
            *butter(2, 3500 / (RATE / 2), btype="high"), base
        )
    if dark:
        signal = signal + dark * lfilter(
            *butter(2, 1500 / (RATE / 2), btype="low"), base
        )
    return (signal / np.abs(signal).max() * 0.75).astype("float32")


@pytest.fixture()
def guitars(workspace):
    """Three instruments, and the record - which is one of them, as an mp3."""
    directory = workspace / "audio"
    directory.mkdir(parents=True, exist_ok=True)

    signals = {
        "tele": variant(bright=0.8),
        "lespaul": variant(dark=0.8),
        "strat": variant(),
    }
    for name, signal in signals.items():
        sf.write(str(directory / f"{name}.wav"), signal, RATE, subtype="FLOAT")

    sf.write(str(directory / "record.mp3"), signals["tele"], RATE, format="MP3")
    sf.write(str(directory / "record.wav"), signals["tele"], RATE, subtype="FLOAT")
    return directory


def order(target, directory):
    scores = matching.score(
        target, {name: directory / f"{name}.wav" for name in ("tele", "lespaul", "strat")}
    )
    return [score.label for score in scores]


# --------------------------------------------------------------------------
# the ranking survives the codec
# --------------------------------------------------------------------------


def test_an_mp3_target_ranks_the_same_as_a_lossless_one(guitars):
    """The whole point. Untreated, this comparison put the right answer last."""
    lossless = order(guitars / "record.wav", guitars)
    lossy = order(guitars / "record.mp3", guitars)

    assert lossless[0] == "tele", "the identical candidate has to win outright"
    assert lossy == lossless, f"mp3 target reordered: {lossy} vs {lossless}"


def test_the_re_encoding_is_reported_rather_than_done_quietly(guitars):
    scores = matching.score(
        guitars / "record.mp3", {"tele": guitars / "tele.wav"}
    )
    note = " ".join(scores[0].notes)
    assert "re-encoded as MP3" in note
    assert "measures the encoder as well as the tone" in note


def test_nothing_is_done_when_both_sides_already_match(guitars):
    scores = matching.score(
        guitars / "record.wav", {"tele": guitars / "tele.wav"}
    )
    assert not any("re-encoded" in note for note in scores[0].notes)


def test_a_lossy_candidate_against_a_lossy_target_is_left_alone(guitars):
    """Both already carry an encoder's error; adding another would not help."""
    path, note = matched_to(guitars / "record.mp3", guitars / "record.mp3")
    assert path == guitars / "record.mp3"
    assert note is None


def test_a_lossless_target_never_triggers_it(guitars):
    """Encoding the candidate would be adding an error, not cancelling one."""
    path, note = matched_to(guitars / "tele.wav", guitars / "record.wav")
    assert path == guitars / "tele.wav"
    assert note is None


# --------------------------------------------------------------------------
# the mechanics
# --------------------------------------------------------------------------


def test_the_encoded_copy_lands_in_the_workspace(guitars):
    path, _ = matched_to(guitars / "tele.wav", guitars / "record.mp3")

    assert path != guitars / "tele.wav"
    assert path.suffix == ".mp3"
    assert path.parent == config.get_settings().audio_dir / "encoded"
    assert path.is_file()


def test_the_copy_is_made_once_and_reused(guitars):
    first, _ = matched_to(guitars / "tele.wav", guitars / "record.mp3")
    written = first.stat().st_mtime_ns

    second, _ = matched_to(guitars / "tele.wav", guitars / "record.mp3")
    assert second == first
    assert second.stat().st_mtime_ns == written, "it re-encoded the same file"


def test_a_changed_candidate_gets_a_new_copy(guitars):
    first, _ = matched_to(guitars / "tele.wav", guitars / "record.mp3")

    sf.write(str(guitars / "tele.wav"), variant(dark=0.8), RATE, subtype="FLOAT")
    from rigsmith.match.source import forget_cached

    forget_cached()

    second, _ = matched_to(guitars / "tele.wav", guitars / "record.mp3")
    assert second != first, "a stale encode would compare the wrong audio"


def test_read_only_mode_compares_unmatched_and_says_so(guitars, monkeypatch):
    """Refusing the comparison would be worse than doing it with a caveat."""
    monkeypatch.setenv("RIGSMITH_READONLY", "1")
    config.set_settings(config.Settings())

    path, note = matched_to(guitars / "tele.wav", guitars / "record.mp3")
    assert path == guitars / "tele.wav"
    assert "Read-only mode" in note
    assert "codec's own error is in this comparison" in note


def test_an_unreadable_file_is_left_for_the_real_error_to_report(guitars):
    missing = guitars / "gone.wav"
    path, note = matched_to(missing, guitars / "record.mp3")
    assert path == missing and note is None


def test_band_comparison_matches_the_codec_too(guitars):
    """Otherwise the bands would describe the encoder's work, not the amp's."""
    same = matching.compare_bands(guitars / "record.mp3", guitars / "tele.wav")
    assert not any(band.audible for band in same), (
        "a candidate identical to the target should differ nowhere: "
        + matching.render_bands(same)
    )

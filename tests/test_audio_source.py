"""Reading whatever somebody actually has, and saying what it costs.

An mp3 of the record, a loop played into the pedal, a phone memo - all of it
is legitimate input, and none of it is equally trustworthy. These tests cover
both halves: read it if it can be read, and be specific about the
consequences when it matters.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

import soundfile as sf  # noqa: E402

from rigsmith.core.errors import Invalid, NotFound  # noqa: E402
from rigsmith.match.source import (  # noqa: E402
    AudioSource,
    concerns_for,
    inspect_audio,
    load,
    readable_formats,
    readable_suffixes,
)

RATE = 48000


def tone(seconds: float = 1.0, rate: int = RATE) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    rng = np.random.default_rng(0)
    return (
        np.sin(2 * np.pi * 196 * t)
        + 0.4 * np.sin(2 * np.pi * 392 * t)
        + 0.03 * rng.standard_normal(len(t))
    )


def write(path: Path, signal: np.ndarray, rate: int = RATE, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), signal.astype("float32"), rate, **kwargs)
    return path


def has_mp3() -> bool:
    return "MP3" in readable_formats()


# --------------------------------------------------------------------------
# what this install can read
# --------------------------------------------------------------------------


def test_the_readable_list_comes_from_the_library_not_a_guess():
    """libsndfile gained mp3 in 1.1, so the same rigsmith reads mp3 on one
    machine and not another. A hardcoded list would be a lie on one of them."""
    formats = readable_formats()
    assert "WAV" in formats
    suffixes = readable_suffixes()
    assert ".wav" in suffixes
    assert all(s.startswith(".") for s in suffixes)
    # libsndfile never decodes m4a; FFmpeg through PyAV does, when installed.
    from rigsmith.match.source import _have_av

    assert (".m4a" in suffixes) == _have_av()


@pytest.mark.skipif(not has_mp3(), reason="this libsndfile has no mp3 decoder")
def test_mp3_is_offered_when_it_can_be_read():
    assert ".mp3" in readable_suffixes()


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


@pytest.mark.skipif(not has_mp3(), reason="this libsndfile has no mp3 decoder")
def test_an_mp3_reads_back_at_the_right_length_and_rate(tmp_path):
    original = tone(2.0)
    path = write(tmp_path / "riff.mp3", original, format="MP3")

    samples, rate = load(path)
    assert rate == RATE
    # libsndfile handles the encoder's delay and padding, so a decoded mp3
    # lines up with what went in - which is what makes it usable at all.
    assert abs(len(samples) - len(original)) <= 2
    assert np.abs(samples).max() > 0.1


def test_an_ogg_reads_back(tmp_path):
    if "OGG" not in readable_formats():
        pytest.skip("this libsndfile has no vorbis decoder")
    path = write(tmp_path / "riff.ogg", tone(1.0), format="OGG")
    samples, rate = load(path)
    assert rate == RATE and len(samples) > 1000


def test_a_flac_reads_back(tmp_path):
    path = write(tmp_path / "riff.flac", tone(1.0), format="FLAC")
    samples, rate = load(path)
    assert rate == RATE and len(samples) > 1000


def test_a_stereo_file_is_averaged_to_mono(tmp_path):
    mono = tone(1.0)
    stereo = np.stack([mono, mono * 0.5], axis=1)
    path = write(tmp_path / "stereo.wav", stereo, subtype="FLOAT")

    samples, _ = load(path)
    assert samples.ndim == 1
    assert samples == pytest.approx(mono * 0.75, abs=1e-5)


# --------------------------------------------------------------------------
# refusing, usefully
# --------------------------------------------------------------------------


def test_a_format_libsndfile_cannot_decode_names_what_it_can(tmp_path):
    path = tmp_path / "voice memo.m4a"
    path.write_bytes(b"\x00\x00\x00 ftypM4A ")

    with pytest.raises(Invalid) as caught:
        load(path)

    assert "voice memo.m4a" in caught.value.message
    assert ".wav" in caught.value.hint, "it has to say what does work"
    from rigsmith.match.source import _have_av

    if not _have_av():
        # Without FFmpeg it says how to get it, or how to convert.
        assert "m4a" in caught.value.hint and "codecs" in caught.value.hint


def test_a_missing_file_is_not_found_not_invalid(tmp_path):
    with pytest.raises(NotFound):
        load(tmp_path / "nothing.wav")
    with pytest.raises(NotFound):
        inspect_audio(tmp_path / "nothing.wav")


def test_a_wav_that_is_not_a_wav_is_refused_with_its_name(tmp_path):
    path = write(tmp_path / "fine.wav", tone(0.2), subtype="FLOAT")
    path.write_bytes(b"this is not audio at all")

    with pytest.raises(Invalid, match="fine.wav"):
        load(path)


# --------------------------------------------------------------------------
# what is worth saying about a file
# --------------------------------------------------------------------------


def test_a_clean_forty_eight_kilohertz_take_has_nothing_to_report(tmp_path):
    """Saying something has to mean something."""
    path = write(tmp_path / "clean.wav", tone(1.0) * 0.5, subtype="FLOAT")
    source = inspect_audio(path)

    assert source.concerns() == []
    assert not source.lossy and not source.clipped and not source.quiet
    assert "48000 Hz" in source.describe()


@pytest.mark.skipif(not has_mp3(), reason="this libsndfile has no mp3 decoder")
def test_lossy_is_a_bigger_problem_as_a_di_than_as_a_target(tmp_path):
    """Chasing an mp3 of a record is normal. Reamping through one prints its
    artefacts into every candidate equally, and then they get measured."""
    path = write(tmp_path / "riff.mp3", tone(1.0) * 0.5, format="MP3")
    source = inspect_audio(path)

    assert source.lossy
    assert source.concerns() == [], "as a plain target there is nothing to say"

    as_di = " ".join(source.concerns(as_di=True))
    assert "worth avoiding" in as_di
    assert "every candidate" in as_di

    aligned = " ".join(source.concerns(aligned=True))
    assert "sample by sample" in aligned


def test_a_low_sample_rate_says_what_is_missing(tmp_path):
    path = write(tmp_path / "old.wav", tone(1.0, 22050) * 0.5, rate=22050, subtype="FLOAT")
    note = " ".join(inspect_audio(path).concerns())

    assert "22050 Hz" in note
    assert "11025 Hz" in note, "it has to say where the audio stops"
    assert "bite and fizz" in note, "and which comparisons that breaks"


@pytest.mark.parametrize("rate", [44100, 48000, 96000])
def test_an_ordinary_sample_rate_is_not_complained_about(tmp_path, rate):
    """44.1k is a CD and most mp3s of records. Complaining about every one
    of them is noise, and noise trains people to ignore the real notes."""
    path = write(tmp_path / "cd.wav", tone(1.0, rate) * 0.5, rate=rate, subtype="FLOAT")
    assert inspect_audio(path).concerns() == []


def test_clipping_is_a_flat_top_not_a_loud_peak(tmp_path):
    """A driven guitar grazes full scale constantly without being clipped.
    Counting every sample that touches 1.0 fires on exactly the material this
    tool exists for."""
    driven = np.tanh(tone(1.0) * 4)
    driven = driven / np.abs(driven).max() * 0.95
    assert not inspect_audio(write(tmp_path / "driven.wav", driven, subtype="FLOAT")).clipped

    clipped = np.clip(tone(1.0) * 4, -1, 1)
    source = inspect_audio(write(tmp_path / "clipped.wav", clipped, subtype="FLOAT"))
    assert source.clipped
    assert "distortion is the thing these metrics measure" in " ".join(source.concerns())


def test_one_transient_at_full_scale_is_not_clipping(tmp_path):
    signal = tone(2.0) * 0.4
    signal[1000:1006] = 0.999
    assert not inspect_audio(write(tmp_path / "spike.wav", signal, subtype="FLOAT")).clipped


def test_a_very_quiet_take_is_mentioned_without_being_refused(tmp_path):
    path = write(tmp_path / "quiet.wav", tone(1.0) * 0.01, subtype="FLOAT")
    source = inspect_audio(path)

    assert source.quiet
    assert "noise floor" in " ".join(source.concerns())
    assert load(path)[0].size, "quiet is a caveat, not a refusal"


# --------------------------------------------------------------------------
# collecting them for a comparison
# --------------------------------------------------------------------------


def test_concerns_are_collected_without_repeating(tmp_path):
    quiet = write(tmp_path / "a.wav", tone(1.0) * 0.01, subtype="FLOAT")
    notes = concerns_for([quiet, quiet, quiet])
    assert len(notes) == 1


def test_an_unreadable_file_in_the_set_does_not_break_the_report(tmp_path):
    """The score itself will fail on it; this is only the commentary."""
    good = write(tmp_path / "good.wav", tone(1.0) * 0.5, subtype="FLOAT")
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"nope")

    assert concerns_for([good, bad]) == []


def test_the_di_is_singled_out(tmp_path):
    if not has_mp3():
        pytest.skip("this libsndfile has no mp3 decoder")
    di = write(tmp_path / "di.mp3", tone(1.0) * 0.5, format="MP3")
    target = write(tmp_path / "target.mp3", tone(1.0) * 0.5, format="MP3")

    notes = " ".join(concerns_for([di, target], di_paths=[di]))
    assert "di.mp3" in notes and "worth avoiding" in notes
    assert "target.mp3" not in notes, "a lossy target is normal and unremarkable"


def test_a_source_serialises_for_the_page(tmp_path):
    path = write(tmp_path / "riff.wav", tone(1.5) * 0.5, subtype="FLOAT")
    payload = inspect_audio(path).to_dict()

    assert payload["duration"] == pytest.approx(1.5, abs=0.01)
    assert payload["sample_rate"] == RATE
    assert payload["channels"] == 1
    assert payload["lossy"] is False
    assert payload["concerns"] == []


def test_skipping_the_measurement_still_gives_the_shape(tmp_path):
    path = write(tmp_path / "riff.wav", tone(1.0) * 0.5, subtype="FLOAT")
    source = inspect_audio(path, measure=False)
    assert source.frames == RATE
    assert source.peak == 0.0, "not measured, and not pretending to be"


def test_a_source_with_no_samples_does_not_divide_by_zero():
    source = AudioSource(path=Path("empty.wav"), sample_rate=0, channels=1, frames=0)
    assert source.duration == 0.0
    assert source.concerns()


# --------------------------------------------------------------------------
# not reading the same file twenty times
# --------------------------------------------------------------------------


def test_a_repeated_inspection_is_not_a_repeated_read(tmp_path):
    """Scoring twenty candidates asks about the same files several times
    each - once to score, once for the commentary, once for the bands."""
    from rigsmith.match.source import forget_cached, inspect_audio

    forget_cached()
    path = write(tmp_path / "riff.wav", tone(2.0) * 0.5, subtype="FLOAT")

    first = inspect_audio(path)
    again = inspect_audio(path)
    assert again is first, "the second inspection should be the first one"


def test_a_file_edited_in_place_is_read_again(tmp_path):
    """The only way a remembered answer could be wrong."""
    import os
    import time

    from rigsmith.match.source import forget_cached, inspect_audio

    forget_cached()
    path = write(tmp_path / "riff.wav", tone(1.0) * 0.5, subtype="FLOAT")
    loud = inspect_audio(path).peak
    assert loud > 0.3

    write(tmp_path / "riff.wav", tone(1.0) * 0.02, subtype="FLOAT")
    later = time.time() + 2
    os.utime(path, (later, later))

    assert inspect_audio(path).peak < loud / 4


def test_measured_and_unmeasured_are_remembered_separately(tmp_path):
    from rigsmith.match.source import forget_cached, inspect_audio

    forget_cached()
    path = write(tmp_path / "riff.wav", tone(1.0) * 0.5, subtype="FLOAT")

    assert inspect_audio(path, measure=False).peak == 0.0
    assert inspect_audio(path, measure=True).peak > 0.0, "not the cheap answer"

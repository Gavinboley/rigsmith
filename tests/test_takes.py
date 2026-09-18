"""The library of your own playing.

A folder of wav files is not a library. What makes this one is that each
recording carries the instrument that made it - so a capture that scores well
scores well through your pickups, and six months later you still know which
guitar that was.
"""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

import soundfile as sf  # noqa: E402

from rigsmith import config  # noqa: E402
from rigsmith.core import takes  # noqa: E402
from rigsmith.core.errors import Conflict, Invalid, NotFound, ReadOnly  # noqa: E402
from rigsmith.match.source import readable_formats  # noqa: E402

RATE = 48000


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def _seed(name: str) -> int:
    """A stable seed per filename.

    Two takes of the same riff are never byte-identical in real life, and the
    library refuses an exact duplicate on purpose - so fixtures that differ
    only by filename would be testing that refusal by accident.
    """
    return abs(hash(name)) % (2**31)


def recording(directory, name="riff.wav", seconds=2.0, **kwargs):
    t = np.arange(int(seconds * RATE)) / RATE
    rng = np.random.default_rng(_seed(name))
    signal = (
        np.sin(2 * np.pi * 196 * t) * 0.4 + 0.01 * rng.standard_normal(len(t))
    ).astype("float32")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    sf.write(str(path), signal, RATE, **(kwargs or {"subtype": "FLOAT"}))
    return path


# --------------------------------------------------------------------------
# adding
# --------------------------------------------------------------------------


def test_a_take_carries_the_instrument_that_made_it(workspace):
    source = recording(workspace / "src")
    take = takes.add(
        source,
        kind="di",
        origin="usb",
        guitar="Les Paul Standard",
        pickup="bridge",
        tuning="E standard",
        note="the riff, twice",
    )

    assert take.take_id == "riff"
    assert take.guitar == "Les Paul Standard"
    assert take.duration == pytest.approx(2.0, abs=0.01)
    assert take.sample_rate == RATE
    assert "Les Paul Standard bridge" in take.describe()


def test_the_file_is_brought_into_the_workspace(workspace):
    """A library pointing at files on a drive you unplug is not a library."""
    source = recording(workspace / "src")
    take = takes.add(source, guitar="Strat")

    assert take.file.parent == config.get_settings().takes_dir
    assert take.file.is_file()
    assert source.is_file(), "the original is copied, not moved"


def test_it_can_be_told_to_leave_the_file_alone(workspace):
    source = recording(workspace / "src")
    take = takes.add(source, guitar="Strat", copy=False)
    assert take.file == source


def test_a_take_is_read_before_it_is_filed(workspace):
    """One that turns out to be unreadable is worse than one never added -
    it sits in the library looking usable until you reach for it."""
    broken = workspace / "src" / "broken.wav"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b"not audio")

    with pytest.raises(Invalid):
        takes.add(broken)
    assert takes.list_takes() == []


def test_two_takes_cannot_share_a_name(workspace):
    """Different recordings, same name - which is a different complaint from
    the same recording twice."""
    first = recording(workspace / "src", "monday.wav")
    second = recording(workspace / "src", "tuesday.wav")
    takes.add(first, take_id="the-riff")

    with pytest.raises(Conflict, match="already exists"):
        takes.add(second, take_id="the-riff")


def test_an_unknown_kind_lists_the_real_ones(workspace):
    source = recording(workspace / "src")
    with pytest.raises(Invalid) as caught:
        takes.add(source, kind="vibes")
    assert "di" in caught.value.hint and "loop" in caught.value.hint


def test_adding_is_refused_in_read_only_mode(workspace, monkeypatch):
    source = recording(workspace / "src")
    monkeypatch.setenv("RIGSMITH_READONLY", "1")
    config.set_settings(config.Settings())
    with pytest.raises(ReadOnly):
        takes.add(source)


# --------------------------------------------------------------------------
# what a take is good for
# --------------------------------------------------------------------------


def test_only_a_di_can_be_reamped(workspace):
    """Rendering a wet take through an amp model stacks two amps."""
    recording(workspace / "src", "dry.wav")
    recording(workspace / "src", "wet.wav")
    recording(workspace / "src", "loop.wav")

    assert takes.add(workspace / "src" / "dry.wav", kind="di").reamps
    assert not takes.add(workspace / "src" / "wet.wav", kind="wet").reamps
    assert not takes.add(workspace / "src" / "loop.wav", kind="loop").reamps


def test_a_lossy_di_is_flagged_when_it_is_added(workspace):
    if "MP3" not in readable_formats():
        pytest.skip("this libsndfile has no mp3 encoder")
    source = recording(workspace / "src", "riff.mp3", format="MP3")
    take = takes.add(source, kind="di", guitar="Strat")

    assert take.lossy
    assert any("worth avoiding" in note for note in take.concerns)


def test_a_lossy_reference_is_not_flagged(workspace):
    """Chasing an mp3 of a record is the normal case, not a mistake."""
    if "MP3" not in readable_formats():
        pytest.skip("this libsndfile has no mp3 encoder")
    source = recording(workspace / "src", "record.mp3", format="MP3")
    take = takes.add(source, kind="reference")

    assert take.lossy
    assert take.concerns == []


# --------------------------------------------------------------------------
# finding them again
# --------------------------------------------------------------------------


def test_a_take_id_resolves_to_its_file(workspace):
    source = recording(workspace / "src")
    take = takes.add(source, take_id="cherub riff", guitar="Strat")

    assert take.take_id == "cherub-riff"
    assert takes.resolve("cherub-riff") == take.file
    assert takes.resolve("Cherub Riff") == take.file, "slugified, so typing is forgiving"


def test_a_path_still_resolves_to_itself(workspace):
    source = recording(workspace / "src")
    assert takes.resolve(source) == source


def test_a_typo_suggests_the_take_it_meant(workspace):
    recording(workspace / "src", "cherub.wav")
    takes.add(workspace / "src" / "cherub.wav")

    with pytest.raises(NotFound) as caught:
        takes.resolve("cherubb")
    assert "Did you mean" in caught.value.hint
    assert "cherub" in caught.value.hint


def test_something_unrelated_lists_what_there_is(workspace):
    recording(workspace / "src", "cherub.wav")
    takes.add(workspace / "src" / "cherub.wav")

    with pytest.raises(NotFound) as caught:
        takes.resolve("zzzzz")
    assert "The library holds" in caught.value.hint


def test_a_take_whose_file_has_gone_says_so(workspace):
    source = recording(workspace / "src")
    take = takes.add(source, guitar="Strat")
    take.file.unlink()

    with pytest.raises(NotFound, match="which is gone"):
        takes.resolve(take.take_id)
    assert "MISSING" in takes.get(take.take_id).describe()


def test_nothing_at_all_is_refused(workspace):
    with pytest.raises(Invalid):
        takes.resolve("")


# --------------------------------------------------------------------------
# the default DI
# --------------------------------------------------------------------------


def test_the_default_di_can_be_asked_for_by_guitar(workspace):
    recording(workspace / "src", "lp.wav")
    recording(workspace / "src", "strat.wav")
    takes.add(workspace / "src" / "lp.wav", kind="di", guitar="Les Paul")
    takes.add(workspace / "src" / "strat.wav", kind="di", guitar="Strat")

    assert takes.default_di("Strat").guitar == "Strat"
    assert takes.default_di("Les Paul").guitar == "Les Paul"
    assert takes.default_di("Telecaster") is None


def test_a_wet_take_is_never_the_default_di(workspace):
    """Reamping through one would stack two amps."""
    recording(workspace / "src", "wet.wav")
    takes.add(workspace / "src" / "wet.wav", kind="wet", guitar="Strat")
    assert takes.default_di() is None


def test_a_missing_file_is_never_the_default_di(workspace):
    source = recording(workspace / "src")
    take = takes.add(source, kind="di", guitar="Strat")
    take.file.unlink()
    assert takes.default_di() is None


# --------------------------------------------------------------------------
# editing and forgetting
# --------------------------------------------------------------------------


def test_labels_can_be_corrected(workspace):
    source = recording(workspace / "src")
    take = takes.add(source)
    assert take.guitar is None

    updated = takes.update(take.take_id, guitar="Les Paul", pickup="neck")
    assert updated.guitar == "Les Paul" and updated.pickup == "neck"
    assert takes.get(take.take_id).guitar == "Les Paul", "and it persisted"


def test_the_audio_is_not_editable(workspace):
    source = recording(workspace / "src")
    take = takes.add(source)
    with pytest.raises(Invalid, match="cannot change"):
        takes.update(take.take_id, path="/somewhere/else.wav")


def test_forgetting_leaves_the_audio_alone_by_default(workspace):
    source = recording(workspace / "src")
    take = takes.add(source, guitar="Strat")

    message = takes.delete(take.take_id)
    assert "The file stays" in message
    assert take.file.is_file()
    assert takes.list_takes() == []


def test_forgetting_can_delete_the_audio_when_asked(workspace):
    source = recording(workspace / "src")
    take = takes.add(source, guitar="Strat")

    takes.delete(take.take_id, remove_file=True)
    assert not take.file.exists()
    assert source.is_file(), "only the library's own copy"


def test_it_never_deletes_a_file_it_did_not_bring_in(workspace):
    """These are recordings of somebody playing. Deleting one outside the
    library would be reaching into their filesystem."""
    source = recording(workspace / "src")
    take = takes.add(source, copy=False)

    message = takes.delete(take.take_id, remove_file=True)
    assert "outside the library, so it stays" in message
    assert source.is_file()


# --------------------------------------------------------------------------
# the listing
# --------------------------------------------------------------------------


def test_an_empty_library_explains_what_it_is_for(workspace):
    text = takes.render_list(takes.list_takes())
    assert "your own playing" in text
    assert "rather than through a stranger" in text


def test_the_listing_calls_out_takes_with_no_guitar(workspace):
    recording(workspace / "src", "a.wav")
    recording(workspace / "src", "b.wav")
    takes.add(workspace / "src" / "a.wav", guitar="Strat")
    takes.add(workspace / "src" / "b.wav")

    text = takes.render_list(takes.list_takes())
    assert "guitars: Strat" in text
    assert "no guitar recorded: b" in text


def test_takes_can_be_filtered(workspace):
    recording(workspace / "src", "a.wav")
    recording(workspace / "src", "b.wav")
    takes.add(workspace / "src" / "a.wav", kind="di", guitar="Strat")
    takes.add(workspace / "src" / "b.wav", kind="wet", guitar="Les Paul")

    assert [t.take_id for t in takes.list_takes(kind="di")] == ["a"]
    assert [t.take_id for t in takes.list_takes(guitar="les paul")] == ["b"]
    assert takes.guitars() == ["Les Paul", "Strat"]


# --------------------------------------------------------------------------
# using them without naming a file
# --------------------------------------------------------------------------


def test_a_sweep_falls_back_to_the_librarys_own_di(workspace):
    from rigsmith.core.matching import _di_or_default

    source = recording(workspace / "src")
    take = takes.add(source, kind="di", guitar="Strat")
    assert _di_or_default(None) == take.file


def test_with_no_di_at_all_it_says_how_to_make_one(workspace):
    from rigsmith.core.matching import _di_or_default

    with pytest.raises(Invalid) as caught:
        _di_or_default(None)
    assert "take add" in caught.value.hint
    assert "capture-di" in caught.value.hint


# --------------------------------------------------------------------------
# which guitar
# --------------------------------------------------------------------------


def _guitar(directory, name, *, dark=0.0, bright=0.0):
    """A DI with a deliberate tonal slant, so a ranking has something to find."""
    scipy_signal = pytest.importorskip("scipy.signal")

    t = np.arange(3 * RATE) / RATE
    rng = np.random.default_rng(_seed(name))
    # Harmonically rich on purpose. Two pure sines have nothing above 400 Hz
    # for a shelf to act on, so filtered copies of them come out identical
    # once levels are normalised - and the test would pass on a fixture that
    # proves nothing.
    base = np.tanh(
        (np.sin(2 * np.pi * 196 * t) + 0.4 * np.sin(2 * np.pi * 392 * t)) * 3
    ) + 0.05 * rng.standard_normal(len(t))
    signal = base.copy()
    if dark:
        signal += dark * scipy_signal.lfilter(
            *scipy_signal.butter(2, 2000 / (RATE / 2), btype="low"), base
        )
    if bright:
        signal += bright * scipy_signal.lfilter(
            *scipy_signal.butter(2, 3000 / (RATE / 2), btype="high"), base
        )
    signal = signal / np.abs(signal).max() * 0.7

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.wav"
    sf.write(str(path), signal.astype("float32"), RATE, subtype="FLOAT")
    return path


def test_the_instrument_closest_to_the_target_comes_first(workspace):
    """Same rig, same target, one variable - the one no capture fixes."""
    from rigsmith.core.matching import compare_guitars, render_guitar_comparison

    source = workspace / "src"
    takes.add(_guitar(source, "lp", dark=0.9), kind="di", guitar="Les Paul")
    takes.add(_guitar(source, "strat"), kind="di", guitar="Strat")
    bright = _guitar(source, "tele", bright=1.1)
    takes.add(bright, kind="di", guitar="Telecaster")

    results = compare_guitars(bright)
    assert results[0].guitar == "Telecaster", [r.label for r in results]

    text = render_guitar_comparison(results, through_a_rig=False)
    assert "Telecaster" in text
    assert "raw instruments" in text, "a dry comparison has to say it is one"


def test_a_pickup_is_part_of_the_answer(workspace):
    from rigsmith.core.matching import compare_guitars

    source = workspace / "src"
    takes.add(_guitar(source, "a"), kind="di", guitar="Strat", pickup="bridge")
    takes.add(_guitar(source, "b", dark=0.9), kind="di", guitar="Strat", pickup="neck")

    results = compare_guitars(workspace / "src" / "a.wav")
    assert {r.label for r in results} == {"Strat bridge", "Strat neck"}


def test_comparing_needs_more_than_one_instrument(workspace):
    from rigsmith.core.matching import compare_guitars

    source = workspace / "src"
    only = _guitar(source, "lonely")
    takes.add(only, kind="di", guitar="Strat")

    with pytest.raises(Invalid) as caught:
        compare_guitars(only)
    assert "at least two DI takes" in caught.value.message
    assert "stacks two amps" in caught.value.hint


def test_a_wet_take_is_never_entered_into_the_comparison(workspace):
    """Rendering one through an amp model stacks two amps."""
    from rigsmith.core.matching import compare_guitars

    source = workspace / "src"
    takes.add(_guitar(source, "dry"), kind="di", guitar="Strat")
    takes.add(_guitar(source, "wet"), kind="wet", guitar="Les Paul")

    with pytest.raises(Invalid, match="at least two DI takes"):
        compare_guitars(workspace / "src" / "dry.wav")


def test_the_comparison_can_be_limited_to_named_takes(workspace):
    from rigsmith.core.matching import compare_guitars

    source = workspace / "src"
    for name in ("a", "b", "c"):
        takes.add(_guitar(source, name), kind="di", guitar=name.upper())

    results = compare_guitars(workspace / "src" / "a.wav", take_ids=["a", "b"])
    assert {r.take_id for r in results} == {"a", "b"}


def test_every_result_carries_where_it_differs(workspace):
    """A ranking says which. The bands say what to do about it."""
    from rigsmith.core.matching import compare_guitars

    source = workspace / "src"
    takes.add(_guitar(source, "dark", dark=1.2), kind="di", guitar="Les Paul")
    bright = _guitar(source, "bright", bright=1.2)
    takes.add(bright, kind="di", guitar="Telecaster")

    results = compare_guitars(bright)
    worst = next(r for r in results if r.guitar == "Les Paul")
    assert worst.bands, "no band detail to act on"
    assert any(band.audible for band in worst.bands)


# --------------------------------------------------------------------------
# what the guitar was tuned to
# --------------------------------------------------------------------------


def _played(directory, name, *, base=82.41, cents=0.0):
    """A short riff of plucked notes, so pitch has something to find."""
    directory.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(_seed(name))
    frames = []
    for frequency in (base, base * 1.5, base * 2, base):
        tuned = frequency * (2 ** (cents / 1200))
        t = np.arange(int(0.5 * RATE)) / RATE
        partials = sum(
            (1 / (h + 1)) * np.sin(2 * np.pi * tuned * (h + 1) * t) for h in range(6)
        )
        frames.append(partials * np.exp(-t * 3))
    signal = np.concatenate(frames * 3)
    signal = signal + 0.005 * rng.standard_normal(len(signal))
    signal = signal / np.abs(signal).max() * 0.6

    path = directory / f"{name}.wav"
    sf.write(str(path), signal.astype("float32"), RATE, subtype="FLOAT")
    return path


def test_a_take_records_what_it_was_tuned_to(workspace):
    take = takes.add(
        _played(workspace / "src", "riff"), kind="di", guitar="Les Paul"
    )
    assert take.lowest_note == "E2"
    assert take.in_concert_pitch is True


def test_a_take_recorded_flat_says_so(workspace):
    """Every harmonic sits off where the record's does, and the band
    comparison reads that as tone."""
    take = takes.add(
        _played(workspace / "src", "flat", cents=-30), kind="di", guitar="Strat"
    )

    assert take.in_concert_pitch is False
    assert take.cents_offset == pytest.approx(-30, abs=8)
    assert any("flat of concert pitch" in note for note in take.concerns)
    assert "-30c" in take.describe() or "-29c" in take.describe()


def test_a_tuning_label_that_does_not_match_is_queried(workspace):
    take = takes.add(
        _played(workspace / "src", "dropped", base=73.42),
        kind="di",
        guitar="Strat",
        tuning="E standard",
    )
    assert any("lowest note here is D2" in note for note in take.concerns)
    assert any("not proof of a wrong label" in note for note in take.concerns)


def test_a_matching_tuning_label_says_nothing(workspace):
    take = takes.add(
        _played(workspace / "src", "standard"),
        kind="di",
        guitar="Strat",
        tuning="E standard",
    )
    assert take.concerns == []


def test_a_reference_is_not_measured_for_tuning(workspace):
    """Somebody else's record is not something this library is tuning."""
    take = takes.add(_played(workspace / "src", "record"), kind="reference")
    assert take.lowest_note is None
    assert take.in_concert_pitch is None


def test_a_take_with_nothing_pitched_in_it_claims_nothing(workspace):
    path = workspace / "src" / "hum.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(1)
    sf.write(
        str(path),
        (rng.standard_normal(RATE * 2) * 0.2).astype("float32"),
        RATE,
        subtype="FLOAT",
    )
    take = takes.add(path, kind="di", guitar="Strat")
    assert take.in_concert_pitch in (None, True)


# --------------------------------------------------------------------------
# the same recording, twice
# --------------------------------------------------------------------------


def test_the_same_file_added_twice_is_refused_with_the_first_one(workspace):
    """Pulling loops off the pedal twice is the normal way this happens."""
    source = recording(workspace / "src")
    first = takes.add(source, take_id="first", guitar="Strat")

    with pytest.raises(Conflict) as caught:
        takes.add(source, take_id="second", guitar="Strat")

    assert "already in the library as 'first'" in caught.value.message
    assert first.describe() in caught.value.hint
    assert [t.take_id for t in takes.list_takes()] == ["first"]


def test_a_copy_under_a_different_name_is_still_the_same_recording(workspace):
    import shutil

    source = recording(workspace / "src")
    takes.add(source, guitar="Strat")

    copy = workspace / "src" / "renamed.wav"
    shutil.copy2(source, copy)
    with pytest.raises(Conflict, match="already in the library"):
        takes.add(copy)


def test_a_different_recording_of_the_same_part_is_keepable(workspace):
    """Two takes of the same riff are two takes, not a duplicate."""
    first = recording(workspace / "src", "take1.wav", seconds=2.0)
    second = recording(workspace / "src", "take2.wav", seconds=2.5)

    takes.add(first, guitar="Strat")
    takes.add(second, guitar="Strat")
    assert len(takes.list_takes()) == 2


# --------------------------------------------------------------------------
# recording straight into the library
# --------------------------------------------------------------------------


def test_recording_files_both_halves_of_the_performance(workspace, monkeypatch):
    """The Core sends the wet tone and the dry DI at once, and they are two
    different things: one reamps, the other is the reference."""
    from rigsmith.core import matching

    wet = recording(workspace / "src", "captured-wet.wav")
    dry = recording(workspace / "src", "captured-dry.wav")

    class FakeCapture:
        def describe(self):
            return "captured 2.0s"

    monkeypatch.setattr(
        matching,
        "capture_di",
        lambda duration, name="take": (FakeCapture(), {"wet": wet, "dry": dry}),
    )

    di, wet_take, message = takes.record(
        2.0, take_id="cherub", guitar="Les Paul", pickup="bridge"
    )

    assert di.take_id == "cherub-di" and di.kind == "di" and di.reamps
    assert wet_take.take_id == "cherub-wet" and not wet_take.reamps
    assert di.guitar == wet_take.guitar == "Les Paul"
    assert di.origin == "usb"
    assert "captured 2.0s" in message


def test_recording_without_a_dry_channel_says_what_that_costs(workspace, monkeypatch):
    from rigsmith.core import matching

    wet = recording(workspace / "src", "only-wet.wav")

    class FakeCapture:
        def describe(self):
            return "captured"

    monkeypatch.setattr(
        matching,
        "capture_di",
        lambda duration, name="take": (FakeCapture(), {"wet": wet}),
    )

    di, wet_take, message = takes.record(2.0, guitar="Strat")

    assert di is None
    assert wet_take is not None
    assert "cannot be rendered through other rigs" in message
    assert "channel 5" in message, "and how to fix it"


def test_relabelling_a_take_as_a_di_reconsiders_what_matters(workspace):
    """Lossy encoding is unremarkable in a reference and a real problem in a
    DI, and the label is what decides which one this is."""
    if "MP3" not in readable_formats():
        pytest.skip("this libsndfile has no mp3 encoder")

    source = recording(workspace / "src", "riff.mp3", format="MP3")
    take = takes.add(source, kind="reference")
    assert take.concerns == []

    updated = takes.update(take.take_id, kind="di")
    assert any("worth avoiding" in note for note in updated.concerns)
    assert takes.get(take.take_id).concerns == updated.concerns, "and it persisted"


def test_adding_a_tuning_afterwards_checks_it(workspace):
    take = takes.add(
        _played(workspace / "src", "dropped", base=73.42), kind="di", guitar="Strat"
    )
    assert take.concerns == []

    updated = takes.update(take.take_id, tuning="E standard")
    assert any("lowest note here is D2" in note for note in updated.concerns)


def test_changing_an_unrelated_label_leaves_the_notes_alone(workspace):
    take = takes.add(_played(workspace / "src", "riff"), kind="di", guitar="Strat")
    before = list(take.concerns)
    assert takes.update(take.take_id, pickup="neck").concerns == before

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


def recording(directory, name="riff.wav", seconds=2.0, **kwargs):
    t = np.arange(int(seconds * RATE)) / RATE
    signal = (np.sin(2 * np.pi * 196 * t) * 0.4).astype("float32")
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
    recording(workspace / "a", "riff.wav")
    recording(workspace / "b", "riff.wav")
    takes.add(workspace / "a" / "riff.wav")

    with pytest.raises(Conflict, match="already exists"):
        takes.add(workspace / "b" / "riff.wav")


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

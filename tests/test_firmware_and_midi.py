"""Tests for firmware static analysis and the MIDI control map.

Both run without hardware: the firmware tests build synthetic images, and the
MIDI tests exercise the documented control-number arithmetic rather than a
device.
"""

from __future__ import annotations

import os
import zipfile

import pytest

from rigsmith.device.midi import (
    CC_BLOCK_TOGGLE_BASE,
    CC_FOOTSWITCH_BASE,
    CC_TAP_TEMPO,
    MidiController,
    block_toggle_cc,
    find_headrush_port,
    footswitch_cc,
)
from rigsmith.protocol.firmware import (
    analyse_firmware,
    extract_strings,
    find_signatures,
    mine_catalog_strings,
    shannon_entropy,
)

# --------------------------------------------------------------------------
# firmware
# --------------------------------------------------------------------------


@pytest.fixture()
def firmware_zip(tmp_path):
    """A synthetic image shaped like a firmware package."""
    path = tmp_path / "fw-5.1.0.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "www/index.html",
            "<html><title>HeadRush Remote</title>"
            '<script src="app.js"></script></html>',
        )
        archive.writestr(
            "www/app.js",
            'fetch("/api/v1/rigs"); new WebSocket("ws://host/ws/state");',
        )
        archive.writestr(
            "data/models.json",
            '{"amp_model": "82 LEAD 800 100W", "revalver": true}',
        )
        archive.writestr(
            "bin/app.elf", b"\x7fELF" + b"HeadRush amp_model 65 BLACK PRINCE\x00" * 4
        )
    return path


def test_entropy_spans_the_expected_range():
    assert shannon_entropy(b"\x00" * 10000) == pytest.approx(0.0, abs=1e-9)
    assert 3.0 < shannon_entropy(b"the quick brown fox " * 500) < 5.0
    assert shannon_entropy(os.urandom(20000)) > 7.8


def test_entropy_of_empty_input_is_zero():
    assert shannon_entropy(b"") == 0.0


def test_finds_container_signatures(firmware_zip):
    signatures = find_signatures(firmware_zip.read_bytes())
    names = {s.name for s in signatures}
    assert "zip" in names
    assert "elf" in names


def test_signatures_are_ordered_by_offset(firmware_zip):
    signatures = find_signatures(firmware_zip.read_bytes())
    offsets = [s.offset for s in signatures]
    assert offsets == sorted(offsets)


def test_tar_magic_only_matches_at_its_header_offset():
    """'ustar' appearing in arbitrary data is not a tar archive."""
    noise = b"x" * 100 + b"ustar" + b"y" * 100
    assert not [s for s in find_signatures(noise) if s.name == "tar"]


def test_analyse_unpacks_and_classifies(firmware_zip, tmp_path):
    report = analyse_firmware(firmware_zip, unpack_to=tmp_path / "out")

    assert report.container == "zip"
    assert report.unpacked_to is not None
    assert {p.name for p in report.web_assets} == {"index.html", "app.js"}
    assert any(p.name == "models.json" for p in report.candidate_catalog_files)


def test_analyse_suggests_next_steps(firmware_zip, tmp_path):
    report = analyse_firmware(firmware_zip, unpack_to=tmp_path / "out")
    joined = " ".join(report.next_steps)
    assert "analyse_bundle" in joined
    assert "catalog_extract" in joined


def test_analyse_records_that_it_touched_no_device(firmware_zip, tmp_path):
    report = analyse_firmware(firmware_zip, unpack_to=tmp_path / "out")
    assert any("nothing was flashed" in note for note in report.notes)


def test_analyse_without_unpack_target_does_not_extract(firmware_zip):
    report = analyse_firmware(firmware_zip)
    assert report.unpacked_to is None
    assert report.files == []
    assert report.container == "zip"


def test_missing_image_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        analyse_firmware(tmp_path / "absent.bin")


def test_high_entropy_without_structure_is_flagged(tmp_path):
    """A seeded stream, not os.urandom.

    Two megabytes of true random bytes contain a given 4-byte magic with
    probability ~0.05%, and there are several - so with urandom this test
    fails roughly one run in two hundred, which is exactly the kind of
    intermittent failure nobody can reproduce on demand.
    """
    import random

    path = tmp_path / "encrypted.bin"
    path.write_bytes(random.Random(20260916).randbytes(2_000_000))

    report = analyse_firmware(path)
    assert not report.strong_signatures, "fixture should contain no real magic"
    assert report.entropy > 7.9
    assert report.likely_encrypted
    assert "HIGH ENTROPY" in report.render()


def test_zip_extraction_refuses_path_traversal(tmp_path):
    """A firmware image is untrusted input."""
    path = tmp_path / "evil.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../escaped.txt", "nope")
        archive.writestr("safe.txt", "fine")

    destination = tmp_path / "out"
    report = analyse_firmware(path, unpack_to=destination)

    assert not (tmp_path / "escaped.txt").exists()
    assert {p.name for p in report.files} == {"safe.txt"}


def test_extract_strings_finds_printable_runs():
    data = b"\x00\x01HeadRush Core\x00\xff65 BLACK PRINCE\x00"
    found = extract_strings(data)
    assert "HeadRush Core" in found
    assert "65 BLACK PRINCE" in found


def test_extract_strings_respects_minimum_length():
    assert extract_strings(b"\x00abc\x00", min_length=6) == []


def test_mine_catalog_strings_groups_by_hint(firmware_zip, tmp_path):
    report = analyse_firmware(firmware_zip, unpack_to=tmp_path / "out")
    mined = mine_catalog_strings(report.files)
    assert any("HeadRush" in v for values in mined.values() for v in values)
    assert "headrush" in mined or "amp_model" in mined


# --------------------------------------------------------------------------
# MIDI
# --------------------------------------------------------------------------


def test_block_toggle_cc_matches_the_documented_range():
    """Blocks 1-14 map to CC 75-88."""
    assert block_toggle_cc(1) == CC_BLOCK_TOGGLE_BASE == 75
    assert block_toggle_cc(14) == 88
    assert [block_toggle_cc(s) for s in range(1, 15)] == list(range(75, 89))


def test_footswitch_cc_matches_the_documented_range():
    """Footswitches 1-5 map to CC 49-53."""
    assert footswitch_cc(1) == CC_FOOTSWITCH_BASE == 49
    assert footswitch_cc(5) == 53


def test_block_and_footswitch_ranges_do_not_overlap():
    blocks = {block_toggle_cc(s) for s in range(1, 15)}
    switches = {footswitch_cc(f) for f in range(1, 6)}
    assert not blocks & switches
    assert CC_TAP_TEMPO not in blocks | switches


@pytest.mark.parametrize("slot", [0, 15, -1, 100])
def test_block_toggle_cc_rejects_out_of_range_slots(slot):
    with pytest.raises(ValueError, match="outside 1..14"):
        block_toggle_cc(slot)


@pytest.mark.parametrize("switch", [0, 6, -1])
def test_footswitch_cc_rejects_out_of_range_switches(switch):
    with pytest.raises(ValueError, match="outside 1..5"):
        footswitch_cc(switch)


def test_controller_rejects_an_invalid_channel():
    with pytest.raises(ValueError, match="channel"):
        MidiController(channel=16)


def test_controller_refuses_to_send_before_opening():
    from rigsmith.device.midi import MidiUnavailable

    controller = MidiController()
    with pytest.raises(MidiUnavailable, match="not open"):
        controller.control(CC_TAP_TEMPO)


def test_find_headrush_port_matches_case_insensitively():
    ports = ["Microsoft GS Wavetable Synth", "HeadRush Core MIDI 1"]
    assert find_headrush_port(ports) == "HeadRush Core MIDI 1"


def test_find_headrush_port_returns_none_when_absent():
    assert find_headrush_port(["Some Other Interface"]) is None


def test_activate_scene_requires_a_footswitch():
    """MIDI has no direct scene-select control."""
    from rigsmith.rig.model import Scene

    controller = MidiController()
    with pytest.raises(ValueError, match="no footswitch"):
        controller.activate_scene(Scene(name="Lead"))


# --------------------------------------------------------------------------
# tap tempo
# --------------------------------------------------------------------------


def test_tap_tempo_sends_taps_at_the_right_interval(monkeypatch):
    """There is no set-BPM message; the device averages what a foot does."""
    import time

    from rigsmith.core import devices
    from rigsmith.core.errors import Invalid

    sent: list[str] = []
    slept: list[float] = []

    class FakeController:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def tap_tempo(self):
            sent.append("tap")

    monkeypatch.setattr(devices, "_controller", lambda port, channel: FakeController())
    monkeypatch.setattr(time, "sleep", slept.append)

    message = devices.tap_tempo(120, taps=4)

    assert len(sent) == 4
    # Three gaps between four taps, each half a second at 120 BPM.
    assert slept == pytest.approx([0.5, 0.5, 0.5])
    assert "120.0 BPM" in message
    assert "close rather than exact" in message


def test_tap_tempo_refuses_a_tempo_no_tap_could_express():
    from rigsmith.core import devices
    from rigsmith.core.errors import Invalid

    with pytest.raises(Invalid, match="outside what a tap tempo can express"):
        devices.tap_tempo(2000)


def test_tap_tempo_needs_an_interval_to_average():
    from rigsmith.core import devices
    from rigsmith.core.errors import Invalid

    with pytest.raises(Invalid, match="at least two taps"):
        devices.tap_tempo(120, taps=1)

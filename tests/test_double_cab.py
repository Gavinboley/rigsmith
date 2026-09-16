"""Cab advice that reads the capture instead of guessing about it.

Two mistakes sit on opposite sides of the same question: an amp-only capture
with nothing after it sounds harsh, and a full-rig capture with an IR after it
goes through two speakers. The second is the harder one - it sounds boxy and
dull for a reason nobody can point at - and neither can be called without
knowing what was actually captured.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rigsmith.rig import Block, BlockCategory, Rig
from rigsmith.rig.validate import validate_rig
from rigsmith.tone.nam import find_capture, includes_cab, parse_nam

DILATIONS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]


def capture_document(*, name: str, gear_type: str | None) -> dict:
    layer = {
        "input_size": 1,
        "condition_size": 1,
        "channels": 16,
        "head_size": 8,
        "kernel_size": 3,
        "dilations": DILATIONS,
        "activation": "LeakyReLU",
    }
    metadata: dict = {"name": name, "size": "full"}
    if gear_type is not None:
        metadata["gear_type"] = gear_type
    return {
        "version": "0.5.2",
        "architecture": "WaveNet",
        "config": {"layers": [dict(layer), dict(layer)]},
        "metadata": metadata,
        "weights": [0.1] * 32,
        "sample_rate": 48000,
    }


def written(directory: Path, name: str, gear_type: str | None) -> Path:
    path = directory / f"{name}.nam"
    path.write_text(
        json.dumps(capture_document(name=name, gear_type=gear_type)), encoding="utf-8"
    )
    return path


def lookup_for(directory: Path):
    return lambda model_id: find_capture(model_id, directory)


def messages(result) -> str:
    return " | ".join(f.message for f in result.findings)


# --------------------------------------------------------------------------
# reading the capture
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "gear_type,expected",
    [
        ("full_rig", True),
        ("Full Rig", True),
        ("amp-cab", True),
        ("amp", False),
        ("preamp", False),
        ("pedal", False),
        (None, None),
        ("something new", None),
    ],
)
def test_whether_a_capture_contains_a_speaker(tmp_path, gear_type, expected):
    model = parse_nam(capture_document(name="x", gear_type=gear_type))
    assert includes_cab(model) is expected


def test_a_capture_is_found_by_its_filename(tmp_path):
    written(tmp_path, "jcm800-hot", "full_rig")
    found = find_capture("jcm800-hot", tmp_path)
    assert found is not None and includes_cab(found) is True


def test_a_capture_is_found_by_its_stated_name(tmp_path):
    path = written(tmp_path, "whatever", "amp")
    document = json.loads(path.read_text(encoding="utf-8"))
    document["metadata"]["name"] = "Marshall JMP"
    path.write_text(json.dumps(document), encoding="utf-8")

    assert find_capture("Marshall JMP", tmp_path) is not None


def test_a_near_miss_is_not_a_match(tmp_path):
    """Fuzzy matching would let a rig validate against the wrong capture,
    which is worse than not finding one."""
    written(tmp_path, "jcm800-hot", "full_rig")
    assert find_capture("jcm800", tmp_path) is None
    assert find_capture("", tmp_path) is None


def test_an_unreadable_file_is_not_a_crash(tmp_path):
    (tmp_path / "broken.nam").write_text("{not json", encoding="utf-8")
    assert find_capture("broken", tmp_path) is None


def test_a_missing_directory_is_not_a_crash(tmp_path):
    assert find_capture("anything", tmp_path / "nowhere") is None


# --------------------------------------------------------------------------
# what the rig is told
# --------------------------------------------------------------------------


def rig_with_capture(model_id: str, *, ir_after: bool = False) -> Rig:
    rig = Rig(name="t")
    rig.add_block(Block(slot=0, category=BlockCategory.NAM, model_id=model_id))
    if ir_after:
        rig.add_block(Block(slot=0, category=BlockCategory.IR, model_id="1960A"))
    return rig


def test_a_full_rig_capture_with_an_ir_after_it_is_a_warning(tmp_path):
    """The mistake this exists to catch."""
    written(tmp_path, "jcm800-full", "full_rig")
    result = validate_rig(
        rig_with_capture("jcm800-full", ir_after=True),
        capture_lookup=lookup_for(tmp_path),
    )

    text = messages(result)
    assert "two speakers" in text
    assert "boxy" in text
    assert result.ok, "it is a mistake, not a rig the device will refuse"


def test_an_amp_only_capture_with_no_speaker_is_a_warning(tmp_path):
    written(tmp_path, "jmp-amp", "amp")
    result = validate_rig(
        rig_with_capture("jmp-amp"), capture_lookup=lookup_for(tmp_path)
    )
    text = messages(result)
    assert "amp alone" in text
    assert "harsh" in text


def test_a_full_rig_capture_alone_is_confirmed_as_correct(tmp_path):
    """Saying nothing would leave it looking like the one rig with a gap."""
    written(tmp_path, "jcm800-full", "full_rig")
    result = validate_rig(
        rig_with_capture("jcm800-full"), capture_lookup=lookup_for(tmp_path)
    )

    text = messages(result)
    assert "no IR is needed" in text
    assert not [f for f in result.findings if f.severity == "warning" and "cab" in f.message]


def test_a_capture_that_does_not_say_leaves_the_question_open(tmp_path):
    written(tmp_path, "mystery", None)
    result = validate_rig(
        rig_with_capture("mystery"), capture_lookup=lookup_for(tmp_path)
    )
    assert "depends on what was captured" in messages(result)


def test_a_capture_that_is_not_on_disk_leaves_the_question_open(tmp_path):
    result = validate_rig(
        rig_with_capture("never-downloaded"), capture_lookup=lookup_for(tmp_path)
    )
    assert "depends on what was captured" in messages(result)


def test_without_a_lookup_nothing_is_claimed(tmp_path):
    """The default has no workspace to consult and must not pretend to."""
    written(tmp_path, "jcm800-full", "full_rig")
    result = validate_rig(rig_with_capture("jcm800-full", ir_after=True))
    assert "two speakers" not in messages(result)

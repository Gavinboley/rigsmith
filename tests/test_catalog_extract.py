"""Tests for model-catalog extraction.

This is what lets validation leave "unverified" mode. Until it existed, the
parameter-range and model-id checks - the part of the safety net meant to
stop a confidently wrong write - could never run against real data.
"""

from __future__ import annotations

import json

import pytest

from rigsmith.protocol.catalog_extract import (
    extract,
    find_json_blobs,
    from_json_text,
)
from rigsmith.rig.catalog import Catalog, ModelSpec
from rigsmith.rig.model import BlockCategory

MODELS = [
    {
        "id": "82-lead-800",
        "name": "82 LEAD 800 100W",
        "category": "amp",
        "params": [
            {"name": "gain", "min": 0, "max": 10, "default": 5},
            {"name": "master", "min": 0, "max": 10},
        ],
    },
    {
        "id": "1960a",
        "name": "4X12 65W",
        "category": "cabinet",
        "params": [{"name": "breakup", "min": 0, "max": 1}],
    },
    {
        "id": "tape-delay",
        "name": "Tape Delay",
        "category": "effect",
        "params": ["time", "feedback", "mix"],
    },
]


# --------------------------------------------------------------------------
# blob scanning
# --------------------------------------------------------------------------


def test_finds_a_balanced_object():
    blobs = find_json_blobs('var x = {"a": 1, "b": 2, "c": 3, "d": 4};')
    assert any(json.loads(b) == {"a": 1, "b": 2, "c": 3, "d": 4} for b in blobs)


def test_handles_nested_objects():
    """A non-greedy regex stops at the first closing brace and breaks this."""
    payload = {"outer": {"inner": {"deep": [1, 2, 3]}}, "pad": "xxxxxxxxxxxx"}
    text = "const C=" + json.dumps(payload) + ";"
    assert any(json.loads(b) == payload for b in find_json_blobs(text))


def test_ignores_braces_inside_strings():
    payload = {"label": "a{b}c", "note": "]][[", "pad": "xxxxxxxxxxxxxxxx"}
    text = "x=" + json.dumps(payload) + ";"
    assert any(json.loads(b) == payload for b in find_json_blobs(text))


def test_handles_single_quoted_javascript_strings():
    text = "var s='{not json}'; var d={\"real\": true, \"pad\": \"xxxxxxxxxxxx\"};"
    parsed = [json.loads(b) for b in find_json_blobs(text) if b.startswith("{")
              and _safe(b)]
    assert {"real": True, "pad": "xxxxxxxxxxxx"} in parsed


def _safe(blob: str) -> bool:
    try:
        json.loads(blob)
        return True
    except json.JSONDecodeError:
        return False


def test_unbalanced_input_does_not_hang_or_raise():
    assert find_json_blobs('{"a": 1') == []
    assert find_json_blobs("") == []


# --------------------------------------------------------------------------
# model recognition
# --------------------------------------------------------------------------


def test_extracts_models_from_plain_json():
    catalog = from_json_text(json.dumps(MODELS), source="device")
    assert set(catalog.models) == {"82-lead-800", "1960a", "tape-delay"}


def test_extracts_models_from_minified_javascript():
    text = 'var t="x{y}";const CATALOG=' + json.dumps({"models": MODELS}) + ";f()"
    catalog = from_json_text(text, source="device")
    assert len(catalog.models) == 3


def test_maps_category_aliases():
    catalog = from_json_text(json.dumps(MODELS), source="device")
    assert catalog.models["82-lead-800"].category is BlockCategory.AMP
    # "cabinet" must map onto our own CAB vocabulary.
    assert catalog.models["1960a"].category is BlockCategory.CAB
    assert catalog.models["tape-delay"].category is BlockCategory.EFFECT


def test_extracted_models_are_marked_verified():
    catalog = from_json_text(json.dumps(MODELS), source="device")
    assert all(m.verified for m in catalog.models.values())
    assert catalog.is_verified


def test_extracts_parameter_ranges():
    catalog = from_json_text(json.dumps(MODELS), source="device")
    gain = catalog.models["82-lead-800"].param("gain")
    assert gain is not None
    assert gain.minimum == 0.0
    assert gain.maximum == 10.0
    assert gain.default == 5


def test_bare_string_parameters_are_accepted():
    catalog = from_json_text(json.dumps(MODELS), source="device")
    names = {p.name for p in catalog.models["tape-delay"].params}
    assert names == {"time", "feedback", "mix"}


def test_objects_that_are_not_models_are_ignored():
    noise = [
        {"id": "x", "name": "no category here"},
        {"category": "amp"},
        {"colour": "red", "width": 3},
    ]
    assert from_json_text(json.dumps(noise), source="device").models == {}


# --------------------------------------------------------------------------
# merge and conflicts
# --------------------------------------------------------------------------


def test_conflicting_sources_are_reported_not_silently_resolved():
    """A catalog that guessed a winner would be worse than one that admits it."""
    first = Catalog(source="device")
    first.models["a1"] = ModelSpec(
        id="a1", name="From Device", category=BlockCategory.AMP, verified=True
    )

    second = Catalog(source="firmware")
    second.models["a1"] = ModelSpec(
        id="a1", name="From Firmware", category=BlockCategory.AMP, verified=True
    )

    conflicts = first.merge(second)
    assert conflicts
    assert "From Device" in conflicts[0]
    assert "From Firmware" in conflicts[0]


def test_verified_entries_win_over_unverified_ones():
    merged = Catalog(source="seed")
    merged.models["a1"] = ModelSpec(
        id="a1", name="Guess", category=BlockCategory.AMP, verified=False
    )

    real = Catalog(source="device")
    real.models["a1"] = ModelSpec(
        id="a1", name="Real", category=BlockCategory.AMP, verified=True
    )

    merged.merge(real)
    assert merged.models["a1"].name == "Real"


# --------------------------------------------------------------------------
# end to end
# --------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    from rigsmith import config

    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def write_bundle(root, models=MODELS):
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "app.min.js").write_text(
        "const C=" + json.dumps({"models": models}) + ";", encoding="utf-8"
    )
    return root


def test_extract_saves_a_catalog_the_validator_then_uses(workspace, tmp_path):
    bundle = write_bundle(tmp_path / "capture")
    report = extract(capture_dir=bundle)

    assert report.model_count == 3
    assert (workspace / "catalog.json").exists()

    from rigsmith.rig.catalog import get_catalog

    catalog = get_catalog(reload=True)
    assert catalog.is_verified
    assert "82-lead-800" in catalog.models


def test_verified_catalog_rejects_an_unknown_model(workspace, tmp_path):
    extract(capture_dir=write_bundle(tmp_path / "capture"))

    from rigsmith.rig.catalog import get_catalog
    from rigsmith.rig.model import Block, BlockCategory as BC, Rig
    from rigsmith.rig.validate import validate_rig

    rig = Rig(name="t")
    rig.add_block(Block(slot=0, category=BC.AMP, model_id="not-a-real-model"))

    result = validate_rig(rig, get_catalog(reload=True))
    assert not result.ok
    assert any("not in the catalog" in f.message for f in result.errors)


def test_verified_catalog_enforces_parameter_ranges(workspace, tmp_path):
    extract(capture_dir=write_bundle(tmp_path / "capture"))

    from rigsmith.rig.catalog import get_catalog
    from rigsmith.rig.model import Block, BlockCategory as BC, Rig
    from rigsmith.rig.validate import validate_rig

    rig = Rig(name="t")
    rig.add_block(
        Block(slot=0, category=BC.AMP, model_id="82-lead-800", params={"gain": 99})
    )

    result = validate_rig(rig, get_catalog(reload=True))
    assert not result.ok
    assert any("above maximum" in f.message for f in result.errors)


def test_extract_without_a_source_is_rejected(workspace):
    with pytest.raises(ValueError, match="nothing to extract"):
        extract()


def test_empty_bundle_reports_honestly_rather_than_claiming_success(
    workspace, tmp_path
):
    empty = tmp_path / "capture" / "assets"
    empty.mkdir(parents=True)
    (empty / "app.js").write_text("console.log('nothing here');", encoding="utf-8")

    report = extract(capture_dir=tmp_path / "capture")
    assert report.model_count == 0
    assert "Nothing was extracted" in report.render()
    assert not (workspace / "catalog.json").exists()

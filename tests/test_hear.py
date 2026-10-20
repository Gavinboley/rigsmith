"""A draft heard offline: a DI through its amp, cab, EQ and effects."""

from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

from rigsmith import config  # noqa: E402
from rigsmith.core import autopilot, hear, rigs  # noqa: E402
from rigsmith.core.errors import Invalid  # noqa: E402
from rigsmith.learn.pluck import render_notes  # noqa: E402
from rigsmith.match.render_offline import load_audio, save_audio  # noqa: E402

RATE = 48000


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def _di(path):
    notes = [(b * 0.5, 0.2, [64, 67, 69, 71][b % 4], 0.9) for b in range(8)]
    return save_audio(path, np.concatenate([render_notes(notes, seed=3), np.zeros(2 * RATE)]), RATE)


def _draft():
    draft_id = rigs.create("Heard").draft.draft_id
    for category, model, params in (
        ("effect", "Green JRC-OD", {}),
        ("amp", "82 Lead 800 100W", {"GainA": 0.6, "Bass": 0.5}),
        ("cab", "4x12 Green 20W", {}),
        ("effect", "G EQ", {"Gain800Hz": 0.7}),
        ("effect", "M104 Analog Delay", {"Sync": True, "DelayTime_Synced": 9, "Mix": 0.35}),
        ("effect", "AIR Reverb", {"Mode": 1, "Mix": 0.25}),
        ("effect", "Mystery Box", {}),
    ):
        rigs.add_block(draft_id, category, model, params=params)
    rigs.set_tempo(draft_id, 120.0)
    return draft_id


def test_a_draft_is_heard_block_by_block_effects_and_all(workspace):
    draft_id = _draft()
    di = _di(workspace / "di.wav")
    heard = hear.render(draft_id, di=str(di))
    played = " | ".join(heard.played)
    for expected in ("Green JRC-OD", "82 Lead 800 100W (as the simulated lead voicing) into 4x12 Green 20W", "G EQ", "375 ms", "AIR Reverb (1.6 s"):
        assert expected in played, played
    assert heard.skipped == ["Mystery Box"] and heard.composite is None
    # The delay and the room are there: the tail rings on after the DI stops.
    audio, rate = load_audio(heard.path)
    assert np.sqrt(np.mean(audio[int(4.2 * rate):] ** 2)) > 1e-3
    assert "rehearsal" in heard.describe()


def test_a_scene_is_heard_alone_and_against_its_own_part(workspace):
    draft_id = _draft()
    blocks = rigs.get(draft_id).rig.blocks
    amp = next(b.slot for b in blocks if b.category.value == "amp")
    cab = next(b.slot for b in blocks if b.category.value == "cab")
    rigs.configure_scene(draft_id, "dry", on_slots=[amp, cab], off_slots=[b.slot for b in blocks if b.slot not in (amp, cab)], footswitch=1)
    di = _di(workspace / "di.wav")
    record = _di(workspace / "record.wav")
    autopilot.Run(run_id="h1", request=autopilot.Request(title="Heard"), draft_id=draft_id, di_path=str(di), target_path=str(record), mode="performance", state="done").save()
    heard = hear.render(draft_id, scene="dry")
    assert len(heard.played) == 1 and heard.played[0].startswith("82 Lead 800 100W")
    assert heard.composite is not None and heard.target_path == str(record)
    with pytest.raises(Invalid):
        hear.render(draft_id, scene="nope")


def test_with_no_di_anywhere_it_says_what_to_do(workspace):
    with pytest.raises(Invalid, match="no DI"):
        hear.render(_draft())

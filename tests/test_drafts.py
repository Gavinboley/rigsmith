"""Stepping a draft back through its edits, and forward again."""

from __future__ import annotations

import pytest

from rigsmith import config
from rigsmith.core.errors import Invalid


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def test_an_edit_can_be_undone_and_redone(workspace):
    from rigsmith.core import rigs

    draft_id = rigs.create("Steps").draft.draft_id
    rigs.add_block(draft_id, "amp", "Some Amp", params={"Treble": 0.5})
    rigs.nudge(draft_id, "brighter")
    back = rigs.undo(draft_id)
    assert back.draft.rig.blocks[0].params["Treble"] == 0.5
    assert back.summary.startswith("undid: slot 1 (Some Amp): Treble 50% -> 58%"), "says which edit it undid"
    assert rigs.get(draft_id).rig.blocks[0].params["Treble"] == 0.5, "and it is what is saved"
    forward = rigs.undo(draft_id, redo=True)
    assert forward.draft.rig.blocks[0].params["Treble"] == 0.58 and forward.summary.startswith("redid: ")
    rigs.undo(draft_id)
    rigs.undo(draft_id)
    assert rigs.get(draft_id).rig.blocks == [], "back to before the amp"
    with pytest.raises(Invalid, match="nothing to undo"):
        rigs.undo(draft_id)


def test_a_new_edit_after_an_undo_drops_the_redo(workspace):
    from rigsmith.core import rigs
    from rigsmith.core.drafts import get_store

    draft_id = rigs.create("Branch").draft.draft_id
    rigs.add_block(draft_id, "amp", "Some Amp")
    rigs.nudge(draft_id, "more gain")
    rigs.undo(draft_id)
    assert get_store().steps(draft_id) == (1, 1)
    rigs.nudge(draft_id, "less gain")
    assert get_store().steps(draft_id) == (2, 0)
    with pytest.raises(Invalid, match="nothing to redo"):
        rigs.undo(draft_id, redo=True)


def test_history_is_kept_beside_the_draft_and_bounded(workspace):
    from rigsmith.core import drafts, rigs

    draft_id = rigs.create("Long").draft.draft_id
    rigs.add_block(draft_id, "amp", "Some Amp")
    for _ in range(drafts.HISTORY + 5):
        rigs.nudge(draft_id, "a bit brighter" if _ % 2 else "a bit darker")
    store = drafts.get_store()
    assert store.steps(draft_id) == (drafts.HISTORY, 0)
    assert store.list_ids() == [draft_id], "the history file is not a draft"
    # A re-save that changes nothing is not a step.
    store.save(store.get(draft_id))
    assert store.steps(draft_id) == (drafts.HISTORY, 0)
    rigs.delete(draft_id)
    assert not list(store.directory.glob(f"{draft_id}*")), "deleting a draft takes its history"

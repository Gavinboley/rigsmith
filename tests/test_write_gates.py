"""The gates in front of a device write.

A write overwrites a rig slot on hardware. A rig built by hand on the
device's own screen exists nowhere else, so every gate here is the difference
between an edit and a loss: validate, back up, diff, confirm.
"""

from __future__ import annotations

import pytest

from rigsmith import config
from rigsmith.core import drafts, rigs
from rigsmith.core.errors import ReadOnly
from rigsmith.device.usbmsc import DeviceStorageError


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    drafts.set_store(drafts.DraftStore(tmp_path / "drafts"))
    yield tmp_path
    config.set_settings(None)
    drafts.set_store(None)


@pytest.fixture()
def draft_id(workspace):
    created = rigs.create("Cherub Rock")
    rigs.add_block(created.draft.draft_id, "nam", "JMP-1959")
    return created.draft.draft_id


def _no_storage(monkeypatch):
    """Pretend the device's USB storage is not mounted."""
    from rigsmith.device import usbmsc

    def refuse(*args, **kwargs):
        raise DeviceStorageError("no HeadRush volume is mounted")

    monkeypatch.setattr(usbmsc, "find_device_volume", refuse)


# --------------------------------------------------------------------------
# confirm
# --------------------------------------------------------------------------


def test_without_confirm_nothing_happens(draft_id):
    result = rigs.write(draft_id)
    assert not result.written
    assert "confirm" in result.message
    # And the diff is there to read, which is the point of stopping.
    assert result.diff.changes


def test_the_dry_run_does_not_need_the_device_at_all(draft_id, monkeypatch):
    """Reviewing a diff must work with nothing plugged in."""
    _no_storage(monkeypatch)
    result = rigs.write(draft_id)
    assert not result.written
    assert "backup" not in result.message.lower()


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def test_a_write_without_a_backup_is_refused(draft_id, monkeypatch):
    _no_storage(monkeypatch)
    result = rigs.write(draft_id, confirm=True)

    assert not result.written
    assert "Refusing to write without a backup" in result.message
    assert "USB Transfer" in result.message
    assert "allow_without_backup" in result.message


def test_the_backup_gate_can_be_waived_deliberately(draft_id, monkeypatch):
    """And says loudly what that cost, rather than passing quietly."""
    _no_storage(monkeypatch)
    result = rigs.write(draft_id, confirm=True, allow_without_backup=True)

    # It got past the gate and on to the device, which is not connected here.
    assert "Refusing to write without a backup" not in result.message
    assert result.backup_path is None
    assert "NO BACKUP WAS TAKEN" in result.message


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def test_a_rig_with_errors_is_refused_before_any_of_this(workspace, monkeypatch):
    """The first gate, and the only one that needs no hardware to fail."""
    created = rigs.create("Too Much")
    for index in range(14):
        rigs.add_block(created.draft.draft_id, "nam", f"amp-{index}")

    _no_storage(monkeypatch)
    result = rigs.write(created.draft.draft_id, confirm=True)

    assert not result.written
    if not result.validation.ok:
        assert "Refusing to write" in result.message
        # Refused on its own merits, before the backup was even attempted.
        assert "backup" not in result.message.lower()


# --------------------------------------------------------------------------
# read-only
# --------------------------------------------------------------------------


def test_read_only_mode_stops_a_confirmed_write(draft_id, monkeypatch):
    monkeypatch.setenv("RIGSMITH_READONLY", "1")
    config.set_settings(config.Settings())

    with pytest.raises(ReadOnly):
        rigs.write(draft_id, confirm=True)


def test_read_only_mode_still_lets_you_look(draft_id, monkeypatch):
    """Refusing to write is not a reason to refuse to show the diff."""
    monkeypatch.setenv("RIGSMITH_READONLY", "1")
    config.set_settings(config.Settings())

    result = rigs.write(draft_id)
    assert not result.written
    assert result.diff.changes

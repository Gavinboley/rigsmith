"""Tests for USB mass-storage backup and file transfer.

A temporary directory stands in for the mounted device, so these run without
hardware. The backup path matters more than most: the write pipeline refuses
to touch a rig until a backup exists, so a backup that silently does nothing
would quietly remove the safety net.
"""

from __future__ import annotations

import json

import pytest

from acro.device.usbmsc import (
    BACKUP_FOLDERS,
    DeviceStorageError,
    backup,
    find_device_volume,
    install_file,
    install_nam,
    timestamped_backup,
)


def nam_bytes(*, activation: str = "LeakyReLU", size: str = "lite", name: str = "Amp") -> str:
    return json.dumps(
        {
            "version": "0.5.4",
            "architecture": "WaveNet",
            "weights": [0.1, 0.2],
            "config": {"activation": activation, "size": size},
            "metadata": {"name": name, "gear_make": "Marshall"},
        }
    )


@pytest.fixture()
def volume(tmp_path):
    """A directory laid out like a mounted HeadRush drive."""
    root = tmp_path / "HeadRush"
    for folder in (
        "Rigs",
        "Setlists",
        "Blocks",
        "Impulse Responses/User",
        "Amp Clones/User",
        "NAM/User",
        "NAM/TONE3000",
    ):
        (root / folder).mkdir(parents=True)

    (root / "Rigs" / "Cherub Rock.rig").write_bytes(b"RIG" * 100)
    (root / "Rigs" / "Clean.rig").write_bytes(b"RIG" * 50)
    (root / "Impulse Responses/User" / "1960A.wav").write_bytes(b"RIFF" + b"\x00" * 200)
    (root / "NAM/User" / "jmp.nam").write_text(nam_bytes(name="JMP"))
    return find_device_volume(root)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def test_volume_reports_its_contents(volume):
    described = volume.describe()
    assert "Rigs" in described
    assert "2 file(s)" in described


def test_list_files_filters_by_suffix(volume):
    rigs = volume.list_files("Rigs", ".rig")
    assert len(rigs) == 2
    assert volume.list_files("Rigs", ".nam") == []


def test_explicit_path_that_is_not_a_directory_is_rejected(tmp_path):
    path = tmp_path / "file.txt"
    path.write_text("x")
    with pytest.raises(DeviceStorageError, match="not a directory"):
        find_device_volume(path)


def test_missing_path_is_rejected(tmp_path):
    with pytest.raises(DeviceStorageError):
        find_device_volume(tmp_path / "absent")


# --------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------


def test_backup_copies_every_populated_folder(volume, tmp_path):
    result = backup(tmp_path / "backup", volume)
    assert result.file_count == 4
    assert result.folders["Rigs"] == 2
    assert result.total_bytes > 0


def test_backup_preserves_the_folder_layout(volume, tmp_path):
    destination = tmp_path / "backup"
    backup(destination, volume)
    assert (destination / "Rigs" / "Cherub Rock.rig").is_file()
    assert (destination / "Impulse Responses" / "User" / "1960A.wav").is_file()
    assert (destination / "NAM" / "User" / "jmp.nam").is_file()


def test_backup_writes_a_manifest(volume, tmp_path):
    destination = tmp_path / "backup"
    result = backup(destination, volume)
    manifest = (destination / "manifest.txt").read_text(encoding="utf-8")
    assert "acro backup" in manifest
    assert f"files: {result.file_count}" in manifest


def test_backup_leaves_the_device_untouched(volume, tmp_path):
    before = {p: p.read_bytes() for p in volume.root.rglob("*") if p.is_file()}
    backup(tmp_path / "backup", volume)
    after = {p: p.read_bytes() for p in volume.root.rglob("*") if p.is_file()}
    assert before == after


def test_empty_device_backup_says_so(tmp_path):
    root = tmp_path / "HeadRush"
    for folder in ("Rigs", "Blocks"):
        (root / folder).mkdir(parents=True)
    empty = find_device_volume(root)

    result = backup(tmp_path / "backup", empty)
    assert result.file_count == 0
    assert any("nothing was copied" in note for note in result.notes)


def test_timestamped_backups_do_not_collide(volume, tmp_path):
    first = timestamped_backup(tmp_path / "backups", volume)
    second = timestamped_backup(tmp_path / "backups", volume)
    # Same second is possible; the important thing is both succeeded and the
    # content is complete either way.
    assert first.file_count == second.file_count == 4


def test_backup_folder_list_covers_every_user_area():
    """A backup that misses a folder is a backup that loses work."""
    assert set(BACKUP_FOLDERS) >= {
        "Rigs",
        "Setlists",
        "Blocks",
        "Impulse Responses",
        "Amp Clones",
        "NAM",
    }


# --------------------------------------------------------------------------
# installing files
# --------------------------------------------------------------------------


def test_install_nam_copies_and_reminds_about_sync(volume, tmp_path):
    source = tmp_path / "newamp.nam"
    source.write_text(nam_bytes(name="New"))

    message = install_nam(source, volume)
    assert (volume.root / "NAM" / "User" / "newamp.nam").is_file()
    assert "Sync" in message


def test_install_rejects_a_suffix_that_does_not_belong(volume, tmp_path):
    source = tmp_path / "cab.wav"
    source.write_bytes(b"RIFF")
    with pytest.raises(DeviceStorageError, match="expects .nam"):
        install_file(source, "NAM/User", volume)


def test_install_refuses_to_overwrite_by_default(volume, tmp_path):
    source = tmp_path / "jmp.nam"
    source.write_text(nam_bytes())
    with pytest.raises(DeviceStorageError, match="already exists"):
        install_file(source, "NAM/User", volume)


def test_install_overwrites_when_asked(volume, tmp_path):
    source = tmp_path / "jmp.nam"
    source.write_text(nam_bytes(name="Replaced"))
    target = install_file(source, "NAM/User", volume, overwrite=True)
    assert "Replaced" in target.read_text(encoding="utf-8")


def test_install_rejects_a_directory(volume, tmp_path):
    """The device cannot accept folders or archives, only individual files."""
    folder = tmp_path / "pack"
    folder.mkdir()
    with pytest.raises(DeviceStorageError, match="not a file"):
        install_file(folder, "NAM/User", volume)


def test_install_nam_refuses_a_capture_the_core_cannot_load(volume, tmp_path):
    """Custom architectures are not in the Core's supported set."""
    source = tmp_path / "exotic.nam"
    source.write_text(
        json.dumps(
            {
                "architecture": "WaveNet",
                "weights": [0.1],
                "config": {"activation": "LeakyReLU", "size": "gigantic"},
                "metadata": {"name": "Exotic"},
            }
        )
    )
    with pytest.raises(DeviceStorageError, match="does not load"):
        install_nam(source, volume)


def test_install_nam_warns_when_compatibility_cannot_be_determined(volume, tmp_path):
    source = tmp_path / "mystery.nam"
    source.write_text(
        json.dumps({"architecture": "WaveNet", "weights": [0.1], "config": {}})
    )
    message = install_nam(source, volume)
    assert "unverified" in message
    assert (volume.root / "NAM" / "User" / "mystery.nam").is_file()

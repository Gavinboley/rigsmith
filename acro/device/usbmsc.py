"""File transfer over the device's USB mass-storage mode.

Putting the Core into Menu > USB Transfer mounts it as a drive named
"HeadRush" with a known layout::

    Rigs/                     .rig    presets
    Setlists/                         ordered collections of rigs
    Blocks/                           single-model presets
    Impulse Responses/User/   .wav    cabinet IRs
    Amp Clones/User/          .ac     HeadRush Amp Cloner captures
    NAM/User/                 .nam    side-loaded NAM captures
    NAM/TONE3000/             .nam    captures downloaded on-device

Two constraints that are easy to trip over:

* **Folders and archives cannot be transferred** - individual files only.
* After copying, eject the drive and then tap **Sync** on the device.
  Without that the files are on the storage but not in the device's index.

This is also how backups are taken. The write pipeline will not touch a rig
until a backup exists, and this is where it comes from.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: Volume label the device presents in USB Transfer mode.
VOLUME_LABEL = "HeadRush"

#: Folders on the device, and what belongs in each.
DEVICE_FOLDERS = {
    "Rigs": ".rig",
    "Setlists": None,
    "Blocks": None,
    "Impulse Responses/User": ".wav",
    "Amp Clones/User": ".ac",
    "NAM/User": ".nam",
}

#: Folders a backup captures, in priority order.
BACKUP_FOLDERS = (
    "Rigs",
    "Setlists",
    "Blocks",
    "Impulse Responses",
    "Amp Clones",
    "NAM",
)

#: Marker folders used to recognise the drive when the label is unavailable.
IDENTIFYING_FOLDERS = ("Rigs", "Blocks")


class DeviceStorageError(RuntimeError):
    """The device storage is not mounted, or does not look right."""


@dataclass
class DeviceVolume:
    """A mounted HeadRush drive."""

    root: Path
    label: str = ""

    def folder(self, name: str) -> Path:
        return self.root / name

    def exists(self, name: str) -> bool:
        return self.folder(name).is_dir()

    def list_files(self, name: str, suffix: str | None = None) -> list[Path]:
        folder = self.folder(name)
        if not folder.is_dir():
            return []
        files = [p for p in sorted(folder.rglob("*")) if p.is_file()]
        if suffix:
            files = [p for p in files if p.suffix.lower() == suffix]
        return files

    def describe(self) -> str:
        lines = [f"HeadRush storage at {self.root}"]
        for name in BACKUP_FOLDERS:
            if self.exists(name):
                count = len([p for p in self.folder(name).rglob("*") if p.is_file()])
                lines.append(f"  {name:22} {count} file(s)")
            else:
                lines.append(f"  {name:22} (absent)")
        return "\n".join(lines)


def find_device_volume(explicit: str | Path | None = None) -> DeviceVolume:
    """Locate the mounted device storage.

    Args:
        explicit: Use this path instead of searching.

    Raises:
        DeviceStorageError: if nothing that looks like the device is mounted.
    """
    if explicit is not None:
        root = Path(explicit)
        if not root.is_dir():
            raise DeviceStorageError(f"{root} is not a directory")
        return DeviceVolume(root=root, label=root.name)

    candidates: list[Path] = []

    # Windows: walk the drive letters looking for the volume label or layout.
    import string
    import sys

    if sys.platform == "win32":
        for letter in string.ascii_uppercase:
            root = Path(f"{letter}:/")
            try:
                if root.exists():
                    candidates.append(root)
            except OSError:
                continue
    else:
        for base in (Path("/Volumes"), Path("/media"), Path("/mnt")):
            if base.is_dir():
                try:
                    candidates.extend(p for p in base.iterdir() if p.is_dir())
                    for child in list(candidates):
                        if child.is_dir():
                            candidates.extend(
                                p for p in child.iterdir() if p.is_dir()
                            )
                except OSError:
                    continue

    for root in candidates:
        try:
            if all((root / marker).is_dir() for marker in IDENTIFYING_FOLDERS):
                return DeviceVolume(root=root, label=root.name)
        except OSError:
            continue

    raise DeviceStorageError(
        "No HeadRush storage found.\n\n"
        "On the device: Menu > USB Transfer, with a USB-B cable to this "
        "computer. It should mount as a drive named 'HeadRush' containing "
        "Rigs, Blocks, and Impulse Responses folders.\n"
        "If it is mounted somewhere unusual, pass the path explicitly."
    )


@dataclass
class BackupResult:
    """A completed backup."""

    destination: Path
    folders: dict[str, int] = field(default_factory=dict)
    total_bytes: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def file_count(self) -> int:
        return sum(self.folders.values())

    def describe(self) -> str:
        lines = [
            f"Backup at {self.destination}",
            f"  {self.file_count} file(s), {self.total_bytes:,} bytes",
        ]
        for name, count in sorted(self.folders.items()):
            lines.append(f"  {name:22} {count}")
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


def backup(
    destination: str | Path,
    volume: DeviceVolume | None = None,
    *,
    folders: tuple[str, ...] = BACKUP_FOLDERS,
) -> BackupResult:
    """Copy the device's content to a local directory.

    Nothing on the device is modified - this is a read and copy. Run it before
    any write, which is what the authoring pipeline does automatically.
    """
    volume = volume or find_device_volume()
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)

    result = BackupResult(destination=destination)

    for name in folders:
        source = volume.folder(name)
        if not source.is_dir():
            continue
        target = destination / name
        target.mkdir(parents=True, exist_ok=True)

        count = 0
        for path in source.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(source)
            copy_to = target / relative
            copy_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, copy_to)
            count += 1
            result.total_bytes += path.stat().st_size
        if count:
            result.folders[name] = count

    if not result.folders:
        result.notes.append(
            "nothing was copied - the device folders were empty or absent"
        )

    manifest = destination / "manifest.txt"
    manifest.write_text(
        "\n".join(
            [
                f"acro backup of {volume.root}",
                f"taken: {datetime.now().isoformat(timespec='seconds')}",
                f"files: {result.file_count}",
                f"bytes: {result.total_bytes}",
                "",
                *(f"{name}: {count}" for name, count in sorted(result.folders.items())),
            ]
        ),
        encoding="utf-8",
    )
    return result


def timestamped_backup(
    backups_dir: str | Path, volume: DeviceVolume | None = None
) -> BackupResult:
    """Back up into a new timestamped folder under ``backups_dir``."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return backup(Path(backups_dir) / stamp, volume)


def install_file(
    source: str | Path,
    folder: str,
    volume: DeviceVolume | None = None,
    *,
    overwrite: bool = False,
) -> Path:
    """Copy one file onto the device.

    Args:
        source: Local file. A ``.nam`` capture, a ``.wav`` IR, a ``.rig``.
        folder: Destination folder, e.g. ``NAM/User``.
        overwrite: Replace an existing file of the same name.

    Raises:
        DeviceStorageError: on a suffix that does not belong in ``folder``, a
            directory, or an existing file when ``overwrite`` is False.
    """
    volume = volume or find_device_volume()
    source = Path(source)

    if not source.is_file():
        raise DeviceStorageError(
            f"{source} is not a file. The device cannot accept folders or "
            "archives - copy individual files only."
        )

    expected = DEVICE_FOLDERS.get(folder)
    if expected and source.suffix.lower() != expected:
        raise DeviceStorageError(
            f"{folder} expects {expected} files, but {source.name} is "
            f"{source.suffix or 'extensionless'}"
        )

    target_dir = volume.folder(folder)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name

    if target.exists() and not overwrite:
        raise DeviceStorageError(
            f"{target} already exists. Pass overwrite=True to replace it."
        )

    shutil.copy2(source, target)
    return target


def install_nam(
    source: str | Path, volume: DeviceVolume | None = None, *, overwrite: bool = False
) -> str:
    """Copy a NAM capture to the device's user folder.

    Returns a message including the Sync reminder, because without it the
    file is present on the storage but invisible to the device.
    """
    from ..tone.nam import parse_nam

    source = Path(source)
    warnings: list[str] = []
    try:
        model = parse_nam(source)
        if model.headrush_compatible is False:
            raise DeviceStorageError(
                f"{source.name} is {model.generation.value} {model.size}, which "
                "the Core does not load. It accepts A2 Full/Lite and A1 "
                "Standard/Lite/Nano/Feather."
            )
        if model.headrush_compatible is None:
            warnings.append(
                "could not determine this capture's architecture or size, so "
                "compatibility is unverified - it may not load"
            )
    except ValueError as exc:
        warnings.append(f"could not parse as NAM ({exc}); copying anyway")

    target = install_file(source, "NAM/User", volume, overwrite=overwrite)

    lines = [f"Copied {source.name} to {target}"]
    lines.extend(f"  warning: {w}" for w in warnings)
    lines.append(
        "\nNow eject the drive, then tap Sync on the device. Until you do, "
        "the file is on the storage but not in the device's index."
    )
    return "\n".join(lines)

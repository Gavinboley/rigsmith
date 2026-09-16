"""What is in the workspace, and how to clean it up.

``~/.rigsmith`` accumulates real weight — device backups, archived Remote
Editor bundles, unpacked firmware, rendered audio, drafts, A/B sessions — and
until now nothing could tell you what was in there. You had to go and look.

This gives every surface one answer to "what has this tool got of mine, how
big is it, and what can I safely delete".

Pruning is deliberately conservative: it defaults to a dry run, it will not
touch backups unless asked explicitly, and it always reports what it would
remove before removing it.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings, require_writable
from .errors import Invalid

#: Each area of the workspace, and whether it is safe to prune by default.
#: Backups are excluded because they are the safety net for device writes.
AREAS: dict[str, tuple[str, bool]] = {
    "backups": ("device backups, taken before every write", False),
    "captures": ("archived Remote Editor bundles and protocol inventories", True),
    "firmware": ("downloaded firmware images and unpacked trees", True),
    "audio": ("DI captures, rendered candidates, separated stems, A/B sessions", True),
    "research": ("cached tone research and compiled recipes", True),
    "drafts": ("rigs being worked on", False),
    "exports": ("rigs exported to JSON", True),
}


def _format_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@dataclass
class AreaReport:
    """One subdirectory of the workspace."""

    name: str
    path: Path
    description: str
    prunable: bool
    exists: bool = False
    file_count: int = 0
    total_bytes: int = 0
    newest: float | None = None
    oldest: float | None = None

    @property
    def age_days(self) -> float | None:
        """Age of the newest item, in days."""
        if self.newest is None:
            return None
        return (time.time() - self.newest) / 86400

    def render(self) -> str:
        if not self.exists:
            return f"  {self.name:10} (not created yet)"
        age = ""
        if self.age_days is not None:
            age = f"newest {self.age_days:.0f}d ago" if self.age_days >= 1 else "newest today"
        return (
            f"  {self.name:10} {self.file_count:>5} files  "
            f"{_format_bytes(self.total_bytes):>9}  {age}"
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": str(self.path),
            "description": self.description,
            "prunable": self.prunable,
            "exists": self.exists,
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "human_size": _format_bytes(self.total_bytes),
            "newest": self.newest,
            "age_days": self.age_days,
        }


@dataclass
class WorkspaceReport:
    """The whole workspace."""

    root: Path
    areas: list[AreaReport] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(a.total_bytes for a in self.areas)

    @property
    def file_count(self) -> int:
        return sum(a.file_count for a in self.areas)

    def area(self, name: str) -> AreaReport | None:
        return next((a for a in self.areas if a.name == name), None)

    def render(self) -> str:
        lines = [
            f"workspace: {self.root}",
            f"           {self.file_count} files, {_format_bytes(self.total_bytes)}",
            "",
        ]
        lines.extend(a.render() for a in self.areas)
        prunable = sum(a.total_bytes for a in self.areas if a.prunable)
        if prunable:
            lines.append("")
            lines.append(
                f"  {_format_bytes(prunable)} is prunable "
                "(backups and drafts are kept unless named explicitly)"
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "root": str(self.root),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "human_size": _format_bytes(self.total_bytes),
            "areas": [a.to_dict() for a in self.areas],
        }


def inspect(root: Path | None = None) -> WorkspaceReport:
    """Walk the workspace and report what is in each area."""
    settings = get_settings()
    root = Path(root) if root else settings.workspace
    report = WorkspaceReport(root=root)

    for name, (description, prunable) in AREAS.items():
        path = root / name
        area = AreaReport(
            name=name, path=path, description=description, prunable=prunable
        )
        if path.is_dir():
            area.exists = True
            times: list[float] = []
            for item in path.rglob("*"):
                if not item.is_file():
                    continue
                try:
                    stat = item.stat()
                except OSError:
                    continue
                area.file_count += 1
                area.total_bytes += stat.st_size
                times.append(stat.st_mtime)
            if times:
                area.newest = max(times)
                area.oldest = min(times)
        report.areas.append(area)

    return report


@dataclass
class PruneResult:
    """What a prune removed, or would remove."""

    dry_run: bool
    removed: list[tuple[str, Path, int]] = field(default_factory=list)
    freed_bytes: int = 0
    skipped: list[str] = field(default_factory=list)

    def render(self) -> str:
        if not self.removed:
            lines = ["Nothing to prune."]
            lines.extend(f"  skipped: {s}" for s in self.skipped)
            return "\n".join(lines)

        verb = "Would remove" if self.dry_run else "Removed"
        lines = [f"{verb} {len(self.removed)} item(s), {_format_bytes(self.freed_bytes)}:"]
        for area, path, size in self.removed[:25]:
            lines.append(f"  {area:10} {path.name}  ({_format_bytes(size)})")
        if len(self.removed) > 25:
            lines.append(f"  ... and {len(self.removed) - 25} more")
        lines.extend(f"  skipped: {s}" for s in self.skipped)
        if self.dry_run:
            lines.append("\nDry run - nothing was deleted. Pass dry_run=False to apply.")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "count": len(self.removed),
            "freed_bytes": self.freed_bytes,
            "human_size": _format_bytes(self.freed_bytes),
            "removed": [
                {"area": a, "path": str(p), "bytes": s} for a, p, s in self.removed
            ],
            "skipped": self.skipped,
        }


def prune(
    areas: list[str] | None = None,
    *,
    older_than_days: float | None = None,
    dry_run: bool = True,
    root: Path | None = None,
) -> PruneResult:
    """Remove workspace content, dry-run by default.

    Args:
        areas: Which areas to prune. Defaults to every area marked prunable —
            which excludes ``backups`` and ``drafts``, since those are the
            safety net and the work in progress respectively. Name them
            explicitly to prune them anyway.
        older_than_days: Only remove entries last modified before this.
        dry_run: Report without deleting. **Default true.**
        root: Override the workspace location.

    Raises:
        Invalid: on an unknown area name.
    """
    settings = get_settings()
    root = Path(root) if root else settings.workspace

    if areas is None:
        targets = [n for n, (_, prunable) in AREAS.items() if prunable]
    else:
        unknown = [a for a in areas if a not in AREAS]
        if unknown:
            raise Invalid(
                f"unknown workspace area(s): {', '.join(unknown)}",
                hint=f"Known areas: {', '.join(AREAS)}",
            )
        targets = list(areas)

    result = PruneResult(dry_run=dry_run)
    if not dry_run:
        require_writable("prune the workspace")

    cutoff = time.time() - older_than_days * 86400 if older_than_days else None

    for name in targets:
        path = root / name
        if not path.is_dir():
            continue

        # Prune whole top-level entries rather than individual files, so a
        # backup or a capture stays coherent instead of half-deleted.
        for entry in sorted(path.iterdir()):
            try:
                stat = entry.stat()
            except OSError:
                continue
            if cutoff is not None and stat.st_mtime >= cutoff:
                continue

            if entry.is_dir():
                size = sum(
                    f.stat().st_size for f in entry.rglob("*") if f.is_file()
                )
            else:
                size = stat.st_size

            result.removed.append((name, entry, size))
            result.freed_bytes += size

            if not dry_run:
                try:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                except OSError as exc:
                    result.skipped.append(f"{entry.name}: {exc}")

    for name, (_, prunable) in AREAS.items():
        if not prunable and name not in targets and (root / name).is_dir():
            result.skipped.append(f"{name} (protected; name it explicitly to prune)")

    return result

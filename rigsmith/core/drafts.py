"""Draft rigs held between tool calls.

An agent builds a rig incrementally - create it, add a block, set a parameter,
add a scene, check it, look at the diff, then write. That needs somewhere for
the work-in-progress to live, and it needs to survive a server restart, since
a half-built rig that evaporates is worse than useless.

Drafts are plain JSON on disk in the workspace. Nothing here touches the
device; writing is a separate, explicitly gated step.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings
from ..rig.codec import load_rig, rig_to_dict, save_rig
from ..rig.model import Rig

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.lower()).strip("-")
    return slug or "rig"


@dataclass
class Draft:
    """A rig being worked on, plus what it was based on."""

    draft_id: str
    rig: Rig
    baseline: Rig | None = None
    """The device's current version, when this draft edits an existing rig.
    ``None`` means the rig is new, and the diff reads as a creation."""
    created_at: str = ""
    updated_at: str = ""
    notes: list[str] = field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


class DraftStore:
    """Drafts on disk, keyed by id."""

    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            directory = get_settings().workspace / "drafts"
        self.directory = Path(directory)
        self._cache: dict[str, Draft] = {}

    # -- paths ------------------------------------------------------------

    def _path(self, draft_id: str) -> Path:
        return self.directory / f"{draft_id}.json"

    def _baseline_path(self, draft_id: str) -> Path:
        return self.directory / f"{draft_id}.baseline.json"

    # -- crud -------------------------------------------------------------

    def create(self, rig: Rig, *, baseline: Rig | None = None, draft_id: str | None = None) -> Draft:
        draft_id = draft_id or self._unique_id(slugify(rig.name))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        draft = Draft(
            draft_id=draft_id,
            rig=rig,
            baseline=baseline,
            created_at=now,
            updated_at=now,
        )
        self.save(draft)
        return draft

    def _unique_id(self, base: str) -> str:
        candidate = base
        index = 2
        while self._path(candidate).exists() or candidate in self._cache:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def save(self, draft: Draft) -> Draft:
        draft.touch()
        self.directory.mkdir(parents=True, exist_ok=True)
        payload = rig_to_dict(draft.rig)
        payload["_draft"] = {
            "draft_id": draft.draft_id,
            "created_at": draft.created_at,
            "updated_at": draft.updated_at,
            "notes": draft.notes,
            "has_baseline": draft.baseline is not None,
        }
        from . import storage

        # Baseline first: a crash between the two then leaves an older draft
        # beside its baseline, never a newer draft diffed against a baseline
        # that is not its own. Each is written beside and swapped in.
        if draft.baseline is not None:
            save_rig(draft.baseline, self._baseline_path(draft.draft_id))
        storage.write_json(self._path(draft.draft_id), payload)
        self._cache[draft.draft_id] = draft
        return draft

    def get(self, draft_id: str) -> Draft:
        if draft_id in self._cache:
            return self._cache[draft_id]

        path = self._path(draft_id)
        if not path.exists():
            known = ", ".join(self.list_ids()) or "none"
            raise KeyError(f"no draft '{draft_id}'. Known drafts: {known}")

        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = payload.pop("_draft", {})
        from ..rig.codec import rig_from_dict

        baseline = None
        baseline_path = self._baseline_path(draft_id)
        if baseline_path.exists():
            baseline = load_rig(baseline_path)

        draft = Draft(
            draft_id=draft_id,
            rig=rig_from_dict(payload),
            baseline=baseline,
            created_at=meta.get("created_at", ""),
            updated_at=meta.get("updated_at", ""),
            notes=meta.get("notes", []),
        )
        self._cache[draft_id] = draft
        return draft

    def delete(self, draft_id: str) -> None:
        self._cache.pop(draft_id, None)
        for path in (self._path(draft_id), self._baseline_path(draft_id)):
            if path.exists():
                path.unlink()

    def list_ids(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(
            p.stem
            for p in self.directory.glob("*.json")
            if not p.name.endswith(".baseline.json")
        )

    def list_drafts(self) -> list[Draft]:
        drafts = []
        for draft_id in self.list_ids():
            try:
                drafts.append(self.get(draft_id))
            except (KeyError, ValueError, OSError):
                continue
        return drafts


_store: DraftStore | None = None


def get_store() -> DraftStore:
    global _store
    if _store is None:
        _store = DraftStore()
    return _store


def set_store(store: DraftStore | None) -> None:
    """Replace the process-wide store. Used by the CLI and by tests."""
    global _store
    _store = store

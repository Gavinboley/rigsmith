"""Draft rigs held between tool calls.

An agent builds a rig incrementally - create it, add a block, set a parameter,
add a scene, check it, look at the diff, then write. That needs somewhere for
the work-in-progress to live, and it needs to survive a server restart, since
a half-built rig that evaporates is worse than useless.

Drafts are plain JSON on disk in the workspace. Nothing here touches the
device; writing is a separate, explicitly gated step.

Each save keeps the version it replaces, so an edit can be undone - most
of all a nudge by ear, where "that was worse" is half the point of trying.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings
from ..rig.codec import load_rig, rig_to_dict, save_rig
from ..rig.model import Rig

_SLUG = re.compile(r"[^a-z0-9]+")

#: Versions of a draft kept to step back through.
HISTORY = 30


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
        # To the millisecond: two edits in one second - the page's and an
        # agent's - must not look like the same edit.
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class DraftStore:
    """Drafts on disk, keyed by id."""

    def __init__(self, directory: Path | None = None) -> None:
        if directory is None:
            directory = get_settings().workspace / "drafts"
        self.directory = Path(directory)
        self._cache: dict[str, Draft] = {}
        # The web app saves from several threads; a save and its history step
        # (or an undo) must not interleave with another on the same files.
        self._lock = threading.RLock()

    # -- paths ------------------------------------------------------------

    def _path(self, draft_id: str) -> Path:
        return self.directory / f"{draft_id}.json"

    def _baseline_path(self, draft_id: str) -> Path:
        return self.directory / f"{draft_id}.baseline.json"

    def _history_path(self, draft_id: str) -> Path:
        return self.directory / f"{draft_id}.history.json"

    # -- history ----------------------------------------------------------

    def _history(self, draft_id: str) -> dict:
        """``{"past": [...], "future": [...]}``, each entry a saved version
        and the change that separates it from the one after."""
        try:
            data = json.loads(self._history_path(draft_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"past": [], "future": []}
        return {"past": list(data.get("past") or []), "future": list(data.get("future") or [])}

    @staticmethod
    def _content(payload: dict) -> dict:
        """A saved version without its timestamp, to tell a change from a re-save."""
        meta = {k: v for k, v in (payload.get("_draft") or {}).items() if k != "updated_at"}
        return {**payload, "_draft": meta}

    def steps(self, draft_id: str) -> tuple[int, int]:
        """How many edits can be undone, and how many redone."""
        history = self._history(draft_id)
        return len(history["past"]), len(history["future"])

    def changes(self, draft_id: str) -> dict[str, list[str]]:
        """What each step would undo, newest first, and what each would redo, next first."""
        history = self._history(draft_id)
        said = lambda entry: (entry.get("change") if isinstance(entry, dict) else None) or "an edit"  # noqa: E731
        return {"past": [said(e) for e in reversed(history["past"])], "future": [said(e) for e in reversed(history["future"])]}

    def changes_since(self, draft_id: str, when: str) -> list[str]:
        """The edits made after ``when`` (an ``updated_at``) that still stand, oldest first."""
        found = []
        for entry in self._history(draft_id)["past"]:
            if isinstance(entry, dict) and str((entry.get("payload") or {}).get("_draft", {}).get("updated_at", "")) >= when:
                found.append(entry.get("change") or "an edit")
        return found

    def undo(self, draft_id: str, *, redo: bool = False) -> tuple[Draft, str]:
        """Put back the version before the last edit (or, ``redo``, after the
        last undo) and say which change that was. LookupError if there is none."""
        from . import storage

        with self._lock:
            draft, change = self._step(draft_id, redo, storage)
        _announce(draft)
        return draft, change

    def _step(self, draft_id: str, redo: bool, storage) -> tuple[Draft, str]:
        history = self._history(draft_id)
        source, target = ("future", "past") if redo else ("past", "future")
        if not history[source]:
            raise LookupError("nothing to redo" if redo else "nothing to undo")
        entry = history[source].pop()
        if not isinstance(entry, dict) or not isinstance(entry.get("payload"), dict):
            raise LookupError("this draft's history cannot be read, so there is nothing to step to")
        current = json.loads(self._path(draft_id).read_text(encoding="utf-8"))
        history[target].append({"payload": current, "change": entry.get("change") or "an edit"})
        restored = entry["payload"]
        restored.setdefault("_draft", {})["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        storage.write_json(self._history_path(draft_id), history)
        storage.write_json(self._path(draft_id), restored)
        self._cache.pop(draft_id, None)
        return self.get(draft_id), entry.get("change") or "an edit"

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

    def save(self, draft: Draft, *, change: str | None = None) -> Draft:
        """Write ``draft``, keeping the version it replaces as one step back;
        ``change`` is what this save did, said when it is undone."""
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

        with self._lock:
            self._write(draft, payload, change, storage)
        _announce(draft)
        return draft

    def _write(self, draft: Draft, payload: dict, change: str | None, storage) -> Draft:
        path = self._path(draft.draft_id)
        try:
            before = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (OSError, ValueError):
            before = None
        if before is not None and self._content(before) != self._content(payload):
            history = self._history(draft.draft_id)
            history["past"] = (history["past"] + [{"payload": before, "change": change or "an edit"}])[-HISTORY:]
            history["future"] = []
            storage.write_json(self._history_path(draft.draft_id), history)

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
        for path in (self._path(draft_id), self._baseline_path(draft_id), self._history_path(draft_id)):
            if path.exists():
                path.unlink()

    def list_ids(self) -> list[str]:
        if not self.directory.exists():
            return []
        return sorted(
            p.stem
            for p in self.directory.glob("*.json")
            if not p.name.endswith((".baseline.json", ".history.json"))
        )

    def list_drafts(self) -> list[Draft]:
        drafts = []
        for draft_id in self.list_ids():
            try:
                drafts.append(self.get(draft_id))
            except (KeyError, ValueError, OSError):
                continue
        return drafts


def _announce(draft: Draft) -> None:
    """Tell open pages a draft changed - the chat's agents and buttons edit
    drafts too, and a Rig page showing one should not go stale."""
    try:
        from . import events

        events.publish({"type": "draft", "draft_id": draft.draft_id, "updated_at": draft.updated_at})
    except Exception:
        pass


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

"""Writing JSON records that other threads are reading.

A chat's file is rewritten on every event its agent produces while the page
polls it; an autopilot run's while the stage list is drawn. Writing in place
lets a reader catch the file half-written - which surfaced as an occasional
``JSONDecodeError`` in a test, and would surface as a blank chat in the app.

So every record is written to a temporary file beside it and swapped in with
``os.replace``, which is atomic. On Windows the swap is refused while another
handle has the target open, so it retries for a moment rather than failing.
"""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def lock_for(key: str) -> threading.RLock:
    """One re-entrant lock per record, for read-modify-write sequences."""
    with _guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


@contextmanager
def locked(path: Path):
    with lock_for(str(Path(path).resolve())):
        yield


def write_text(path: Path, text: str, *, attempts: int = 50) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(attempts):
        try:
            os.replace(temporary, path)
            return path
        except PermissionError:
            time.sleep(0.01 * (attempt + 1))
    os.replace(temporary, path)
    return path


def write_json(path: Path, data, **kwargs) -> Path:
    kwargs.setdefault("indent", 2)
    kwargs.setdefault("default", str)
    return write_text(path, json.dumps(data, **kwargs))


def read_json(path: Path, *, attempts: int = 20):
    """Read a record, retrying briefly if it is mid-swap on Windows."""
    path = Path(path)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as exc:
            last = exc
            time.sleep(0.01 * (attempt + 1))
        except json.JSONDecodeError as exc:
            # Only a file written before this module existed can be torn.
            last = exc
            time.sleep(0.01 * (attempt + 1))
    raise last  # type: ignore[misc]

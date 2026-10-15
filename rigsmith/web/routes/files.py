"""Getting files into the workspace from the page: drop a song, a DI, a tab."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, File, UploadFile

from ...config import get_settings, require_writable
from ...core.errors import Invalid
from ..api import _allow_audio, _audio_url
from ..plain import PlainRoute

router = APIRouter(prefix="/api", route_class=PlainRoute)

#: What may be uploaded, by extension. Audio, MIDI and captures - not code.
ALLOWED = {".wav", ".flac", ".mp3", ".ogg", ".aif", ".aiff", ".m4a", ".aac", ".opus", ".mid", ".midi", ".nam", ".txt"}

#: 400 MB: a long lossless album track is well under; a video is not welcome.
MAX_BYTES = 400 * 1024 * 1024


@router.post("/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    """Store an uploaded file under ``workspace/uploads`` and return its path."""
    require_writable("upload a file")
    name = Path(file.filename or "upload").name
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED:
        raise Invalid(f"'{suffix or name}' files are not accepted here", hint="Audio, MIDI or .nam captures.")
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip() or "upload" + suffix
    settings = get_settings()
    settings.ensure_workspace()
    directory = settings.workspace / "uploads"
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / safe
    counter = 2
    while destination.exists():
        destination = directory / f"{Path(safe).stem}-{counter}{suffix}"
        counter += 1
    size = 0
    with open(destination, "wb") as handle:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_BYTES:
                handle.close()
                destination.unlink(missing_ok=True)
                raise Invalid("that file is over 400 MB")
            handle.write(chunk)
    _allow_audio(destination)
    return {"path": str(destination), "name": destination.name, "size": size, "url": _audio_url(destination)}

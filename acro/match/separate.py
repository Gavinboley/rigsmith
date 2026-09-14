"""Isolate the guitar from a mixed recording.

Uses Demucs' ``htdemucs_6s`` model, which is the only widely available open
model with a first-class **guitar** stem (the standard four-stem models put
guitar into "other"). It runs entirely offline.

**What a separated stem actually is.** It is not a DI and not a clean target.
It still carries the original amp, cabinet, microphone, room, and whatever
the mix engineer did afterwards - compression, EQ, saturation, reverb - plus
separation artefacts. It is a *timbral reference*: good for ranking
candidates by character, useless as ground truth for sample-wise error. The
scoring layer treats it accordingly.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: The six-stem model. The four-stem default has no guitar stem.
DEFAULT_MODEL = "htdemucs_6s"

#: Stems htdemucs_6s produces.
SIX_STEMS = ("vocals", "drums", "bass", "guitar", "piano", "other")


class SeparationUnavailable(RuntimeError):
    """Demucs is not installed or not runnable."""


@dataclass
class SeparationResult:
    """Where the separated stems ended up."""

    source: Path
    output_dir: Path
    stems: dict[str, Path] = field(default_factory=dict)
    model: str = DEFAULT_MODEL
    warnings: list[str] = field(default_factory=list)

    @property
    def guitar(self) -> Path | None:
        return self.stems.get("guitar")

    def describe(self) -> str:
        lines = [f"{self.source.name} -> {len(self.stems)} stems ({self.model})"]
        for name, path in sorted(self.stems.items()):
            marker = "  <- guitar reference" if name == "guitar" else ""
            lines.append(f"  {name:8} {path}{marker}")
        lines.extend(f"  warning: {w}" for w in self.warnings)
        return "\n".join(lines)


def demucs_available() -> bool:
    """Whether Demucs can be invoked."""
    if shutil.which("demucs"):
        return True
    try:
        import demucs  # noqa: F401

        return True
    except ImportError:
        return False


def separate(
    audio_path: str | Path,
    output_dir: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    stems: tuple[str, ...] | None = ("guitar",),
    timeout_s: float = 1800.0,
) -> SeparationResult:
    """Split a recording into stems.

    Args:
        audio_path: The mixed recording.
        output_dir: Where stems are written.
        model: Demucs model; keep the six-stem default for a guitar stem.
        stems: Restrict to these stems for speed. ``None`` extracts all six.
        timeout_s: Demucs is slow on CPU - minutes per track is normal.

    Raises:
        SeparationUnavailable: if Demucs is not installed.
    """
    audio_path = Path(audio_path)
    output_dir = Path(output_dir)

    if not audio_path.exists():
        raise FileNotFoundError(f"no such audio file: {audio_path}")
    if not demucs_available():
        raise SeparationUnavailable(
            "Demucs is not installed. Install it with:\n"
            "  pip install -U demucs\n\n"
            f"Then acro can isolate a guitar stem with the {DEFAULT_MODEL} model. "
            "Note it is slow on CPU - expect minutes per track."
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable, "-m", "demucs",
        "-n", model,
        "-o", str(output_dir),
    ]
    if stems and len(stems) == 1:
        command += ["--two-stems", stems[0]]
    command.append(str(audio_path))

    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SeparationUnavailable(
            f"Demucs timed out after {timeout_s:.0f}s on {audio_path.name}."
        ) from exc

    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()
        detail = "\n".join(tail[-6:]) if tail else "no output"
        raise SeparationUnavailable(
            f"Demucs exited {completed.returncode}:\n{detail}"
        )

    result = SeparationResult(source=audio_path, output_dir=output_dir, model=model)
    stem_root = output_dir / model / audio_path.stem
    if stem_root.exists():
        for path in sorted(stem_root.glob("*.wav")):
            result.stems[path.stem] = path

    if "guitar" not in result.stems:
        found = ", ".join(sorted(result.stems)) or "none"
        result.warnings.append(
            f"no guitar stem was produced (got: {found}). The six-stem model "
            f"'{DEFAULT_MODEL}' is required for a guitar stem; four-stem models "
            "fold guitar into 'other'."
        )

    result.warnings.append(
        "A separated stem retains the record's amp, cab, mic, and mix "
        "processing, plus separation artefacts. Treat it as a timbral "
        "reference for ranking, not as ground truth."
    )
    return result

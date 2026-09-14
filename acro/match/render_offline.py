"""Layer 1: render a DI through a candidate chain in Python.

Fast, needs no hardware, and approximate. Use it to narrow a large field of
candidate captures down to a shortlist; then confirm the shortlist on the
actual device (:mod:`acro.match.render_device`), because only the Core's own
DSP is the Core's tone.

What this can do faithfully is cabinet IR convolution - that is a plain
linear convolution and matches the hardware closely. NAM inference is
delegated to the reference ``neural-amp-modeler`` package when it is
installed; reimplementing WaveNet inference here would be a second,
subtly-different implementation of somebody else's model, and a capture that
renders differently offline than on the device is worse than no offline
rendering at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .metrics import normalise_rms, to_mono


class RenderUnavailable(RuntimeError):
    """A required optional dependency is missing."""


@dataclass
class RenderResult:
    """Rendered audio plus what actually happened to produce it."""

    audio: np.ndarray
    sample_rate: int
    stages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def exact(self) -> bool:
        """True only if every stage was rendered rather than approximated."""
        return not self.warnings

    def describe(self) -> str:
        lines = [f"{len(self.audio)} samples @ {self.sample_rate} Hz"]
        lines.append("chain: " + " -> ".join(self.stages or ["(passthrough)"]))
        lines.extend(f"  warning: {w}" for w in self.warnings)
        return "\n".join(lines)


# --------------------------------------------------------------------------
# audio I/O
# --------------------------------------------------------------------------


def load_audio(path: str | Path, *, mono: bool = True) -> tuple[np.ndarray, int]:
    """Load an audio file. Requires ``soundfile``."""
    try:
        import soundfile as sf
    except ImportError as exc:
        raise RenderUnavailable(
            "soundfile is required to read audio. Install with: "
            "pip install 'acro[audio]'"
        ) from exc

    data, sample_rate = sf.read(str(path), always_2d=False)
    if mono:
        data = to_mono(data)
    return np.asarray(data, dtype=np.float64), int(sample_rate)


def save_audio(path: str | Path, audio: np.ndarray, sample_rate: int) -> Path:
    """Write audio to a file. Requires ``soundfile``."""
    try:
        import soundfile as sf
    except ImportError as exc:
        raise RenderUnavailable(
            "soundfile is required to write audio. Install with: "
            "pip install 'acro[audio]'"
        ) from exc

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.asarray(audio, dtype=np.float32), sample_rate)
    return path


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    """Resample, preferring a proper polyphase filter when scipy is present."""
    if source_rate == target_rate:
        return audio
    try:
        from math import gcd

        from scipy.signal import resample_poly

        divisor = gcd(source_rate, target_rate)
        return resample_poly(audio, target_rate // divisor, source_rate // divisor)
    except ImportError:
        # Linear interpolation is audibly worse, but better than refusing.
        duration = len(audio) / source_rate
        target_length = int(duration * target_rate)
        return np.interp(
            np.linspace(0, len(audio) - 1, target_length),
            np.arange(len(audio)),
            audio,
        )


# --------------------------------------------------------------------------
# stages
# --------------------------------------------------------------------------


def convolve_ir(
    signal: np.ndarray,
    impulse: np.ndarray,
    *,
    truncate_samples: int | None = None,
    trim_to_input: bool = True,
) -> np.ndarray:
    """Convolve a signal with a cabinet impulse response.

    Args:
        truncate_samples: Shorten the IR first. The Core offers a 1024-sample
            truncated mode that costs less DSP; pass 1024 to mirror it.
        trim_to_input: Return the same length as the input, as a real cab
            block does, rather than the full convolution tail.
    """
    signal = to_mono(signal)
    impulse = to_mono(impulse)

    if truncate_samples is not None and len(impulse) > truncate_samples:
        impulse = impulse[:truncate_samples]
        # Fade the new end so truncation does not add a click.
        fade = min(64, len(impulse) // 8)
        if fade > 0:
            impulse = impulse.copy()
            impulse[-fade:] *= np.linspace(1.0, 0.0, fade)

    try:
        from scipy.signal import fftconvolve

        wet = fftconvolve(signal, impulse, mode="full")
    except ImportError:
        wet = np.convolve(signal, impulse, mode="full")

    return wet[: len(signal)] if trim_to_input else wet


def apply_nam(signal: np.ndarray, nam_path: str | Path, sample_rate: int) -> np.ndarray:
    """Run a signal through a NAM capture.

    Delegates to the reference ``neural-amp-modeler`` package. Raises
    :class:`RenderUnavailable` when it is not installed, rather than
    approximating - an approximation of somebody's amp capture is not useful
    for deciding which capture matches a record.
    """
    try:
        from nam.models import Model  # type: ignore
    except ImportError as exc:
        raise RenderUnavailable(
            "Offline NAM rendering needs the reference implementation:\n"
            "  pip install neural-amp-modeler\n\n"
            "Without it, use hardware-in-the-loop rendering instead - that is "
            "the device's real output and is more trustworthy anyway."
        ) from exc

    model = Model.load_from_checkpoint(str(nam_path)) if str(nam_path).endswith(".ckpt") else None
    if model is None:
        from nam.models.base import BaseNet  # type: ignore

        model = BaseNet.load(str(nam_path))

    import torch  # type: ignore

    model.eval()
    with torch.no_grad():
        tensor = torch.tensor(np.asarray(signal, dtype=np.float32))
        output = model(tensor).cpu().numpy()
    return np.asarray(output, dtype=np.float64)


def render_chain(
    di: np.ndarray,
    sample_rate: int,
    *,
    nam_path: str | Path | None = None,
    ir_path: str | Path | None = None,
    ir_truncate: int | None = None,
    output_gain_db: float = 0.0,
    normalise: bool = True,
) -> RenderResult:
    """Render a DI through an optional NAM capture and cabinet IR.

    Any stage that cannot run is recorded as a warning and skipped, so a
    partial render still returns something usable - clearly labelled as
    partial rather than silently passed off as complete.
    """
    audio = to_mono(np.asarray(di, dtype=np.float64))
    result = RenderResult(audio=audio, sample_rate=sample_rate)

    if nam_path:
        try:
            audio = apply_nam(audio, nam_path, sample_rate)
            result.stages.append(f"NAM({Path(nam_path).stem})")
        except RenderUnavailable as exc:
            result.warnings.append(f"NAM stage skipped: {exc.args[0].splitlines()[0]}")
        except Exception as exc:  # the model may be malformed or a bad version
            result.warnings.append(f"NAM stage failed: {type(exc).__name__}: {exc}")

    if ir_path:
        try:
            impulse, ir_rate = load_audio(ir_path)
            if ir_rate != sample_rate:
                impulse = resample(impulse, ir_rate, sample_rate)
            audio = convolve_ir(audio, impulse, truncate_samples=ir_truncate)
            label = f"IR({Path(ir_path).stem}"
            label += f", {ir_truncate})" if ir_truncate else ")"
            result.stages.append(label)
        except (RenderUnavailable, OSError) as exc:
            result.warnings.append(f"IR stage skipped: {exc}")

    if output_gain_db:
        audio = audio * (10 ** (output_gain_db / 20))
        result.stages.append(f"gain({output_gain_db:+.1f} dB)")

    if normalise:
        audio = normalise_rms(audio)

    result.audio = audio
    if not result.stages:
        result.warnings.append(
            "nothing was rendered - the output is the unprocessed DI"
        )
    return result

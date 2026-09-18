"""What can be read in, and what to say about it.

Everything this tool measures arrives as a file somebody recorded somewhere -
a phone voice memo, an mp3 of the record, a loop played into the pedal. So
the reading end has to be generous about formats and specific about
consequences, because the consequences differ:

- **Lossy** sources (mp3, ogg, m4a) throw away high-frequency detail and add
  pre-echo, which lands exactly where "fizz" lives. Fine as a target to chase.
  Not fine as the DI you reamp, where those artefacts get printed into every
  candidate equally and then measured as if they were tone.
- **Sample rate** below the device's 48 kHz cannot be undone. A 44.1 kHz
  source is fine; a 22 kHz one has no top octave to compare.
- **Clipping** looks like distortion to every metric here, which is the one
  thing they are built to measure.

None of that stops a comparison. All of it changes how much to believe it, so
it is reported rather than silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..core.errors import Invalid, NotFound, Unavailable

#: Formats that are lossy no matter how they were encoded.
LOSSY_FORMATS = {"MP3", "OGG", "VORBIS", "OPUS", "AAC", "MPEG"}

#: The device runs at 48 kHz.
DEVICE_SAMPLE_RATE = 48000

#: The rate below which a source starts losing content this tool measures.
#:
#: Not the device's rate. 44.1 kHz is a CD and most mp3s of records, and
#: complaining about every one of them would be noise - which trains people
#: to ignore the notes that matter. What matters is the Nyquist falling into
#: the bands being compared: the top guitar band runs to 11 kHz, so 32 kHz
#: (16 kHz of audio) still covers everything measured, and 22 kHz does not.
MIN_USEFUL_SAMPLE_RATE = 32000

#: Fraction of samples in a flat-topped run before a take is called clipped.
CLIP_FRACTION = 0.0005

#: How many consecutive samples at full scale count as a flat top.
#:
#: Counting every sample that touches 1.0 gets this wrong on exactly the
#: material it matters for: a hard-driven guitar is soft-saturated and grazes
#: full scale constantly without being clipped. Digital clipping is a flat
#: top - the waveform stops and stays there - so that is what is looked for.
CLIP_RUN = 3

#: Below this peak, a take is quiet enough that its noise floor starts to
#: matter relative to its signal.
QUIET_PEAK = 0.05


def readable_formats() -> dict[str, str]:
    """What this install can actually open, asked of the library itself.

    Hardcoding a list would be a lie on somebody's machine: libsndfile gained
    mp3 in 1.1, so the same rigsmith reads mp3 on one install and not on
    another. Asking means the error message is true where it is printed.
    """
    try:
        import soundfile as sf
    except ImportError:
        return {}
    return dict(sf.available_formats())


def readable_suffixes() -> list[str]:
    """Extensions worth offering in a file picker, for this install."""
    names = set(readable_formats())
    mapping = {
        "WAV": [".wav"],
        "WAVEX": [".wav"],
        "RF64": [".wav"],
        "W64": [".w64"],
        "AIFF": [".aiff", ".aif", ".aifc"],
        "FLAC": [".flac"],
        "OGG": [".ogg", ".oga"],
        "MP3": [".mp3"],
        "CAF": [".caf"],
        "AU": [".au", ".snd"],
        "MAT5": [],
    }
    found: list[str] = []
    for name, suffixes in mapping.items():
        if name in names:
            found.extend(s for s in suffixes if s not in found)
    return sorted(found)


@dataclass
class AudioSource:
    """One audio file, and everything worth knowing before trusting it."""

    path: Path
    sample_rate: int
    channels: int
    frames: int
    format: str = ""
    subtype: str = ""
    peak: float = 0.0
    clipped_fraction: float = 0.0

    @property
    def duration(self) -> float:
        return self.frames / self.sample_rate if self.sample_rate else 0.0

    @property
    def lossy(self) -> bool:
        return any(name in self.format.upper() for name in LOSSY_FORMATS)

    @property
    def clipped(self) -> bool:
        return self.clipped_fraction > CLIP_FRACTION

    @property
    def quiet(self) -> bool:
        return 0.0 < self.peak < QUIET_PEAK

    def concerns(self, *, aligned: bool = False, as_di: bool = False) -> list[str]:
        """What to say about this file, in the context it is being used in.

        Empty when there is nothing to say - which is most of the time, and
        is why saying something means something.
        """
        notes: list[str] = []

        if self.lossy and as_di:
            notes.append(
                f"{self.path.name} is {self.format} - lossy. As a DI that is "
                "worth avoiding: the encoder's pre-echo and missing top end "
                "get printed into every candidate you render through it, and "
                "then measured as though they were tone."
            )
        elif self.lossy and aligned:
            notes.append(
                f"{self.path.name} is {self.format}. Lossy encoding shifts "
                "fine detail, which aligned mode measures sample by sample. "
                "The ranking will still be usable; the absolute ESR will read "
                "worse than the rig deserves."
            )

        if self.sample_rate < MIN_USEFUL_SAMPLE_RATE:
            notes.append(
                f"{self.path.name} is {self.sample_rate} Hz, so it holds "
                f"nothing above {self.sample_rate // 2} Hz. The bite and fizz "
                "bands run past that, so they cannot be compared and this "
                "will read as darker than it is."
            )

        if self.clipped:
            notes.append(
                f"{self.path.name} is clipped ({self.clipped_fraction:.1%} of "
                "samples at full scale). Clipping is distortion, and "
                "distortion is the thing these metrics measure - it will look "
                "like tone."
            )

        if self.quiet:
            notes.append(
                f"{self.path.name} peaks at {self.peak:.3f}. Levels are "
                "normalised before comparison, so this is not fatal, but a "
                "take this quiet brings its noise floor up with it."
            )

        if self.channels > 2:
            notes.append(
                f"{self.path.name} has {self.channels} channels; they are "
                "averaged to mono before anything is measured."
            )
        return notes

    def describe(self) -> str:
        bits = [
            self.path.name,
            f"{self.duration:.1f}s",
            f"{self.sample_rate} Hz",
            "mono" if self.channels == 1 else f"{self.channels}ch",
        ]
        if self.format:
            bits.append(self.format.lower())
        if self.lossy:
            bits.append("lossy")
        return "  ".join(bits)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.path.name,
            "duration": self.duration,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self.frames,
            "format": self.format,
            "subtype": self.subtype,
            "peak": self.peak,
            "lossy": self.lossy,
            "clipped": self.clipped,
            "quiet": self.quiet,
            "concerns": self.concerns(),
        }


def inspect_audio(path: str | Path, *, measure: bool = True) -> AudioSource:
    """Read a file's header, and optionally its samples, without keeping them.

    Args:
        measure: Also scan for peak and clipping. Costs a read of the file;
            skip it when only the shape matters.
    """
    sf = _soundfile()
    target = Path(path)
    if not target.exists():
        raise NotFound(f"no audio file at {target}")

    try:
        info = sf.info(str(target))
    except Exception as exc:
        raise _unreadable(target, exc) from exc

    source = AudioSource(
        path=target,
        sample_rate=int(info.samplerate),
        channels=int(info.channels),
        frames=int(info.frames),
        format=str(info.format or ""),
        subtype=str(info.subtype or ""),
    )

    if measure and source.frames:
        try:
            peak = 0.0
            flat = 0
            total = 0
            for block in sf.blocks(str(target), blocksize=1 << 18, always_2d=True):
                magnitude = np.abs(np.asarray(block, dtype=np.float64))
                if not magnitude.size:
                    continue
                peak = max(peak, float(magnitude.max()))
                total += magnitude.shape[0]
                flat += _flat_top_samples(magnitude.mean(axis=1))
            source.peak = peak
            source.clipped_fraction = flat / total if total else 0.0
        except Exception:
            # Measuring is a nicety; a header that parsed is enough to work
            # with, and refusing the file over it would be worse.
            pass
    return source


def _flat_top_samples(magnitude: np.ndarray) -> int:
    """Samples belonging to a run of CLIP_RUN or more at full scale."""
    at_rail = magnitude >= 0.999
    if not at_rail.any():
        return 0

    count = 0
    run = 0
    for value in at_rail:
        if value:
            run += 1
        else:
            if run >= CLIP_RUN:
                count += run
            run = 0
    if run >= CLIP_RUN:
        count += run
    return count


def _soundfile():
    try:
        import soundfile as sf
    except ImportError as exc:
        raise Unavailable(
            "reading audio needs soundfile",
            hint="Install it with:  pip install 'rigsmith[match]'",
        ) from exc
    return sf


def _unreadable(path: Path, exc: Exception) -> Invalid:
    """A refusal that names what this install *can* read."""
    suffixes = readable_suffixes()
    offered = ", ".join(suffixes) if suffixes else "nothing - soundfile is broken"
    extra = ""
    if path.suffix.lower() in (".m4a", ".aac", ".mp4", ".wma"):
        extra = (
            f"\n{path.suffix.lower()} is not one libsndfile decodes. Convert it "
            "first - any player will export a .wav or .mp3 - or re-record it."
        )
    return Invalid(
        f"could not read {path.name}: {exc}",
        hint=f"This install reads: {offered}.{extra}",
    )


def load(path: str | Path, *, mono: bool = True) -> tuple[np.ndarray, int]:
    """Read a file to samples, refusing unreadable input with a real message."""
    sf = _soundfile()
    target = Path(path)
    if not target.exists():
        raise NotFound(f"no audio file at {target}")

    try:
        data, sample_rate = sf.read(str(target), always_2d=False)
    except Exception as exc:
        raise _unreadable(target, exc) from exc

    from .metrics import to_mono

    array = np.asarray(data, dtype=np.float64)
    if mono:
        array = to_mono(array)
    return array, int(sample_rate)


def concerns_for(
    paths, *, aligned: bool = False, di_paths=(),
) -> list[str]:
    """Collect what is worth saying about a set of files, without repeating.

    Args:
        paths: Everything being compared.
        aligned: Whether the comparison is sample-aligned.
        di_paths: Which of them are being used as a DI to render through.
    """
    di = {str(Path(p)) for p in di_paths}
    notes: list[str] = []
    for path in paths:
        try:
            source = inspect_audio(path)
        except (Invalid, NotFound, Unavailable):
            continue
        for note in source.concerns(aligned=aligned, as_di=str(Path(path)) in di):
            if note not in notes:
                notes.append(note)
    return notes

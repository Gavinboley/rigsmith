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
from functools import lru_cache
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


@lru_cache(maxsize=1)
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
    if _have_av():
        found.extend(s for s in CONTAINER_SUFFIXES if s not in found)
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
        # The codec, not just the container: an .m4a is AAC (lossy) or ALAC
        # (lossless), and only the codec says which.
        described = f"{self.format} {self.subtype}".upper()
        return any(name in described for name in LOSSY_FORMATS)

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


#: Inspections already made, keyed by identity-on-disk. Scoring twenty
#: candidates asks about the same files repeatedly - once to score, once for
#: the commentary, once more for the bands - and reading each one every time
#: is a second of nothing for no reason.
_CACHE: dict[tuple, "AudioSource"] = {}
_CACHE_LIMIT = 256


def _cache_key(path: Path, measure: bool) -> tuple | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    # Size and mtime together: a file edited in place stops matching, which
    # is the only way a stale entry could ever be wrong.
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns, measure)


def forget_cached() -> None:
    """Drop every remembered inspection. For tests, and for a long session."""
    _CACHE.clear()


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

    key = _cache_key(target, measure)
    if key is not None and key in _CACHE:
        return _CACHE[key]

    try:
        info = sf.info(str(target))
    except Exception as exc:
        if not _have_av():
            raise _unreadable(target, exc) from exc
        source = _inspect_with_av(target, measure=measure)
        if key is not None:
            _CACHE[key] = source
        return source

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

    if key is not None:
        if len(_CACHE) >= _CACHE_LIMIT:
            _CACHE.clear()
        _CACHE[key] = source
    return source


def _inspect_with_av(target: Path, *, measure: bool) -> AudioSource:
    """The same report for a file only FFmpeg can read."""
    import av

    try:
        with av.open(str(target)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise Invalid(f"{target.name} has no audio in it")
            rate = int(stream.codec_context.sample_rate or stream.rate or 0)
            channels = int(stream.codec_context.channels or 1)
            seconds = float(stream.duration * stream.time_base) if stream.duration else float((container.duration or 0) / 1_000_000)
            source = AudioSource(
                path=target, sample_rate=rate, channels=channels, frames=int(round(seconds * rate)),
                format=str(container.format.name or "").upper(), subtype=str(stream.codec_context.name or "").upper(),
            )
    except Invalid:
        raise
    except Exception as exc:
        raise _unreadable(target, exc) from exc
    if measure and source.frames:
        try:
            data, _ = _load_with_av(target)
            magnitude = np.abs(data if data.ndim == 2 else data[:, None])
            source.peak = float(magnitude.max())
            source.clipped_fraction = _flat_top_samples(magnitude.mean(axis=1)) / max(len(magnitude), 1)
            source.frames = len(magnitude)
        except Exception:
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
    if path.suffix.lower() in CONTAINER_SUFFIXES and not _have_av():
        extra = (
            f"\n{path.suffix.lower()} is not one libsndfile decodes. Install "
            "'rigsmith[codecs]' (FFmpeg through PyAV) to read it as it is, or "
            "convert it - any player will export a .wav or .flac."
        )
    return Invalid(
        f"could not read {path.name}: {exc}",
        hint=f"This install reads: {offered}.{extra}",
    )


#: What PyAV (FFmpeg) adds when it is installed: the formats a bought song
#: arrives in that libsndfile cannot decode.
CONTAINER_SUFFIXES = (".m4a", ".aac", ".mp4", ".alac", ".wma", ".opus", ".webm", ".mka")


def _have_av() -> bool:
    import importlib.util

    return importlib.util.find_spec("av") is not None


def _load_with_av(target: Path) -> tuple[np.ndarray, int]:
    """Decode with FFmpeg through PyAV - AAC, ALAC, WMA, Opus - to float samples."""
    import av

    with av.open(str(target)) as container:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            raise Invalid(f"{target.name} has no audio in it")
        rate = stream.codec_context.sample_rate or stream.rate
        resampler = av.AudioResampler(format="fltp", layout="stereo" if (stream.codec_context.channels or 1) > 1 else "mono", rate=rate)
        chunks = []
        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                chunks.append(converted.to_ndarray())
        for converted in resampler.resample(None):
            chunks.append(converted.to_ndarray())
    if not chunks:
        raise Invalid(f"{target.name} decoded to nothing")
    planar = np.concatenate(chunks, axis=1)  # channels x samples
    data = planar[0] if planar.shape[0] == 1 else planar.T
    return np.asarray(data, dtype=np.float64), int(rate)


def load(path: str | Path, *, mono: bool = True) -> tuple[np.ndarray, int]:
    """Read a file to samples, refusing unreadable input with a real message.

    libsndfile first; when it cannot (a bought .m4a is AAC) and PyAV is
    installed, FFmpeg decodes it instead.
    """
    sf = _soundfile()
    target = Path(path)
    if not target.exists():
        raise NotFound(f"no audio file at {target}")

    try:
        data, sample_rate = sf.read(str(target), always_2d=False)
    except Exception as exc:
        if not _have_av():
            raise _unreadable(target, exc) from exc
        try:
            data, sample_rate = _load_with_av(target)
        except Invalid:
            raise
        except Exception as av_exc:
            raise _unreadable(target, av_exc) from exc

    from .metrics import to_mono

    array = np.asarray(data, dtype=np.float64)
    if mono:
        array = to_mono(array)
    return array, int(sample_rate)


# --------------------------------------------------------------------------
# comparing like with like
# --------------------------------------------------------------------------


def _encoded_dir() -> Path:
    from ..config import get_settings

    settings = get_settings()
    settings.ensure_workspace()
    directory = settings.audio_dir / "encoded"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def matched_to(candidate: str | Path, target: str | Path) -> tuple[Path, str | None]:
    """A copy of ``candidate`` encoded the way ``target`` is, if that differs.

    This is not a nicety. Measured against an mp3 of itself, a lossless file
    scored far enough off to *invert* a three-way ranking: the right answer
    came last, because the codec's error was larger than the difference
    between the instruments and was not the same for each of them. Put both
    sides through the same encoder and the error is common to both, so it
    cancels - the same three-way comparison then ranked correctly, with the
    identical candidate at zero.

    Returns ``(path, note)``. The path is the candidate unchanged when
    nothing needs doing; the note says what was done when something did.
    """
    candidate_path = Path(candidate)
    try:
        target_source = inspect_audio(target, measure=False)
        candidate_source = inspect_audio(candidate_path, measure=False)
    except (Invalid, NotFound, Unavailable):
        return candidate_path, None

    if not target_source.lossy or candidate_source.lossy:
        return candidate_path, None

    fmt = target_source.format.upper()
    if "AAC" in target_source.subtype.upper() and _have_av():
        return _matched_aac(candidate_path, target_source)
    if fmt not in readable_formats():
        return candidate_path, None

    from ..config import get_settings

    if get_settings().readonly:
        return candidate_path, (
            f"{target_source.path.name} is {fmt} and {candidate_path.name} is "
            "not. Read-only mode cannot make the matching copy, so the "
            "codec's own error is in this comparison."
        )

    suffix = {"MP3": ".mp3", "OGG": ".ogg", "VORBIS": ".ogg"}.get(fmt)
    if suffix is None:
        return candidate_path, None

    try:
        stat = candidate_path.stat()
    except OSError:
        return candidate_path, None

    stem = f"{candidate_path.stem}-{fmt.lower()}-{stat.st_size}-{stat.st_mtime_ns}"
    destination = _encoded_dir() / f"{stem}{suffix}"
    note = (
        f"{candidate_path.name} was re-encoded as {fmt} to match "
        f"{target_source.path.name}. Comparing a lossless candidate against a "
        "lossy target measures the encoder as well as the tone, and that "
        "error is big enough to reorder candidates."
    )
    if destination.is_file():
        return destination, note

    try:
        samples, rate = load(candidate_path, mono=False)
        sf = _soundfile()
        sf.write(str(destination), samples.astype("float32"), rate, format=fmt)
    except Exception:
        # A codec that will not write is a reason to compare unmatched and
        # say so, not a reason to refuse the comparison.
        return candidate_path, (
            f"could not re-encode {candidate_path.name} as {fmt}, so the "
            "codec's own error is in this comparison"
        )
    return destination, note


#: What a bought AAC file is usually encoded at; the matching copy uses it.
AAC_BITRATE = 256_000


def _encode_aac(samples: np.ndarray, rate: int, destination: Path, *, bitrate: int = AAC_BITRATE) -> None:
    import av

    data = np.asarray(samples, dtype=np.float32)
    planar = data[None, :] if data.ndim == 1 else data.T.copy()
    layout = "mono" if planar.shape[0] == 1 else "stereo"
    with av.open(str(destination), "w", format="ipod") as container:
        stream = container.add_stream("aac", rate=rate, layout=layout)
        stream.bit_rate = bitrate
        stream.codec_context.open()
        # The encoder delays everything by its priming (1024 samples for
        # FFmpeg's AAC), and decoding here does not trim it. Starting the
        # input that much late puts the decoded copy back on the original's
        # clock - which performance scoring compares frame by frame.
        priming = int(getattr(stream.codec_context, "initial_padding", 0) or 1024)
        planar = np.concatenate([planar[:, priming:], np.zeros((planar.shape[0], priming), dtype=np.float32)], axis=1)
        step = 1024 * 16
        for start in range(0, planar.shape[1], step):
            frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(planar[:, start : start + step]), format="fltp", layout=layout)
            frame.sample_rate = rate
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)


def _matched_aac(candidate_path: Path, target_source) -> tuple[Path, str | None]:
    """A candidate encoded as AAC, to compare fairly against a bought .m4a."""
    try:
        stat = candidate_path.stat()
    except OSError:
        return candidate_path, None
    destination = _encoded_dir() / f"{candidate_path.stem}-aac-{stat.st_size}-{stat.st_mtime_ns}.m4a"
    note = (
        f"{candidate_path.name} was re-encoded as AAC to match {target_source.path.name}, "
        "so the codec's error is common to both sides instead of reordering candidates."
    )
    if destination.is_file():
        return destination, note
    try:
        samples, rate = load(candidate_path, mono=False)
        _encode_aac(samples, rate, destination)
    except Exception:
        destination.unlink(missing_ok=True)
        return candidate_path, f"could not re-encode {candidate_path.name} as AAC, so the codec's own error is in this comparison"
    return destination, note


def matched_set(candidates: dict, target) -> tuple[dict, list[str]]:
    """Encode every candidate to the target's format where that differs."""
    matched: dict = {}
    notes: list[str] = []
    for label, path in candidates.items():
        resolved, note = matched_to(path, target)
        matched[label] = resolved
        if note and note not in notes:
            notes.append(note)
    return matched, notes


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

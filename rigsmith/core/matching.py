"""Tone matching: isolate, render, score, and listen.

This is where the three render layers finally meet one interface:

1. :func:`render_offline` - a DI through a NAM capture and cab IR in Python.
   Fast, hardware-free, approximate. This existed from the start and was
   reachable from nothing, which meant the documented "three layers" was
   really two.
2. :func:`reamp` - the same DI through the actual Core over USB. Slow, and
   the only output that is genuinely the device's tone.
3. :func:`create_ab` and friends - blind listening, which decides.

:func:`run_offline_search` is the loop the whole design implies but nothing
previously joined up: take one DI, render it through every candidate capture,
score them all against a target, and hand back a ranking - with no hardware
and no per-candidate manual step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import get_settings, require_writable
from .errors import Invalid, NotFound, Unavailable
from .jobs import Progress


def _audio_dir() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    return settings.audio_dir


# --------------------------------------------------------------------------
# audio devices
# --------------------------------------------------------------------------


def audio_devices():
    from ..match.render_device import list_devices

    return list_devices()


def render_audio_devices() -> str:
    from ..match.render_device import (
        AudioUnavailable,
        DRY_GUITAR_CHANNEL,
        find_headrush_device,
        list_devices,
    )

    devices = list_devices()
    lines = [f"{len(devices)} audio device(s):"]
    lines.extend(f"  {d.describe()}" for d in devices)
    try:
        headrush = find_headrush_device()
        lines.append(f"\nHeadRush: {headrush.describe()}")
        if not headrush.has_full_channel_layout:
            lines.append(
                f"  WARNING: fewer than {DRY_GUITAR_CHANNEL} input channels "
                "visible, so the dry DI is not reachable. Check the Core's "
                "USB audio mode."
            )
    except AudioUnavailable as exc:
        lines.append(f"\n{exc}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# separation
# --------------------------------------------------------------------------


def isolate_guitar(
    audio_path: str | Path,
    *,
    out_dir: str | Path | None = None,
    progress: Progress | None = None,
):
    """Separate a guitar stem from a mixed recording with Demucs.

    Slow - minutes per track on CPU - so this reports progress when given a
    :class:`~rigsmith.core.jobs.Progress` handle.

    The result is a timbral reference, not a clean target: it still carries
    the record's amp, cab, mic, and mix, plus separation artefacts.
    """
    from ..match.separate import separate

    require_writable("write separated stems")
    destination = Path(out_dir) if out_dir else _audio_dir() / "stems"
    if progress:
        progress.update(
            None, f"separating {Path(audio_path).name} - this takes minutes on CPU"
        )
    result = separate(audio_path, destination)
    if progress:
        progress.update(1.0, f"{len(result.stems)} stems written")
    return result


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------


@dataclass
class RenderedCandidate:
    """One rendered candidate and where its audio landed."""

    label: str
    path: Path
    layer: str
    """``offline`` or ``hardware``."""
    stages: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    safe_for_aligned: bool = True

    def applied(self, stage: str) -> bool:
        """Did a named stage actually run?

        ``render_chain`` skips a stage it cannot run and says so in a warning,
        which is right for a single render - a partial result is still worth
        hearing. In a sweep it is not: every candidate whose capture failed to
        load renders to the same unprocessed DI, so they all score identically
        and the ranking looks authoritative while meaning nothing.
        """
        return any(entry.startswith(stage) for entry in self.stages)

    def render(self) -> str:
        lines = [f"{self.label}  [{self.layer}]  {self.path}"]
        if self.stages:
            lines.append("  chain: " + " -> ".join(self.stages))
        lines.extend(f"  warning: {w}" for w in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "path": str(self.path),
            "layer": self.layer,
            "stages": self.stages,
            "warnings": self.warnings,
            "safe_for_aligned": self.safe_for_aligned,
        }


def render_offline(
    di_path: str | Path,
    *,
    nam_path: str | Path | None = None,
    ir_path: str | Path | None = None,
    ir_truncate: int | None = None,
    label: str = "candidate",
    out_dir: str | Path | None = None,
) -> RenderedCandidate:
    """Layer 1: render a DI through a NAM capture and cab IR in Python.

    No hardware needed. Use it to narrow a large field of captures before
    spending real-time reamps on the shortlist.
    """
    from ..match.render_offline import load_audio, render_chain, save_audio

    require_writable("write rendered audio")
    di_path = audio_path(di_path)
    if not nam_path and not ir_path:
        raise Invalid(
            "nothing to render through",
            hint="Give a nam_path, an ir_path, or both.",
        )

    di, sample_rate = load_audio(di_path)
    result = render_chain(
        di,
        sample_rate,
        nam_path=nam_path,
        ir_path=ir_path,
        ir_truncate=ir_truncate,
    )
    destination = Path(out_dir) if out_dir else _audio_dir()
    path = save_audio(destination / f"{label}-offline.wav", result.audio, sample_rate)

    return RenderedCandidate(
        label=label,
        path=path,
        layer="offline",
        stages=result.stages,
        warnings=result.warnings,
    )


def capture_di(duration_s: float = 15.0, *, name: str = "take"):
    """Record one performance: the wet tone and the dry DI together.

    Play the part once. The dry DI on channel 5 then reamps through unlimited
    candidates, so more candidates cost compute, not playing time.
    """
    from ..match.render_device import capture_performance
    from ..match.render_offline import save_audio

    require_writable("record audio to the workspace")
    capture = capture_performance(duration_s)

    paths: dict[str, Path] = {}
    directory = _audio_dir()
    paths["wet"] = save_audio(
        directory / f"{name}-wet.wav", capture.wet, capture.sample_rate
    )
    if capture.dry is not None:
        paths["dry"] = save_audio(
            directory / f"{name}-dry.wav", capture.dry, capture.sample_rate
        )
    return capture, paths


def reamp(di_path: str | Path, *, label: str = "candidate") -> RenderedCandidate:
    """Layer 2: play a DI through the Core's loaded rig and record the result.

    Goes through the hardware loop's default route when one is set up (see
    :mod:`rigsmith.core.loop`) - an interface into the Core's guitar input, or
    the Core's own USB - with every capture aligned from its sync marks. With
    no route, falls back to the Core's USB reamp channel directly. Load the
    rig you want to test on the device first - or use
    :func:`rigsmith.core.devices.select_rig`.
    """
    from ..match.render_device import reamp as _reamp
    from ..match.render_offline import load_audio, save_audio

    require_writable("record audio to the workspace")
    from . import loop as hardware_loop

    if hardware_loop.list_routes():
        result = hardware_loop.reamp(audio_path(di_path), label=label)
        capture = result.capture
        warnings = list(capture.notes) + list(result.notes)
        if result.simulated:
            warnings.append("this capture came from the simulated bench, not the Core")
        return RenderedCandidate(
            label=label,
            path=result.path,
            layer="hardware",
            stages=[f"device rig via {result.route_id}"],
            warnings=warnings,
            safe_for_aligned=capture.aligned,
        )
    di, sample_rate = load_audio(di_path)
    capture = _reamp(di, sample_rate=sample_rate)
    path = save_audio(
        _audio_dir() / f"{label}-reamped.wav", capture.wet, capture.sample_rate
    )

    warnings = list(capture.notes)
    if not capture.safe_for_aligned_scoring:
        warnings.append(
            "latency was not measured - score this capture in distribution "
            "mode only"
        )
    return RenderedCandidate(
        label=label,
        path=path,
        layer="hardware",
        stages=["device rig"],
        warnings=warnings,
        safe_for_aligned=capture.safe_for_aligned_scoring,
    )


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# regions
# --------------------------------------------------------------------------

#: Below this, a window carries too few FFT frames for the spectral metrics to
#: mean anything - 2048-sample windows at 48 kHz are ~43 ms each.
MIN_REGION_SECONDS = 0.25


@dataclass(frozen=True)
class Region:
    """A span of one file, in seconds.

    Scoring a whole track averages its clean intro together with its solo,
    which is how a candidate that matches neither can win. Naming the span you
    actually care about is the single cheapest accuracy improvement available
    here, so it is a first-class argument rather than something you pre-trim
    by hand.
    """

    start: float = 0.0
    end: float | None = None

    def __post_init__(self) -> None:
        if self.start < 0:
            raise Invalid(f"region start {self.start}s is before the file begins")
        if self.end is not None:
            if self.end <= self.start:
                raise Invalid(
                    f"region ends ({self.end}s) at or before it starts "
                    f"({self.start}s)"
                )
            if self.end - self.start < MIN_REGION_SECONDS:
                raise Invalid(
                    f"region is {self.end - self.start:.3f}s long, too short to "
                    "measure",
                    hint=f"Select at least {MIN_REGION_SECONDS}s.",
                )

    @property
    def duration(self) -> float | None:
        return None if self.end is None else self.end - self.start

    @classmethod
    def parse(cls, value) -> Region | None:
        """Accept ``None``, a ``(start, end)`` pair, or a ``"start:end"`` string."""
        if value is None:
            return None
        if isinstance(value, Region):
            return value
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            head, _, tail = text.partition(":")
            if not _:
                raise Invalid(
                    f"could not read region '{value}'",
                    hint="Write it as start:end in seconds, for example 41.2:58.0",
                )
            try:
                return cls(float(head or 0), float(tail) if tail else None)
            except ValueError:
                raise Invalid(
                    f"could not read region '{value}'",
                    hint="Both ends must be numbers of seconds, e.g. 41.2:58.0",
                ) from None
        try:
            start, end = value
        except (TypeError, ValueError):
            raise Invalid(f"could not read region {value!r}") from None
        return cls(float(start), None if end is None else float(end))

    def slice(self, audio, sample_rate: int, *, label: str = "audio"):
        """Cut this span out of ``audio``, refusing a span that is not there."""
        total = len(audio) / sample_rate
        if self.start >= total:
            raise Invalid(
                f"{label} is {total:.2f}s long, so a region starting at "
                f"{self.start:.2f}s selects nothing"
            )
        first = int(round(self.start * sample_rate))
        last = len(audio) if self.end is None else int(round(self.end * sample_rate))
        cut = audio[first:min(last, len(audio))]
        if len(cut) < MIN_REGION_SECONDS * sample_rate:
            raise Invalid(
                f"the selected region of {label} is only "
                f"{len(cut) / sample_rate:.3f}s long once clipped to the file",
                hint=f"Select at least {MIN_REGION_SECONDS}s that exists in the file.",
            )
        return cut

    def describe(self) -> str:
        end = "end" if self.end is None else f"{self.end:.2f}s"
        return f"{self.start:.2f}s-{end}"


def _di_or_default(reference) -> Path:
    """The DI to render through, falling back to the library's own.

    Naming a file every time is the friction the takes library exists to
    remove: once your playing is in there, "render these captures" needs no
    filename at all.
    """
    from . import takes

    if reference:
        return takes.resolve(reference)

    default = takes.default_di()
    if default is None:
        raise Invalid(
            "no DI given, and the takes library has none to fall back on",
            hint=(
                "Add one with:  rigsmith take add <file> --kind di --guitar '...'"
                "\nOr record one over USB:  rigsmith match capture-di"
            ),
        )
    return default.file


def audio_path(reference) -> Path:
    """Accept a take id or a path, and return a path.

    Applied at every entry point that takes audio, so "cherub-riff" works
    wherever "C:/takes/cherub-riff.wav" does. The alternative is remembering
    where you put a file, which is the thing the library exists to stop.
    """
    from . import takes

    return takes.resolve(reference)


#: How long an automatically chosen passage is, in seconds. Long enough to
#: average over several notes, short enough not to wander into a different
#: part of the song.
AUTO_REGION_SECONDS = 8.0

#: Hop between the windows the search walks, in seconds.
_AUTO_HOP = 0.1


def suggest_region(path, *, seconds: float = AUTO_REGION_SECONDS) -> Region | None:
    """The busiest continuous passage of a file.

    Finding the solo by hand means scrubbing a waveform and reading a clock,
    which is a thing people do once and then stop doing. This picks the window
    carrying the most energy - in practice the part that is actually being
    played rather than a count-in, a held note, or the tail of a fade.

    It is a starting point, not a judgement. Drag the region if it chose
    wrong; that is why the waveform is there.

    Returns ``None`` when the file is shorter than the window, which is not a
    failure - it means the whole thing already is the passage.
    """
    import numpy as np

    from ..match.render_offline import load_audio

    audio, sample_rate = load_audio(audio_path(path))
    total = len(audio) / sample_rate
    if total <= seconds + MIN_REGION_SECONDS:
        return None

    hop = max(1, int(_AUTO_HOP * sample_rate))
    window = max(1, int(seconds * sample_rate))

    # Energy per hop, then a running sum over the window. Summing squares
    # rather than amplitudes so a loud passage counts for more than a long
    # quiet one, which is the distinction that matters here.
    squares = np.asarray(audio, dtype=np.float64) ** 2
    usable = (len(squares) // hop) * hop
    per_hop = squares[:usable].reshape(-1, hop).sum(axis=1)
    hops_per_window = max(1, window // hop)
    if len(per_hop) <= hops_per_window:
        return None

    cumulative = np.concatenate([[0.0], np.cumsum(per_hop)])
    totals = cumulative[hops_per_window:] - cumulative[:-hops_per_window]
    best = int(np.argmax(totals))

    start = best * hop / sample_rate
    end = min(total, start + seconds)
    if end - start < MIN_REGION_SECONDS:
        return None
    return Region(round(start, 3), round(end, 3))


def resolve_region(value, path):
    """Parse a region, resolving the literal ``auto`` against a file."""
    if isinstance(value, str) and value.strip().lower() == "auto":
        return suggest_region(path)
    return Region.parse(value)


def parse_mode(value: str):
    from ..match.metrics import ComparisonMode

    try:
        return ComparisonMode(str(value).lower())
    except ValueError:
        raise Invalid(
            f"unknown comparison mode '{value}'",
            hint=(
                "Use 'aligned' only when target and candidates are the same "
                "performance (your own DI through each rig). Use "
                "'performance' when the DI was rendered from the record's own "
                "notes on its clock (rigsmith learn part). Use 'distribution' "
                "against anything else separated from a record."
            ),
        ) from None


def score(
    target_path: str | Path,
    candidate_paths: dict[str, str | Path],
    *,
    mode: str = "distribution",
    target_region=None,
    candidate_region=None,
):
    """Score rendered candidates against a target and rank them.

    In distribution mode the sample-wise metrics are not computed at all:
    against a different performance they are artefacts of an arbitrary
    alignment. See docs/TONE_MATCHING.md.

    ``target_region`` and ``candidate_region`` narrow the comparison to one
    span of each file. They are separate because the usual case is asymmetric:
    the solo sits at 2:41 of the record and at 0:04 of the DI you played. In
    aligned mode they must describe the same span, since that mode's whole
    premise is that both signals are the same performance.
    """
    from ..match.render_offline import load_audio, resample
    from ..match.score import rank_candidates

    if not candidate_paths:
        raise Invalid("no candidates given")

    comparison = parse_mode(mode)
    target_path = audio_path(target_path)
    candidate_paths = {
        label: audio_path(path) for label, path in candidate_paths.items()
    }

    target_region = resolve_region(target_region, target_path)
    # "auto" means "the busiest passage of this file", which is a different
    # answer for each candidate. Resolving it once against the first one and
    # applying that to all of them would silently compare the wrong seconds
    # of every candidate after the first.
    per_candidate_auto = (
        isinstance(candidate_region, str)
        and candidate_region.strip().lower() == "auto"
    )
    if not per_candidate_auto:
        candidate_region = Region.parse(candidate_region)

    if (
        comparison.value == "aligned"
        and not per_candidate_auto
        and target_region != candidate_region
    ):
        # Trimming the two signals differently shifts one against the other,
        # which is exactly the contamination aligned mode exists to avoid.
        raise Invalid(
            "aligned mode needs the same region on both sides, but got "
            f"target {target_region.describe() if target_region else 'whole file'} "
            f"and candidates "
            f"{candidate_region.describe() if candidate_region else 'whole file'}",
            hint=(
                "Aligned mode compares sample against sample. Use one region "
                "for both, or switch to distribution mode."
            ),
        )

    # Both sides through the same encoder, or the encoder is what gets
    # measured. See rigsmith.match.source.matched_to.
    from ..match.source import matched_set

    candidate_paths, codec_notes = matched_set(candidate_paths, target_path)

    target, sample_rate = load_audio(target_path)
    if target_region:
        target = target_region.slice(target, sample_rate, label="the target")

    loaded = {}
    for label, path in candidate_paths.items():
        audio, rate = load_audio(path)
        if rate != sample_rate:
            audio = resample(audio, rate, sample_rate)
        span = suggest_region(path) if per_candidate_auto else candidate_region
        if span:
            audio = span.slice(audio, sample_rate, label=label)
        loaded[label] = audio

    scores = rank_candidates(target, loaded, sample_rate, mode=comparison)

    # What the files themselves make of this comparison - lossy encoding, a
    # sample rate below the device's, clipping. None of it stops a score; all
    # of it changes how much to believe one, so it rides along with the result
    # rather than being something the caller has to go and ask about.
    from ..match.source import concerns_for

    notes = codec_notes + concerns_for(
        [target_path, *candidate_paths.values()],
        aligned=comparison.value == "aligned",
    )
    if notes and scores:
        scores[0].notes.extend(notes)
    return scores


def source_concerns(paths, *, aligned: bool = False, di_paths=()) -> list[str]:
    """What the input files themselves make of a comparison."""
    from ..match.source import concerns_for

    return concerns_for(paths, aligned=aligned, di_paths=di_paths)


def inspect_source(path: str | Path):
    """Read an audio file's shape and levels without keeping the samples."""
    from ..match.source import inspect_audio

    return inspect_audio(path)


@dataclass
class GuitarResult:
    """One instrument, rendered through the same rig as every other."""

    take_id: str
    guitar: str
    pickup: str | None
    path: Path
    composite: float
    bands: list = field(default_factory=list)

    @property
    def label(self) -> str:
        bits = [self.guitar or self.take_id]
        if self.pickup:
            bits.append(self.pickup)
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {
            "take_id": self.take_id,
            "guitar": self.guitar,
            "pickup": self.pickup,
            "label": self.label,
            "path": str(self.path),
            "composite": self.composite,
            "bands": [
                {
                    "name": b.name,
                    "delta_db": b.delta_db,
                    "audible": b.audible,
                    "meaning": b.meaning,
                }
                for b in self.bands
            ],
        }


def compare_guitars(
    target_path,
    *,
    nam_path=None,
    ir_path=None,
    take_ids: list[str] | None = None,
    mode: str = "distribution",
    target_region=None,
    progress: Progress | None = None,
) -> list[GuitarResult]:
    """Render each of your DIs through the same rig and rank the instruments.

    The capture sweep answers "which amp gets closest". This answers the
    question underneath it: *which guitar should I be holding*. Same rig,
    same target, one variable - and it is the variable no amount of capture
    hunting can fix, because a Les Paul is not going to sound like a Strat
    however good the model is.

    Needs at least two DI takes. A wet take cannot be used: rendering one
    through an amp model stacks two amps.
    """
    from . import takes as takes_library

    wanted = [takes_library.get(t) for t in take_ids] if take_ids else [
        t for t in takes_library.list_takes(kind="di") if t.exists
    ]
    usable = [t for t in wanted if t.reamps and t.exists]

    if len(usable) < 2:
        raise Invalid(
            f"comparing guitars needs at least two DI takes, found {len(usable)}",
            hint=(
                "Play the same part on each instrument and add them:\n"
                "  rigsmith take add <file> --kind di --guitar '...'\n"
                "Only a DI works here - a wet take through an amp model "
                "stacks two amps."
            ),
        )

    target_path = audio_path(target_path)
    span = resolve_region(target_region, target_path)

    results: list[GuitarResult] = []
    total = len(usable)
    for index, take in enumerate(usable, start=1):
        if progress:
            progress.check()
            progress.update((index - 1) / total, f"rendering {take.label_or_id()}")

        # With no rig given the DIs are compared as they are. That is
        # still a real answer - it is how the instruments differ before
        # anything is done to them - and it is the only one available
        # without the NAM renderer installed.
        if nam_path or ir_path:
            audio = render_offline(
                take.file,
                nam_path=nam_path,
                ir_path=ir_path,
                label=f"guitar-{take.take_id}",
            ).path
        else:
            audio = take.file

        scored = score(
            target_path,
            {take.take_id: audio},
            mode=mode,
            target_region=span,
        )[0]
        results.append(
            GuitarResult(
                take_id=take.take_id,
                guitar=take.guitar or take.take_id,
                pickup=take.pickup,
                path=audio,
                composite=scored.composite,
                bands=compare_bands(target_path, audio, target_region=span),
            )
        )

    results.sort(key=lambda r: r.composite)
    if progress:
        progress.update(1.0, f"{len(results)} instrument(s) compared")
    return results


def render_guitar_comparison(
    results: list[GuitarResult], *, through_a_rig: bool = True
) -> str:
    if not results:
        return "Nothing to compare."

    header = (
        "Your instruments against this target, closest first:"
        if through_a_rig
        else "Your instruments against this target, dry, closest first:"
    )
    lines = [header, ""]
    for position, result in enumerate(results, start=1):
        lines.append(f"  {position}. {result.label}  composite {result.composite:.4f}")
        worst = [b for b in result.bands if b.audible]
        worst.sort(key=lambda b: abs(b.delta_db), reverse=True)
        if worst:
            summary = ", ".join(
                f"{b.name} {b.delta_db:+.1f} dB" for b in worst[:3]
            )
            lines.append(f"       {summary}")

    lines.append(
        "\nSame rig, same target, one variable. This is the one difference "
        "no amount of capture hunting fixes - a Les Paul is not going to "
        "sound like a Strat however good the model is."
        if through_a_rig
        else "\nNo rig was given, so these are the raw instruments. That is "
        "a real comparison - it is how they differ before anything is done "
        "to them - but a dry signal against a recorded one reads as dark "
        "and thin across the board. Pass a capture to hear them in context."
    )
    if all(r.composite == results[0].composite for r in results):
        lines.append(
            "\nEvery instrument scored identically, which usually means the "
            "renders came out the same - check that the NAM stage actually ran."
        )
    return "\n".join(lines)


def compare_bands(
    target_path: str | Path,
    candidate_path: str | Path,
    *,
    target_region=None,
    candidate_region=None,
):
    """Say where a candidate differs from the target, band by band.

    The natural next question after a ranking. A composite tells you which
    candidate is closest; this tells you what is still wrong with it, in the
    terms an amp's controls are labelled in.
    """
    from ..match.metrics import band_differences
    from ..match.render_offline import load_audio, resample

    from ..match.source import matched_to

    target_path = audio_path(target_path)
    candidate_path, _ = matched_to(audio_path(candidate_path), target_path)
    target_region = resolve_region(target_region, target_path)
    candidate_region = resolve_region(candidate_region, candidate_path)

    target, sample_rate = load_audio(target_path)
    if target_region:
        target = target_region.slice(target, sample_rate, label="the target")

    candidate, rate = load_audio(candidate_path)
    if rate != sample_rate:
        candidate = resample(candidate, rate, sample_rate)
    if candidate_region:
        candidate = candidate_region.slice(
            candidate, sample_rate, label=Path(candidate_path).name
        )

    return band_differences(target, candidate, sample_rate)


def render_bands(differences, *, label: str = "candidate") -> str:
    from ..match.metrics import render_band_differences

    return render_band_differences(differences, label=label)


def render_ranking(scores, *, top: int = 5) -> str:
    from ..match.score import render_ranking as _render

    return _render(scores, top=top)


# --------------------------------------------------------------------------
# the loop that ties it together
# --------------------------------------------------------------------------


@dataclass
class SearchResult:
    """A full offline candidate sweep: rendered, scored, ranked."""

    target_path: Path
    mode: str
    region: str | None = None
    rendered: list[RenderedCandidate] = field(default_factory=list)
    scores: list[Any] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    """What the input files themselves make of this sweep."""

    def render(self) -> str:
        from ..match.score import render_ranking as _render

        lines = [
            f"target: {self.target_path.name}",
            f"rendered {len(self.rendered)} candidate(s) offline",
            *([f"scored over {self.region} of the target"] if self.region else []),
            "",
            _render(self.scores),
        ]
        if self.concerns:
            lines.append("\nabout the input:")
            lines.extend(f"  {note}" for note in self.concerns)
        if self.failures:
            lines.append("\nfailed to render:")
            lines.extend(f"  {f}" for f in self.failures)
        lines.append(
            "\nThis is Layer 1 - an approximation, used to shortlist. Confirm "
            "the top candidates on the device before trusting the order."
        )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "target_path": str(self.target_path),
            "mode": self.mode,
            "region": self.region,
            "concerns": self.concerns,
            "rendered": [r.to_dict() for r in self.rendered],
            "failures": self.failures,
            "ranking": [
                {
                    "label": s.label,
                    "composite": s.composite,
                    "metrics": {m.name: m.value for m in s.metrics},
                }
                for s in self.scores
            ],
        }


def run_offline_search(
    di_path: str | Path | None,
    target_path: str | Path,
    captures: dict[str, str | Path],
    *,
    ir_path: str | Path | None = None,
    mode: str = "distribution",
    target_region=None,
    candidate_region=None,
    progress: Progress | None = None,
) -> SearchResult:
    """Render a DI through every candidate capture and rank them all.

    The end-to-end Layer 1 loop: one DI, many captures, one ranking, no
    hardware. Previously every step of this existed and nothing joined them,
    so a user had to render and score each candidate by hand.

    Args:
        di_path: A dry DI recording, a take id, or nothing at all - in which
            case the takes library's most recent DI is used.
        target_path: What to match - usually a separated guitar stem.
        captures: ``{label: path to .nam}``.
        ir_path: A cabinet IR applied to every candidate, for fairness.
        mode: ``distribution`` against a record, ``aligned`` only when target
            and candidates are the same performance.
        target_region: ``"start:end"`` in seconds, to rank against one passage
            of the target rather than the whole of it.
        candidate_region: The matching passage of each render. Since every
            render comes from the same DI, one span fits them all.
    """
    if not captures:
        raise Invalid("no captures to try")

    di_path = _di_or_default(di_path)
    target_path = audio_path(target_path)

    # A lossy DI prints its artefacts into every candidate equally, and then
    # they get measured as tone. Worth saying before minutes of rendering.
    from ..match.source import concerns_for

    described = resolve_region(target_region, target_path)
    target_region = described
    result = SearchResult(
        target_path=Path(target_path),
        mode=mode,
        region=described.describe() if described else None,
    )
    total = len(captures)

    for index, (label, nam_path) in enumerate(captures.items(), start=1):
        if progress:
            progress.check()
            progress.update((index - 1) / total, f"rendering {label} ({index}/{total})")
        try:
            rendered = render_offline(
                di_path, nam_path=nam_path, ir_path=ir_path, label=label
            )
        except (Unavailable, Invalid, OSError) as exc:
            result.failures.append(f"{label}: {exc}")
            continue

        if not rendered.applied("NAM("):
            # The capture did not load. Keeping this candidate would enter the
            # bare DI into the ranking under the capture's name.
            reason = next(
                (w for w in rendered.warnings if w.startswith("NAM")),
                "the capture did not load",
            )
            result.failures.append(f"{label}: {reason}")
            continue

        result.rendered.append(rendered)

    if not result.rendered:
        raise Unavailable(
            "no candidate rendered successfully",
            hint="\n".join(result.failures) or "Check the capture paths.",
        )

    if progress:
        progress.update(0.9, f"scoring {len(result.rendered)} candidates")
    result.scores = score(
        target_path,
        {r.label: r.path for r in result.rendered},
        mode=mode,
        target_region=target_region,
        candidate_region=candidate_region,
    )
    result.concerns = concerns_for(
        [di_path, target_path], aligned=mode == "aligned", di_paths=[di_path]
    )
    if progress:
        progress.update(1.0, "done")
    return result


# --------------------------------------------------------------------------
# blind A/B
# --------------------------------------------------------------------------


def _ab_path(session_id: str) -> Path:
    return _audio_dir() / f"ab-{session_id}.json"


def create_ab(
    session_id: str,
    target_label: str,
    candidates: dict[str, str],
    *,
    pairs: int | None = None,
):
    """Start a blind listening test. Metrics rank; ears decide.

    The clips are copied to opaque filenames first. Blinding that only hides
    the label while the audio is still served from ``jcm800-hot-offline.wav``
    is not blinding: the path shows up in a terminal, in a URL bar, and in the
    page's own DOM. A copy per clip is cheap next to a listening test whose
    result you intend to trust.
    """
    from ..match.ab import create_session

    require_writable("create an A/B session")
    if len(candidates) < 2:
        raise Invalid("a blind test needs at least two candidates")

    session = create_session(session_id, target_label, candidates, pairs=pairs)
    session.audio_paths = _blind_the_audio(session_id, session)
    session.save(_ab_path(session_id))
    return session


def _blind_the_audio(session_id: str, session) -> dict[str, str]:
    """Copy each clip to a name that gives nothing away.

    A clip that cannot be copied keeps its original path rather than
    disappearing from the test - a missing side is worse than a named one,
    and :func:`ab_leaks` reports what is still identifying.
    """
    import shutil

    directory = _audio_dir() / f"ab-{session_id}"
    directory.mkdir(parents=True, exist_ok=True)

    blinded: dict[str, str] = {}
    for blind_id, source in session.audio_paths.items():
        origin = Path(source)
        destination = directory / f"{blind_id}{origin.suffix or '.wav'}"
        try:
            shutil.copyfile(origin, destination)
        except OSError:
            blinded[blind_id] = str(origin)
            continue
        blinded[blind_id] = str(destination)
    return blinded


def ab_leaks(session) -> list[str]:
    """Blinded ids whose audio path still contains the candidate's name."""
    leaking = []
    for blind_id, real_label in session.blinded.items():
        path = str(session.audio_paths.get(blind_id, "")).lower()
        if real_label.lower() in path:
            leaking.append(blind_id)
    return leaking


def load_ab(session_id: str):
    from ..match.ab import ABSession

    path = _ab_path(session_id)
    if not path.exists():
        known = ", ".join(list_ab_sessions()) or "none"
        raise NotFound(f"no A/B session '{session_id}'", hint=f"Sessions: {known}")
    return ABSession.load(path)


def list_ab_sessions() -> list[str]:
    """Session ids in the workspace, so one started last week is findable."""
    directory = _audio_dir()
    if not directory.is_dir():
        return []
    return sorted(
        p.stem[len("ab-") :] for p in directory.glob("ab-*.json") if p.is_file()
    )


def answer_ab(
    session_id: str,
    trial_id: int,
    verdict: str,
    *,
    confidence: int | None = None,
    comment: str | None = None,
):
    """Record a verdict and return the session."""
    require_writable("record an A/B verdict")
    session = load_ab(session_id)
    try:
        session.answer(trial_id, verdict, confidence=confidence, comment=comment)
    except (ValueError, KeyError) as exc:
        raise Invalid(str(exc)) from exc
    session.save(_ab_path(session_id))
    return session

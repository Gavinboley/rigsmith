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

    Round-trip latency is measured and compensated. Load the rig you want to
    test on the device first - or use :func:`rigsmith.core.devices.select_rig`.
    """
    from ..match.render_device import reamp as _reamp
    from ..match.render_offline import load_audio, save_audio

    require_writable("record audio to the workspace")
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
                "'distribution' against anything separated from a record."
            ),
        ) from None


def score(
    target_path: str | Path,
    candidate_paths: dict[str, str | Path],
    *,
    mode: str = "distribution",
):
    """Score rendered candidates against a target and rank them.

    In distribution mode the sample-wise metrics are not computed at all:
    against a different performance they are artefacts of an arbitrary
    alignment. See docs/TONE_MATCHING.md.
    """
    from ..match.render_offline import load_audio, resample
    from ..match.score import rank_candidates

    if not candidate_paths:
        raise Invalid("no candidates given")

    comparison = parse_mode(mode)
    target, sample_rate = load_audio(target_path)

    loaded = {}
    for label, path in candidate_paths.items():
        audio, rate = load_audio(path)
        if rate != sample_rate:
            audio = resample(audio, rate, sample_rate)
        loaded[label] = audio

    return rank_candidates(target, loaded, sample_rate, mode=comparison)


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
    rendered: list[RenderedCandidate] = field(default_factory=list)
    scores: list[Any] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def render(self) -> str:
        from ..match.score import render_ranking as _render

        lines = [
            f"target: {self.target_path.name}",
            f"rendered {len(self.rendered)} candidate(s) offline",
            "",
            _render(self.scores),
        ]
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
    di_path: str | Path,
    target_path: str | Path,
    captures: dict[str, str | Path],
    *,
    ir_path: str | Path | None = None,
    mode: str = "distribution",
    progress: Progress | None = None,
) -> SearchResult:
    """Render a DI through every candidate capture and rank them all.

    The end-to-end Layer 1 loop: one DI, many captures, one ranking, no
    hardware. Previously every step of this existed and nothing joined them,
    so a user had to render and score each candidate by hand.

    Args:
        di_path: A dry DI recording.
        target_path: What to match - usually a separated guitar stem.
        captures: ``{label: path to .nam}``.
        ir_path: A cabinet IR applied to every candidate, for fairness.
        mode: ``distribution`` against a record, ``aligned`` only when target
            and candidates are the same performance.
    """
    if not captures:
        raise Invalid("no captures to try")

    result = SearchResult(target_path=Path(target_path), mode=mode)
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
    """Start a blind listening test. Metrics rank; ears decide."""
    from ..match.ab import create_session

    require_writable("create an A/B session")
    if len(candidates) < 2:
        raise Invalid("a blind test needs at least two candidates")
    session = create_session(session_id, target_label, candidates, pairs=pairs)
    session.save(_ab_path(session_id))
    return session


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

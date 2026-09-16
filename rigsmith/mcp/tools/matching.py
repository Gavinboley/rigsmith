"""Tone-matching tools: separate, render, score, and listen blind."""

from __future__ import annotations

from typing import Literal

from ...core import matching
from ...core.jobs import get_manager
from .._shared import tool

Mode = Literal["aligned", "distribution"]


@tool
def list_audio_devices() -> str:
    """List audio interfaces, flagging the HeadRush if it is connected.

    Hardware tone testing needs the Core connected over USB-B and exposing
    all six input channels - channel 5 is the dry DI, which is what makes
    reamping one performance through many candidates possible.
    """
    return matching.render_audio_devices()


@tool
def isolate_guitar(audio_path: str, output_dir: str | None = None) -> str:
    """Separate a guitar stem from a mixed recording, using Demucs htdemucs_6s.

    Runs in the background because it takes minutes on CPU - this returns a
    job id immediately. Poll it with job_status.

    The result is a timbral reference, not a clean target: it still carries
    the record's amp, cab, mic, and mix processing, plus separation
    artefacts. Score against it in distribution mode only.
    """
    job = get_manager().submit(
        f"isolate {audio_path}",
        lambda progress: matching.isolate_guitar(
            audio_path, out_dir=output_dir, progress=progress
        ),
    )
    return (
        f"Started job {job.id} - separating {audio_path}.\n"
        "Demucs takes minutes on CPU. Check progress with "
        f"job_status('{job.id}')."
    )


@tool
def capture_di(duration_s: float = 15.0, name: str = "take") -> str:
    """Record one performance, capturing the wet tone and the dry DI together.

    Play the part once. The dry DI on channel 5 can then be reamped through
    any number of candidate rigs, each compared against this same wet
    reference - so trying more candidates costs compute, not playing time.

    Needs the Core connected over USB-B.
    """
    capture, paths = matching.capture_di(duration_s, name=name)
    lines = [capture.describe()]
    lines.extend(f"{kind}: {path}" for kind, path in paths.items())
    if "dry" not in paths:
        lines.append(
            "No dry DI was captured, so this take cannot be reamped through "
            "other candidates. Check the Core's USB audio mode exposes "
            "channel 5."
        )
    return "\n".join(lines)


@tool
def reamp_di(di_path: str, name: str = "candidate") -> str:
    """Play a DI through the Core's currently loaded rig and record the result.

    Round-trip latency is measured and compensated, because a few
    milliseconds of offset is enough to make an aligned comparison report a
    perfect match as a catastrophic one.

    Load the rig you want to test first - midi_select_rig can do that.
    """
    rendered = matching.reamp(di_path, label=name)
    return rendered.render()


@tool
def render_offline(
    di_path: str,
    nam_path: str | None = None,
    ir_path: str | None = None,
    ir_truncate: int | None = None,
    name: str = "candidate",
) -> str:
    """Render a DI through a NAM capture and cab IR in Python, no hardware.

    Fast and approximate - use it to narrow a large field of captures before
    spending real-time reamps on the shortlist. Cabinet IR convolution is
    faithful; NAM inference needs the `neural-amp-modeler` package.

    Args:
        ir_truncate: Shorten the IR to this many samples. Pass 1024 to mirror
            the Core's truncated-IR mode, which costs less DSP.
    """
    rendered = matching.render_offline(
        di_path,
        nam_path=nam_path,
        ir_path=ir_path,
        ir_truncate=ir_truncate,
        label=name,
    )
    return rendered.render()


@tool
def search_offline(
    di_path: str,
    target_path: str,
    captures: dict[str, str],
    ir_path: str | None = None,
    mode: Mode = "distribution",
) -> str:
    """Render one DI through many captures and rank them all, no hardware.

    This is the whole Layer 1 loop in one call: render each candidate, score
    every result against the target, return a ranking. Use it to get from
    "twenty plausible captures" to "these three are worth reamping".

    Runs in the background - returns a job id to poll with job_status.

    Args:
        di_path: A dry DI recording.
        target_path: What to match, usually a separated guitar stem.
        captures: {label: path to .nam}.
        ir_path: A cabinet IR applied to every candidate, for fairness.
        mode: 'distribution' against a record. Only use 'aligned' when target
            and candidates are genuinely the same performance.
    """
    job = get_manager().submit(
        f"offline search over {len(captures)} captures",
        lambda progress: matching.run_offline_search(
            di_path,
            target_path,
            captures,
            ir_path=ir_path,
            mode=mode,
            progress=progress,
        ),
    )
    return (
        f"Started job {job.id} - rendering {len(captures)} candidates and "
        f"scoring them against {target_path}.\n"
        f"Check progress with job_status('{job.id}')."
    )


@tool
def score_candidates(
    target_path: str,
    candidate_paths: dict[str, str],
    mode: Mode = "distribution",
) -> str:
    """Score rendered candidates against a target and rank them.

    Args:
        target_path: The reference audio.
        candidate_paths: {label: path} of rendered candidates.
        mode: Use 'aligned' ONLY when the target and candidates are the same
            performance - your own DI reamped through each rig, against your
            own reference. Use 'distribution' against anything separated from
            a record, which is a different performance.

    In distribution mode the sample-wise metrics (ESR, STFT loss) are not
    computed at all. Against a different performance they are unstable to
    arbitrary timing offsets and report nonsense.
    """
    scores = matching.score(target_path, candidate_paths, mode=mode)
    detail = "\n\n".join(s.render() for s in scores[:3])
    return f"{matching.render_ranking(scores)}\n\nTop candidates in detail:\n\n{detail}"

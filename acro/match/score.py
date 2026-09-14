"""Score a rendered tone against a target, honestly.

The whole module exists to prevent one specific mistake.

Take a signal and shift it by 500 samples - ten milliseconds, inaudible as a
timing difference. ESR between the original and the shifted copy comes out
around 2.6, which by the usual reading ("0.04 good, 0.1 broken") says the two
are wildly different. They are the same recording. Meanwhile the MFCC
distribution distance is 0.002: identical, correctly.

That is what happens if you point a sample-wise metric at a commercial
recording. The record is a different performance, on a different guitar, by a
different player, inside a mix. Sample-wise error is meaningless there, and
it will confidently tell you your best candidate is your worst.

So the comparison mode is a required argument, not an inferred one, and the
aligned-only metrics are simply not computed in distribution mode.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .metrics import (
    ComparisonMode,
    MetricResult,
    esr,
    log_spectral_distance,
    mfcc_distance,
    multi_resolution_stft_loss,
    normalise_rms,
    spectral_summary,
    to_mono,
)

#: ESR thresholds the NAM community uses to judge a capture.
ESR_GOOD = 0.04
ESR_BROKEN = 0.10


@dataclass
class CandidateScore:
    """How close one candidate got, and how much to trust the number."""

    label: str
    mode: ComparisonMode
    metrics: list[MetricResult] = field(default_factory=list)
    composite: float = 0.0
    """Lower is closer. Comparable only between candidates scored the same
    way against the same target - it is a ranking, not an absolute."""
    notes: list[str] = field(default_factory=list)

    def metric(self, name: str) -> MetricResult | None:
        return next((m for m in self.metrics if m.name == name), None)

    def render(self) -> str:
        lines = [f"{self.label}  [{self.mode.value}]  composite {self.composite:.4f}"]
        lines.extend("  " + m.render() for m in self.metrics)
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)


def _interpret_esr(value: float) -> str:
    if value <= ESR_GOOD:
        return "good - at or below the 0.04 the NAM community treats as a solid match"
    if value <= ESR_BROKEN:
        return "usable, but above the 0.04 target"
    return "poor - above 0.1, which usually means something is wrong, not merely different"


def score_candidate(
    target: np.ndarray,
    candidate: np.ndarray,
    sample_rate: int,
    *,
    mode: ComparisonMode,
    label: str = "candidate",
) -> CandidateScore:
    """Compare a candidate render to a target.

    Args:
        target: Reference audio. In aligned mode this is your own DI through
            the reference rig; in distribution mode it is typically a guitar
            stem separated from a record.
        candidate: The rendered candidate.
        sample_rate: Shared sample rate, in Hz.
        mode: ``ALIGNED`` only if both signals are the same performance,
            time-aligned. Otherwise ``DISTRIBUTION``.
        label: Name for reporting.

    Raises:
        ValueError: if aligned mode is requested for signals of wildly
            different length, which cannot be the same performance.
    """
    target = normalise_rms(to_mono(target))
    candidate = normalise_rms(to_mono(candidate))
    score = CandidateScore(label=label, mode=mode)

    if mode is ComparisonMode.ALIGNED:
        shorter, longer = sorted((len(target), len(candidate)))
        if longer > shorter * 1.5:
            raise ValueError(
                f"aligned mode requires the same performance, but the signals "
                f"differ in length by more than 50% ({shorter} vs {longer} "
                f"samples). Use ComparisonMode.DISTRIBUTION, or align them first."
            )

        esr_value = esr(target, candidate)
        score.metrics.append(
            MetricResult(
                name="esr",
                value=esr_value,
                mode=ComparisonMode.ALIGNED,
                interpretation=_interpret_esr(esr_value),
            )
        )
        score.metrics.append(
            MetricResult(
                name="multi_res_stft",
                value=multi_resolution_stft_loss(target, candidate),
                mode=ComparisonMode.ALIGNED,
            )
        )

    # Distribution metrics are valid in both modes and are the only thing
    # reported in distribution mode.
    summary_distance = spectral_summary(target, sample_rate).distance(
        spectral_summary(candidate, sample_rate)
    )
    score.metrics.append(
        MetricResult(
            name="spectral_profile",
            value=summary_distance,
            mode=ComparisonMode.DISTRIBUTION,
        )
    )
    score.metrics.append(
        MetricResult(
            name="mfcc_distribution",
            value=mfcc_distance(target, candidate, sample_rate),
            mode=ComparisonMode.DISTRIBUTION,
        )
    )
    score.metrics.append(
        MetricResult(
            name="log_spectral_db",
            value=log_spectral_distance(target, candidate, sample_rate),
            mode=ComparisonMode.DISTRIBUTION,
        )
    )

    if mode is ComparisonMode.ALIGNED:
        # ESR is the metric this domain trusts, so it dominates.
        score.composite = (
            0.5 * min(score.metric("esr").value, 2.0)
            + 0.2 * min(score.metric("multi_res_stft").value, 5.0) / 5.0
            + 0.2 * summary_distance
            + 0.1 * score.metric("mfcc_distribution").value
        )
        score.notes.append(
            "Aligned comparison: ESR and multi-resolution STFT are meaningful "
            "because both signals are the same performance."
        )
    else:
        score.composite = (
            0.45 * summary_distance
            + 0.35 * score.metric("mfcc_distribution").value
            + 0.20 * min(score.metric("log_spectral_db").value, 40.0) / 40.0
        )
        score.notes.append(
            "Distribution comparison: this is a RANKING among candidates, not "
            "an absolute measure of closeness. Sample-wise metrics (ESR, STFT "
            "loss) were deliberately not computed - against a different "
            "performance they report nonsense."
        )
        score.notes.append(
            "A separated guitar stem still carries the original amp, cab, mic, "
            "and mix processing, so it is a timbral reference, not a clean target."
        )

    return score


def rank_candidates(
    target: np.ndarray,
    candidates: dict[str, np.ndarray],
    sample_rate: int,
    *,
    mode: ComparisonMode,
) -> list[CandidateScore]:
    """Score several candidates and return them best first."""
    scores = [
        score_candidate(target, audio, sample_rate, mode=mode, label=label)
        for label, audio in candidates.items()
    ]
    scores.sort(key=lambda s: s.composite)
    return scores


def render_ranking(scores: list[CandidateScore], *, top: int = 5) -> str:
    """A readable leaderboard, with the honest caveat attached."""
    if not scores:
        return "No candidates scored."

    mode = scores[0].mode
    lines = [f"Ranking ({mode.value} comparison), closest first:", ""]
    for position, score in enumerate(scores[:top], start=1):
        headline = f"  {position}. {score.label}  composite {score.composite:.4f}"
        detail = "  ".join(
            f"{m.name}={m.value:.4f}"
            for m in score.metrics
            if m.name in ("esr", "spectral_profile", "mfcc_distribution")
        )
        lines.append(f"{headline}   {detail}")

    if mode is ComparisonMode.DISTRIBUTION:
        lines.append(
            "\nThese are relative rankings against a different performance. "
            "The ordering is informative; the absolute numbers are not. "
            "Confirm the top candidates by ear before committing."
        )
    else:
        best = scores[0].metric("esr")
        if best and best.value > ESR_BROKEN:
            lines.append(
                f"\nEven the best candidate has ESR {best.value:.3f}, above the "
                "0.1 that usually indicates a real problem rather than a near "
                "miss. Check gain staging and alignment before trusting the order."
            )
    return "\n".join(lines)

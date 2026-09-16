"""The composite has to mean what it says it means.

Declared weights are only real if every term is on the same scale. They were
not: ESR is interesting between 0 and 0.1, the spectral profile distance runs
past 20 on pathological input, and the nominal "0.5 ESR" blend measured, on a
realistic aligned pair, as 1% ESR and 64% spectral. These tests pin the shape
of the blend rather than any particular number, so the weights cannot quietly
become fiction again.
"""

from __future__ import annotations

import numpy as np
import pytest

from rigsmith.match.metrics import ComparisonMode
from rigsmith.match.score import (
    ESR_SCALE,
    MFCC_SCALE,
    SPECTRAL_SCALE,
    STFT_SCALE,
    _scaled,
    rank_candidates,
    score_candidate,
)

SR = 48000


@pytest.fixture(scope="module")
def signals():
    t = np.arange(2 * SR) / SR
    rng = np.random.default_rng(1)
    clean = (
        np.sin(2 * np.pi * 196 * t)
        + 0.4 * np.sin(2 * np.pi * 392 * t)
        + 0.02 * rng.standard_normal(2 * SR)
    )
    return {
        "target": np.tanh(clean * 3.0),
        "near": np.tanh(clean * 3.1),
        "far": np.tanh(clean * 12.0),
        "noise": rng.standard_normal(2 * SR),
        "silence": np.zeros(2 * SR),
    }


# --------------------------------------------------------------------------
# the scale helper
# --------------------------------------------------------------------------


@pytest.mark.parametrize("scale", [ESR_SCALE, STFT_SCALE, SPECTRAL_SCALE, MFCC_SCALE])
def test_scaling_lands_in_the_unit_range(scale):
    assert _scaled(0.0, scale) == 0.0
    assert _scaled(scale, scale) == 1.0
    assert _scaled(scale * 1000, scale) == 1.0
    assert _scaled(-5.0, scale) == 0.0, "a negative metric is a bug, not a bonus"


def test_the_esr_scale_covers_the_range_the_domain_argues_about():
    """0.04 is a good capture and 0.1 is a broken one, so both have to sit
    inside the term rather than in the first 2% of it."""
    from rigsmith.match.score import ESR_BROKEN, ESR_GOOD

    assert 0.1 < _scaled(ESR_GOOD, ESR_SCALE) < 0.3
    assert 0.3 < _scaled(ESR_BROKEN, ESR_SCALE) < 0.6


# --------------------------------------------------------------------------
# the composite itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", list(ComparisonMode))
@pytest.mark.parametrize("candidate", ["target", "near", "far", "noise", "silence"])
def test_the_composite_stays_inside_zero_and_one(signals, mode, candidate):
    """Including for input nothing sane would produce - a failed render, a
    silent file, an IR loaded as audio."""
    score = score_candidate(
        signals["target"], signals[candidate], SR, mode=mode, label=candidate
    )
    assert 0.0 <= score.composite <= 1.0, score.render()


def test_a_pathological_candidate_cannot_swamp_the_blend(signals):
    """Unclamped, one term reached 20+ and the other weights stopped mattering."""
    noise = score_candidate(
        signals["target"], signals["noise"], SR, mode=ComparisonMode.DISTRIBUTION
    )
    far = score_candidate(
        signals["target"], signals["far"], SR, mode=ComparisonMode.DISTRIBUTION
    )
    assert far.composite < noise.composite
    assert noise.composite <= 1.0


def test_a_signal_against_itself_scores_zero(signals):
    for mode in ComparisonMode:
        score = score_candidate(
            signals["target"], signals["target"], SR, mode=mode
        )
        assert score.composite == pytest.approx(0.0, abs=1e-6), score.render()


def test_closer_is_lower(signals):
    ranked = rank_candidates(
        signals["target"],
        {"far": signals["far"], "near": signals["near"], "noise": signals["noise"]},
        SR,
        mode=ComparisonMode.DISTRIBUTION,
    )
    assert [s.label for s in ranked] == ["near", "far", "noise"]


# --------------------------------------------------------------------------
# what the weights actually buy
# --------------------------------------------------------------------------


def test_esr_carries_real_weight_in_aligned_mode(signals):
    """It used to contribute about one percent of the number it was said to
    dominate, which made aligned mode a distribution score with extra steps."""
    score = score_candidate(
        signals["target"], signals["far"], SR, mode=ComparisonMode.ALIGNED
    )
    esr_value = score.metric("esr").value
    assert esr_value > 0.04, "this fixture is meant to be a poor match"

    contribution = 0.5 * _scaled(esr_value, ESR_SCALE)
    assert contribution / score.composite > 0.3, (
        "ESR is the metric this domain trusts; if it is a rounding error in "
        "the composite then the composite is not measuring what it claims"
    )


def test_no_single_term_can_exceed_its_weight(signals):
    """The property that makes the declared weights true."""
    for mode in ComparisonMode:
        score = score_candidate(
            signals["target"], signals["noise"], SR, mode=mode, label="noise"
        )
        weights = (
            {"esr": 0.5, "multi_res_stft": 0.2, "spectral_profile": 0.2,
             "mfcc_distribution": 0.1}
            if mode is ComparisonMode.ALIGNED
            else {"spectral_profile": 0.45, "mfcc_distribution": 0.35,
                  "log_spectral_db": 0.20}
        )
        assert sum(weights.values()) == pytest.approx(1.0)

        for name, weight in weights.items():
            metric = score.metric(name)
            assert metric is not None, f"{name} missing in {mode.value} mode"
            scale = {
                "esr": ESR_SCALE,
                "multi_res_stft": STFT_SCALE,
                "spectral_profile": SPECTRAL_SCALE,
                "mfcc_distribution": MFCC_SCALE,
                "log_spectral_db": 40.0,
            }[name]
            assert weight * _scaled(metric.value, scale) <= weight + 1e-9


def test_distribution_mode_still_refuses_to_compute_sample_wise_metrics(signals):
    score = score_candidate(
        signals["target"], signals["near"], SR, mode=ComparisonMode.DISTRIBUTION
    )
    assert score.metric("esr") is None
    assert score.metric("multi_res_stft") is None

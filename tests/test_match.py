"""Tests for the tone-matching layer.

The central property under test is that sample-wise metrics are never applied
to signals that are not the same performance. Everything else is downstream
of getting that right.
"""

from __future__ import annotations

import numpy as np
import pytest

from rigsmith.match.ab import ABSession, create_session
from rigsmith.match.metrics import (
    ComparisonMode,
    esr,
    log_spectral_distance,
    mfcc_distance,
    multi_resolution_stft_loss,
    normalise_rms,
    spectral_summary,
    to_mono,
)
from rigsmith.match.score import rank_candidates, render_ranking, score_candidate

SR = 48000


@pytest.fixture(scope="module")
def signals():
    t = np.arange(SR) / SR
    rng = np.random.default_rng(0)
    clean = (
        np.sin(2 * np.pi * 196 * t)
        + 0.4 * np.sin(2 * np.pi * 392 * t)
        + 0.03 * rng.standard_normal(SR)
    )
    return {
        "clean": clean,
        "target": np.tanh(clean * 3.0),
        "near": np.tanh(clean * 3.05),
        "hotter": np.tanh(clean * 12.0),
        "noise": rng.standard_normal(SR) * 0.3,
    }


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------


def test_esr_is_zero_for_identical_signals(signals):
    assert esr(signals["target"], signals["target"]) == pytest.approx(0.0, abs=1e-9)


def test_esr_orders_by_similarity(signals):
    target = signals["target"]
    assert esr(target, signals["near"]) < esr(target, signals["hotter"])
    assert esr(target, signals["hotter"]) < esr(target, signals["noise"])


def test_esr_ignores_level_differences(signals):
    """Loudness is not tone - a quieter copy must not score as different."""
    target = signals["target"]
    quiet = target * 0.25
    assert esr(target, quiet) < 0.01


def test_multi_res_stft_orders_by_similarity(signals):
    target = signals["target"]
    assert multi_resolution_stft_loss(target, signals["near"]) < (
        multi_resolution_stft_loss(target, signals["hotter"])
    )


def test_more_gain_raises_spectral_centroid(signals):
    """Sanity check that the descriptors track something real."""
    clean = spectral_summary(signals["target"], SR)
    hot = spectral_summary(signals["hotter"], SR)
    assert hot.centroid_hz > clean.centroid_hz
    assert hot.flatness > clean.flatness


def test_spectral_summary_distance_is_zero_for_self(signals):
    summary = spectral_summary(signals["target"], SR)
    assert summary.distance(summary) == pytest.approx(0.0)


def test_mfcc_distance_orders_by_similarity(signals):
    target = signals["target"]
    assert mfcc_distance(target, signals["near"], SR) < (
        mfcc_distance(target, signals["hotter"], SR)
    )


def test_log_spectral_distance_in_db(signals):
    target = signals["target"]
    assert log_spectral_distance(target, target, SR) == pytest.approx(0.0, abs=1e-6)
    assert log_spectral_distance(target, signals["hotter"], SR) > 3.0


def test_to_mono_handles_both_channel_layouts():
    samples_first = np.zeros((1000, 2))
    channels_first = np.zeros((2, 1000))
    assert to_mono(samples_first).shape == (1000,)
    assert to_mono(channels_first).shape == (1000,)


def test_normalise_rms_survives_silence():
    assert np.all(normalise_rms(np.zeros(100)) == 0)


# --------------------------------------------------------------------------
# the property that matters
# --------------------------------------------------------------------------


def test_esr_is_wildly_unstable_to_timing_while_timbre_metrics_are_not(signals):
    """Shift the *same* audio and ESR swings from 0.005 to 3.8; MFCC barely moves.

    The fundamental is 196 Hz, a ~245-sample period at 48 kHz. A shift of 245
    samples realigns the waveform and ESR reports a near-perfect match; a
    shift of 122 - half a period, 2.5 ms, inaudible as timing - reports a
    catastrophic one. Same audio, same timbre, both times.

    That instability is why a commercial recording cannot be scored with
    sample-wise metrics: it is a different performance, so the offset is
    arbitrary and the resulting number is arbitrary with it. Hence the
    comparison mode is an explicit argument rather than something inferred.
    """
    target = signals["target"]
    shifts = [61, 122, 245, 500, 1000]

    esr_values = [esr(target, np.roll(target, s)) for s in shifts]
    mfcc_values = [mfcc_distance(target, np.roll(target, s), SR) for s in shifts]

    assert max(esr_values) > 100 * min(esr_values), (
        "ESR should swing by orders of magnitude across arbitrary shifts"
    )
    assert max(esr_values) > 1.0, "a half-period shift should look catastrophic"
    assert max(mfcc_values) < 0.01, "timbre is unchanged by any of these shifts"

    summaries = [
        spectral_summary(target, SR).distance(spectral_summary(np.roll(target, s), SR))
        for s in shifts
    ]
    assert max(summaries) < 0.01


def test_distribution_mode_never_reports_sample_wise_metrics(signals):
    score = score_candidate(
        signals["target"], np.roll(signals["target"], 500), SR,
        mode=ComparisonMode.DISTRIBUTION,
    )
    assert score.metric("esr") is None
    assert score.metric("multi_res_stft") is None
    assert score.metric("mfcc_distribution") is not None


def test_distribution_mode_scores_a_shifted_copy_as_close(signals):
    score = score_candidate(
        signals["target"], np.roll(signals["target"], 500), SR,
        mode=ComparisonMode.DISTRIBUTION,
    )
    assert score.composite < 0.02


def test_aligned_mode_reports_sample_wise_metrics(signals):
    score = score_candidate(
        signals["target"], signals["near"], SR, mode=ComparisonMode.ALIGNED
    )
    assert score.metric("esr") is not None
    assert score.metric("multi_res_stft") is not None


def test_aligned_mode_refuses_mismatched_lengths(signals):
    """Signals of very different length cannot be the same performance."""
    with pytest.raises(ValueError, match="same performance"):
        score_candidate(
            signals["target"], signals["target"][:5000], SR,
            mode=ComparisonMode.ALIGNED,
        )


def test_esr_interpretation_uses_community_thresholds(signals):
    score = score_candidate(
        signals["target"], signals["near"], SR, mode=ComparisonMode.ALIGNED
    )
    assert "0.04" in (score.metric("esr").interpretation or "")


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [ComparisonMode.ALIGNED, ComparisonMode.DISTRIBUTION])
def test_ranking_puts_the_closest_candidate_first(signals, mode):
    scores = rank_candidates(
        signals["target"],
        {
            "hotter": signals["hotter"],
            "near": signals["near"],
            "noise": signals["noise"],
        },
        SR,
        mode=mode,
    )
    assert scores[0].label == "near"
    assert scores[-1].label == "noise"


def test_distribution_ranking_carries_its_caveat(signals):
    scores = rank_candidates(
        signals["target"],
        {"near": signals["near"], "hotter": signals["hotter"]},
        SR,
        mode=ComparisonMode.DISTRIBUTION,
    )
    rendered = render_ranking(scores)
    assert "relative rankings" in rendered
    assert "by ear" in rendered


def test_empty_ranking_is_handled():
    assert "No candidates" in render_ranking([])


# --------------------------------------------------------------------------
# blind A/B
# --------------------------------------------------------------------------


def test_session_blinds_candidate_identities():
    session = create_session(
        "s", "target", {"winner": "/a.wav", "loser": "/b.wav"}, seed=1
    )
    for trial in session.trials:
        assert trial.left not in ("winner", "loser")
        assert trial.right not in ("winner", "loser")
    assert set(session.blinded.values()) == {"winner", "loser"}


def test_session_generates_every_pairing_by_default():
    session = create_session(
        "s", "t", {f"c{i}": f"/{i}.wav" for i in range(4)}, seed=1
    )
    assert len(session.trials) == 6  # 4 choose 2


def test_session_pair_cap_is_respected():
    session = create_session(
        "s", "t", {f"c{i}": f"/{i}.wav" for i in range(5)}, seed=1, pairs=3
    )
    assert len(session.trials) == 3


def test_session_needs_two_candidates():
    with pytest.raises(ValueError, match="at least two"):
        create_session("s", "t", {"only": "/a.wav"})


def test_standings_rank_the_consistent_winner():
    candidates = {"winner": "/w.wav", "mid": "/m.wav", "loser": "/l.wav"}
    session = create_session("s", "t", candidates, seed=3)
    winner_id = next(k for k, v in session.blinded.items() if v == "winner")

    for trial in session.trials:
        if winner_id == trial.left:
            session.answer(trial.trial_id, "left")
        elif winner_id == trial.right:
            session.answer(trial.trial_id, "right")
        else:
            session.answer(trial.trial_id, "tie")

    assert session.complete
    assert session.standings()[0][0] == "winner"


def test_rejects_an_invalid_verdict():
    session = create_session("s", "t", {"a": "/a.wav", "b": "/b.wav"}, seed=1)
    with pytest.raises(ValueError, match="left"):
        session.answer(1, "maybe")


def test_rejects_out_of_range_confidence():
    session = create_session("s", "t", {"a": "/a.wav", "b": "/b.wav"}, seed=1)
    with pytest.raises(ValueError, match="confidence"):
        session.answer(1, "left", confidence=9)


def test_session_round_trips_through_json(tmp_path):
    session = create_session("s", "t", {"a": "/a.wav", "b": "/b.wav"}, seed=1)
    session.answer(1, "left", confidence=4, comment="tighter low end")

    path = session.save(tmp_path / "ab.json")
    restored = ABSession.load(path)

    assert restored.blinded == session.blinded
    assert restored.trials[0].verdict == "left"
    assert restored.trials[0].comment == "tighter low end"


def test_next_trial_does_not_leak_identities():
    session = create_session(
        "s", "target", {"secret-winner": "/w.wav", "secret-loser": "/l.wav"}, seed=1
    )
    rendered = session.render_next()
    assert "secret-winner" not in rendered
    assert "secret-loser" not in rendered

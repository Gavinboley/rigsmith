"""Putting a tab's notes on the record's clock.

A MIDI export of a tab has the right notes and the wrong timing: a fixed
tempo where the band drifted, no count-in where the record has one, a repeat
written once that the record plays twice. Rendered as-is, the DI would go out
of step with the stem within a bar, and every comparison after that would be
comparing different notes.

So the notes are warped onto the record with dynamic time warping over
chroma - the twelve pitch classes, frame by frame, which is what a tab and a
distorted stem still have in common when nothing else is. The tab's chroma
comes straight from its notes (nothing is rendered); the stem's from its
spectrum. The warping path then maps every tab time to a record time, and the
notes move with it.

The DTW is vectorised along anti-diagonals - every cell on one depends only
on the two before it - so a four-minute song at 20 frames a second aligns in
about a second without a compiled extension.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .synth import NoteEvent

#: Frequency span used for chroma. Below the low B there is only rumble;
#: above ~2 kHz a distorted guitar's spectrum is mostly not its notes.
CHROMA_MIN_HZ = 55.0
CHROMA_MAX_HZ = 2000.0


def audio_chroma(audio: np.ndarray, sample_rate: int, *, hop_s: float = 0.05) -> np.ndarray:
    """Frames x 12, each frame's energy folded into pitch classes."""
    audio = np.asarray(audio, dtype=np.float64)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    # 170 ms at 48 kHz: the low E's pitch classes need the resolution. It
    # smears onsets forward by half a window, which the onset snapping in
    # align_events takes back out.
    n_fft = 8192 if sample_rate >= 32000 else 4096
    hop = max(1, int(hop_s * sample_rate))
    padded = np.pad(audio, (n_fft // 2, n_fft // 2))
    count = max(0, 1 + (len(padded) - n_fft) // hop)
    if count == 0:
        return np.zeros((0, 12))
    freqs = np.fft.rfftfreq(n_fft, 1 / sample_rate)
    mask = (freqs >= CHROMA_MIN_HZ) & (freqs <= CHROMA_MAX_HZ)
    pitch_class = (np.round(12 * np.log2(freqs[mask] / 440.0)) + 9) % 12
    pitch_class = pitch_class.astype(int)
    window = np.hanning(n_fft)
    chroma = np.zeros((count, 12))
    for i in range(count):
        frame = padded[i * hop : i * hop + n_fft] * window
        spectrum = np.abs(np.fft.rfft(frame))[mask]
        # Square-root compression: a loud root should not drown the third.
        np.add.at(chroma[i], pitch_class, np.sqrt(spectrum))
    return chroma


#: A plucked note's first eight harmonics, as (semitones above the
#: fundamental, relative weight). A stem's chroma contains every partial - the
#: third harmonic lands on the fifth, the fifth on the major third - so a note
#: has to be folded in the same way or a perfect transcription scores half.
HARMONIC_TEMPLATE = (
    (0, 1.0), (12, 0.5), (19, 0.33), (24, 0.25),
    (28, 0.2), (31, 0.17), (34, 0.14), (36, 0.12),
)


def events_chroma(events: list[NoteEvent], frames: int | None = None, *, hop_s: float = 0.05) -> np.ndarray:
    """Chroma straight from note events, with a short decay after each note.

    Each note contributes its harmonic series, not only its fundamental, so
    the result is comparable with the chroma of real audio.
    """
    if not events and not frames:
        return np.zeros((0, 12))
    end = max((e.start + e.duration for e in events), default=0.0)
    count = frames if frames is not None else int(end / hop_s) + 2
    chroma = np.zeros((count, 12))
    for event in events:
        first = int(event.start / hop_s)
        last = int((event.start + event.duration) / hop_s) + 1
        if first >= count:
            continue
        root = int(round(event.midi))
        span = np.arange(first, min(last, count))
        decay = np.exp(-(span - first) * hop_s / 1.5)
        for semitones, weight in HARMONIC_TEMPLATE:
            # Square root, matching the compression applied to audio chroma.
            chroma[span, (root + semitones) % 12] += np.sqrt(weight) * event.velocity * decay
    return chroma


def _normalise(chroma: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(chroma, axis=1, keepdims=True)
    return np.divide(chroma, norms, out=np.zeros_like(chroma), where=norms > 0)


#: Added to every step that is not diagonal. Without it the path is free to
#: squeeze a whole tab into one repetition of a riff the record plays twice -
#: chroma cannot tell the repeats apart, so only the cost of distorting time
#: can.
STEP_PENALTY = 0.12


def dtw_path(cost: np.ndarray, *, subsequence: bool = False, step_penalty: float = STEP_PENALTY) -> list[tuple[int, int]]:
    """Minimum-cost monotonic path through a cost matrix.

    Rows are the tab, columns the record. With ``subsequence`` the path may
    start and end anywhere along the record - the tab of one riff is found
    *inside* a whole song, rather than stretched across it, and a silent intro
    is skipped instead of absorbing the first note.
    """
    n, m = cost.shape
    big = np.inf
    acc = np.full((n, m), big)
    if subsequence:
        acc[0, :] = cost[0, :]
    else:
        acc[0, 0] = cost[0, 0]
    # Anti-diagonal sweep: cells with i + j == k depend only on k-1 and k-2.
    for k in range(1, n + m - 1):
        i = np.arange(max(0, k - m + 1), min(n, k + 1))
        j = k - i
        if subsequence:
            keep = i > 0
            i, j = i[keep], j[keep]
            if not len(i):
                continue
        best = np.full(len(i), big)
        has_up = i > 0
        best[has_up] = np.minimum(best[has_up], acc[i[has_up] - 1, j[has_up]] + step_penalty)
        has_left = j > 0
        best[has_left] = np.minimum(best[has_left], acc[i[has_left], j[has_left] - 1] + step_penalty)
        has_diag = has_up & has_left
        best[has_diag] = np.minimum(best[has_diag], acc[i[has_diag] - 1, j[has_diag] - 1])
        acc[i, j] = cost[i, j] + best

    end = int(np.argmin(acc[n - 1])) if subsequence else m - 1
    path = [(n - 1, end)]
    i, j = n - 1, end
    while i > 0 or (j > 0 and not subsequence):
        options = []
        if i > 0 and j > 0:
            options.append((acc[i - 1, j - 1], i - 1, j - 1))
        if i > 0:
            options.append((acc[i - 1, j] + step_penalty, i - 1, j))
        if j > 0 and not (subsequence and i == 0):
            options.append((acc[i, j - 1] + step_penalty, i, j - 1))
        if not options:
            break
        _, i, j = min(options)
        path.append((i, j))
    path.reverse()
    return path


@dataclass
class AlignmentResult:
    events: list[NoteEvent]
    tempo_ratio: float
    """Record seconds per tab second, overall. 1.0 means the tab's tempo was right."""
    similarity_before: float
    similarity_after: float

    def describe(self) -> str:
        return (
            f"aligned {len(self.events)} note(s); the record runs at "
            f"{1 / self.tempo_ratio:.2f}x the tab's tempo; chroma agreement "
            f"{self.similarity_before:.0%} -> {self.similarity_after:.0%}"
        )

    def to_dict(self) -> dict:
        return {
            "events": [e.to_dict() for e in self.events],
            "tempo_ratio": round(self.tempo_ratio, 4),
            "similarity_before": round(self.similarity_before, 3),
            "similarity_after": round(self.similarity_after, 3),
            "describe": self.describe(),
        }


def estimate_scale(
    events: list[NoteEvent],
    target: np.ndarray,
    *,
    hop_s: float = 0.05,
    scales: np.ndarray | None = None,
) -> tuple[float, float, float]:
    """The single tempo ratio and offset that best fit the tab to the record.

    A tab is often simply at the wrong tempo - exported at 120 when the band
    played 96. That is one number, and warping is a poor way to find one
    number: every step it takes off the diagonal is charged as a local
    deviation. So the global ratio is searched for first, by stretching the
    tab's chroma over a range of ratios and sliding each along the record;
    the warp then only has to handle what is genuinely local.

    Returns ``(scale, offset_s, score)`` where record time = scale * tab time
    + offset.
    """
    if scales is None:
        scales = np.geomspace(0.5, 2.0, 49)
    target_n = _normalise(target)
    best = (1.0, 0.0, -1.0)
    for scale in scales:
        stretched = [NoteEvent(e.start * scale, e.duration * scale, e.midi, e.velocity) for e in events]
        source = _normalise(events_chroma(stretched, hop_s=hop_s))
        n, m = len(source), len(target_n)
        if n == 0 or n > m + n:
            continue
        # Correlate every pitch class across every lag at once.
        size = 1 << int(np.ceil(np.log2(n + m)))
        spectrum_t = np.fft.rfft(target_n, size, axis=0)
        spectrum_s = np.fft.rfft(source[::-1], size, axis=0)
        correlation = np.fft.irfft(spectrum_t * spectrum_s, size, axis=0).sum(axis=1)
        # Lag L aligns source frame 0 with target frame L - (n - 1).
        lags = np.arange(len(correlation)) - (n - 1)
        valid = (lags >= -n // 4) & (lags <= m - n // 2)
        if not valid.any():
            continue
        scores = np.where(valid, correlation, -np.inf) / max(1, np.count_nonzero(np.linalg.norm(source, axis=1)))
        index = int(np.argmax(scores))
        if scores[index] > best[2]:
            best = (float(scale), float(lags[index] * hop_s), float(scores[index]))
    return best


def _agreement(a: np.ndarray, b: np.ndarray) -> float:
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    a, b = _normalise(a[:n]), _normalise(b[:n])
    weights = (np.linalg.norm(a, axis=1) > 0) & (np.linalg.norm(b, axis=1) > 0)
    if not weights.any():
        return 0.0
    return float((a[weights] * b[weights]).sum(axis=1).mean())


def align_events(
    events: list[NoteEvent],
    audio: np.ndarray,
    sample_rate: int,
    *,
    hop_s: float = 0.05,
    snap: bool = True,
) -> AlignmentResult:
    """Warp note events onto a recording's timeline.

    Args:
        snap: Move each warped note to the record's nearest onset.
    """
    if not events:
        return AlignmentResult([], 1.0, 0.0, 0.0)
    target = audio_chroma(audio, sample_rate, hop_s=hop_s)
    before = _agreement(target, events_chroma(events, hop_s=hop_s))

    # Global tempo and offset first; then the warp handles what is local.
    scale, offset, _ = estimate_scale(events, target, hop_s=hop_s)
    original = events
    events = [
        NoteEvent(e.start * scale + offset, e.duration * scale, e.midi, e.velocity, e.string, e.technique)
        for e in events
    ]
    lead = max(0.0, -min(e.start for e in events))
    events = [NoteEvent(e.start + lead, e.duration, e.midi, e.velocity, e.string, e.technique) for e in events]
    source = events_chroma(events, hop_s=hop_s)

    a, b = _normalise(source), _normalise(target)
    cost = 1.0 - a @ b.T
    # Silent frames match each other perfectly and anything else badly, which
    # is what keeps a count-in from being stretched across the intro.
    silent_a = np.linalg.norm(source, axis=1) == 0
    silent_b = np.linalg.norm(target, axis=1) == 0
    cost[np.ix_(silent_a, silent_b)] = 0.0
    cost[np.ix_(silent_a, ~silent_b)] = 0.8
    cost[np.ix_(~silent_a, silent_b)] = 0.8
    path = dtw_path(cost, subsequence=True)

    tab_frames = np.array([p[0] for p in path], dtype=float)
    rec_frames = np.array([p[1] for p in path], dtype=float)
    # Where the path is flat in one axis, average - the mapping must be a
    # function of tab time.
    unique_tab, inverse = np.unique(tab_frames, return_inverse=True)
    mapped = np.zeros(len(unique_tab))
    np.add.at(mapped, inverse, rec_frames)
    counts = np.bincount(inverse)
    mapped /= counts

    def warp(seconds: float) -> float:
        return float(np.interp(seconds / hop_s, unique_tab, mapped)) * hop_s

    # The path is only as fine as the chroma hop. The record's own onsets are
    # five-millisecond accurate, so each note moves to the nearest one that is
    # close enough to plausibly be it.
    from .segment import detect_onsets

    onsets = np.array(detect_onsets(audio, sample_rate)) if snap else np.zeros(0)
    # Never reach further than about half the gap between the record's own
    # notes, or a fast passage snaps every note onto its neighbour.
    window = max(0.15, 3 * hop_s)
    if len(onsets) >= 3:
        gaps = np.diff(np.sort(onsets))
        gaps = gaps[gaps > 0.03]
        if len(gaps):
            window = float(np.clip(0.45 * np.median(gaps), 0.04, window))

    # Snapping is one-to-one and keeps order: a chord's notes share one
    # onset, but two successive notes never collapse onto the same one, and a
    # note never jumps back past the one before it.
    claimed: dict[float, float] = {}
    previous = -np.inf
    warped = []
    for event in events:
        start = warp(event.start)
        end = warp(event.start + event.duration)
        if len(onsets):
            order = np.argsort(np.abs(onsets - start))
            for index in order[:4]:
                candidate = float(onsets[index])
                if abs(candidate - start) > window:
                    break
                if candidate < previous - 1e-6:
                    continue
                owner = claimed.get(candidate)
                if owner is not None and abs(owner - event.start) > 0.03:
                    continue
                claimed[candidate] = event.start
                end += candidate - start
                start = candidate
                break
        previous = max(previous, start)
        warped.append(NoteEvent(round(start, 4), max(0.03, end - start), event.midi, event.velocity, event.string, event.technique))

    span_tab = max(e.start for e in original) - min(e.start for e in original)
    span_rec = max(e.start for e in warped) - min(e.start for e in warped)
    ratio = span_rec / span_tab if span_tab > 0 else 1.0
    after = _agreement(target, events_chroma(warped, len(target), hop_s=hop_s))
    return AlignmentResult(warped, ratio, before, after)

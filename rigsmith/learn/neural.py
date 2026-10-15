"""A small network that learns how your guitar's notes are built along the neck.

The voice model (:mod:`.voice`) fills a fret it has not heard by
interpolating between the two nearest notes it has. That is exact where the
neighbours are close and good, and it copies their faults where they are
not: one dead note, or a fret that buzzed, is carried straight into
everything interpolated from it.

This learns the same quantities - each harmonic's starting level and decay,
on a fixed frequency grid - as a smooth function of string and fret, from
every note at once. On its own it does not beat the interpolation. Averaged
with it, it does, a little and consistently: on a chromatic session
(78 notes), predicting each note with that note held out, the blend was 5%
closer than interpolation alone (2.69 against 2.84 dB on the harmonic
levels), and 4% closer with per-note measurement noise added (3.92 against
4.10). With only a quick session's 24 notes it was clearly worse, which is
why it is only trained from :data:`MIN_NOTES` up.

Plain numpy: a 9-input, 24-unit tanh layer, trained full-batch with Adam in
a fraction of a second. No GPU, no framework, and the same weights every
time for the same notes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .instrument import midi_to_hz

#: Fewer notes than this and the network loses to plain interpolation.
MIN_NOTES = 48

#: Where the network describes a note: 40 points, 80 Hz to 10 kHz.
GRID = np.geomspace(80, 10000, 40)

#: Open strings by number, standard tuning; a string's fret is measured from it.
OPEN_MIDI = {1: 64, 2: 59, 3: 55, 4: 50, 5: 45, 6: 40}

HIDDEN = 24
ITERATIONS = 1500
WEIGHT_DECAY = 1e-3
LEARNING_RATE = 0.01


def features(midi: float, string: int, open_midi: dict | None = None) -> np.ndarray:
    """String (one-hot), fret position and its square, and pitch."""
    opens = open_midi or OPEN_MIDI
    one_hot = np.zeros(6)
    if 1 <= string <= 6:
        one_hot[string - 1] = 1.0
    fret = (midi - opens.get(string, 40)) / 24.0
    return np.concatenate([one_hot, [fret, fret * fret, midi / 100.0]])


def on_grid(values, f0: float, inharmonicity: float) -> np.ndarray:
    """Per-harmonic values resampled onto :data:`GRID` by frequency."""
    values = np.asarray(values, dtype=np.float64)
    k = np.arange(1, len(values) + 1)
    freqs = k * f0 * np.sqrt(1 + inharmonicity * k * k)
    return np.interp(np.log(GRID), np.log(freqs), values, left=values[0], right=values[-1])


def off_grid(grid_values, f0: float, inharmonicity: float, count: int) -> np.ndarray:
    """Grid values back onto one note's harmonics."""
    k = np.arange(1, count + 1)
    freqs = k * f0 * np.sqrt(1 + inharmonicity * k * k)
    return np.interp(np.log(freqs), np.log(GRID), np.asarray(grid_values))


@dataclass
class NeuralVoice:
    """The trained network, and how to undo its output scaling."""

    w1: np.ndarray
    b1: np.ndarray
    w2: np.ndarray
    b2: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    notes: int
    open_midi: dict

    def predict(self, midi: float, string: int) -> tuple[np.ndarray, np.ndarray]:
        """(levels, decays) on :data:`GRID` for a note."""
        x = features(midi, string, self.open_midi)
        y = (np.tanh(x @ self.w1 + self.b1) @ self.w2 + self.b2) * self.scale + self.mean
        return y[: len(GRID)], y[len(GRID) :]

    def to_dict(self) -> dict:
        return {
            "w1": self.w1.tolist(), "b1": self.b1.tolist(), "w2": self.w2.tolist(), "b2": self.b2.tolist(),
            "mean": self.mean.tolist(), "scale": self.scale.tolist(), "notes": self.notes,
            "open_midi": {str(k): v for k, v in self.open_midi.items()},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NeuralVoice":
        arrays = {k: np.asarray(data[k], dtype=np.float64) for k in ("w1", "b1", "w2", "b2", "mean", "scale")}
        opens = {int(k): float(v) for k, v in (data.get("open_midi") or {}).items()} or dict(OPEN_MIDI)
        return cls(**arrays, notes=int(data.get("notes", 0)), open_midi=opens)


def train(notes, *, open_midi: dict | None = None, seed: int = 0) -> NeuralVoice | None:
    """Fit the network to a voice model's notes; None when there are too few."""
    usable = [n for n in notes if n.string and len(n.level_db) >= 2]
    if len(usable) < MIN_NOTES:
        return None
    opens = dict(open_midi or OPEN_MIDI)
    x = np.array([features(n.midi, n.string, opens) for n in usable])
    y = np.array([
        np.concatenate([
            on_grid(n.level_db, midi_to_hz(n.midi), n.inharmonicity),
            on_grid(n.decay_db_s, midi_to_hz(n.midi), n.inharmonicity),
        ])
        for n in usable
    ])
    mean, scale = y.mean(axis=0), y.std(axis=0) + 1e-6
    z = (y - mean) / scale
    rng = np.random.default_rng(seed)
    params = [
        rng.normal(0, 0.3, (x.shape[1], HIDDEN)), np.zeros(HIDDEN),
        rng.normal(0, 0.3, (HIDDEN, z.shape[1])), np.zeros(z.shape[1]),
    ]
    first = [np.zeros_like(p) for p in params]
    second = [np.zeros_like(p) for p in params]
    for step in range(1, ITERATIONS + 1):
        w1, b1, w2, b2 = params
        hidden = np.tanh(x @ w1 + b1)
        error = (hidden @ w2 + b2 - z) / len(x)
        grad_w2 = hidden.T @ error + WEIGHT_DECAY * w2
        back = error @ w2.T * (1 - hidden * hidden)
        grads = [x.T @ back + WEIGHT_DECAY * w1, back.sum(axis=0), grad_w2, error.sum(axis=0)]
        for i, (param, grad) in enumerate(zip(params, grads)):
            first[i] = 0.9 * first[i] + 0.1 * grad
            second[i] = 0.999 * second[i] + 0.001 * grad * grad
            param -= LEARNING_RATE * (first[i] / (1 - 0.9**step)) / (np.sqrt(second[i] / (1 - 0.999**step)) + 1e-8)
    return NeuralVoice(*params, mean=mean, scale=scale, notes=len(usable), open_midi=opens)

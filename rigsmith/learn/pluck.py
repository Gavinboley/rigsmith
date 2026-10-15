"""A synthetic plucked string, for when there is no guitar in the room.

Two jobs, and neither of them is pretending to be your instrument:

* **Tests.** A learning pipeline that can only be exercised with a guitar
  plugged in is a pipeline nobody runs before committing. A string whose pitch
  is known exactly is the ground truth every segmenter and transcriber here is
  checked against.
* **A placeholder voice.** Rendering a song's part before you have recorded a
  single note still has a use - the effects chain can be heard on *something*
  - as long as everything downstream says plainly that the DI was synthetic.

The model is extended Karplus-Strong: a delay line one period long, a loss
filter inside the loop, a fractional-delay allpass so the pitch is exact
rather than quantised to whole samples, and two comb filters for where the
string was picked and where the pickup sits. Then a resonant low-pass for the
pickup itself, which is the single biggest reason a DI does not sound like a
microphone - a magnetic pickup into a cable is a second-order filter peaking
somewhere between 2 and 7 kHz.

It is all ``scipy.signal.lfilter`` rather than a Python loop over samples: the
whole loop is one rational transfer function, so it runs at C speed.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .instrument import midi_to_hz

DEFAULT_SAMPLE_RATE = 48000

#: Peak level of a firmly picked note on a DI, in dBFS. Real guitars land
#: anywhere from -20 to -3 depending on pickups and interface gain; this is a
#: middling passive humbucker into an interface set with some headroom.
TYPICAL_PEAK_DBFS = -10.0


@dataclass(frozen=True)
class Pickup:
    """Where the pickup sits and how it rings."""

    name: str
    position: float
    """Distance from the bridge as a fraction of the scale length."""
    resonance_hz: float
    q: float
    output: float
    """Relative output - humbuckers are hotter than single coils."""


PICKUPS = {
    "bridge": Pickup("bridge humbucker", 0.065, 3200.0, 1.6, 1.0),
    "neck": Pickup("neck humbucker", 0.23, 2800.0, 1.4, 0.85),
    "middle": Pickup("middle single coil", 0.16, 5200.0, 2.2, 0.55),
    "bridge-single": Pickup("bridge single coil", 0.07, 5800.0, 2.4, 0.6),
    "neck-single": Pickup("neck single coil", 0.24, 5000.0, 2.0, 0.6),
}


def _lfilter():
    from scipy.signal import lfilter

    return lfilter


def _resonant_lowpass(signal: np.ndarray, sample_rate: int, cutoff: float, q: float) -> np.ndarray:
    """RBJ cookbook low-pass biquad."""
    lfilter = _lfilter()
    cutoff = min(cutoff, 0.45 * sample_rate)
    w0 = 2 * np.pi * cutoff / sample_rate
    alpha = np.sin(w0) / (2 * q)
    cos = np.cos(w0)
    b = np.array([(1 - cos) / 2, 1 - cos, (1 - cos) / 2])
    a = np.array([1 + alpha, -2 * cos, 1 - alpha])
    return lfilter(b / a[0], a / a[0], signal)


def _comb(signal: np.ndarray, delay: int) -> np.ndarray:
    """``x[n] - x[n - delay]``: the notches a pick or pickup position puts in."""
    if delay <= 0 or delay >= len(signal):
        return signal
    out = signal.copy()
    out[delay:] -= signal[:-delay]
    return out


def sustain_seconds(midi: float, *, string_gauge: str = "auto") -> float:
    """How long a note on a solid body rings before falling 60 dB.

    Low notes ring longer; a wound string loses its top faster. These are
    rough figures from measuring real DIs, not physics.
    """
    hz = midi_to_hz(midi)
    return float(np.clip(7.5 * (82.4 / hz) ** 0.45, 1.2, 9.0))


def pluck(
    midi: float,
    duration: float,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    velocity: float = 0.75,
    pickup: str = "bridge",
    pick_position: float = 0.14,
    brightness: float | None = None,
    sustain: float | None = None,
    seed: int | None = None,
) -> np.ndarray:
    """One plucked note, as a DI would record it.

    Args:
        midi: Pitch, fractional allowed - 40.3 is an E2 thirty cents sharp.
        duration: Seconds of audio to return. The string is damped over the
            last 40 ms rather than cut, the way a fretting hand lifts.
        velocity: 0-1. Harder picking is louder *and* brighter.
        pick_position: Fraction of the scale length from the bridge.
        brightness: 0-1 loop-filter brightness; defaults from the pitch, since
            plain high strings are brighter than wound low ones.
        sustain: T60 in seconds; defaults to :func:`sustain_seconds`.
    """
    lfilter = _lfilter()
    rng = np.random.default_rng(seed)
    frequency = midi_to_hz(midi)
    period = sample_rate / frequency
    length = max(1, int(duration * sample_rate))
    velocity = float(np.clip(velocity, 0.05, 1.0))

    if brightness is None:
        # Wound strings (roughly below G3) are darker and lose the top sooner.
        brightness = 0.55 + 0.3 * float(np.clip((midi - 40) / 30, 0, 1))
    brightness = float(np.clip(brightness, 0.5, 0.97))
    t60 = sustain if sustain is not None else sustain_seconds(midi)
    a = brightness

    # The loop filter loses a little at the fundamental on every pass, and a
    # high note makes a thousand passes a second - left alone that doubles
    # the decay rate of the top strings. Divide it back out, so the T60 is
    # the fundamental's and only the harmonics die faster, as they should.
    omega = 2 * np.pi * frequency / sample_rate
    filter_loss = float(np.sqrt(a * a + (1 - a) ** 2 + 2 * a * (1 - a) * np.cos(omega)))
    loop_gain = min(0.99995, 10.0 ** (-3.0 / (t60 * frequency)) / max(filter_loss, 1e-6))

    # Loop filter L(z) = a + (1-a) z^-1 delays low frequencies by (1-a)
    # samples; an allpass makes up the fraction so the pitch is exact.
    filter_delay = 1.0 - a
    whole = int(period - filter_delay - 0.1)
    whole = max(whole, 2)
    fraction = period - filter_delay - whole
    c = (1.0 - fraction) / (1.0 + fraction)

    # Excitation: the string's shape the instant the pick lets go - a
    # triangle peaking at the pick position, which is what gives a real pluck
    # its strong fundamental and 1/k^2 harmonic roll-off - plus a little
    # noise for the pick's scrape, low-passed harder for a soft pick. A pure
    # noise burst (textbook Karplus-Strong) leaves the fundamental to chance,
    # and an estimator tested against that learns the wrong lesson.
    span = max(2, int(round(period)))
    apex = max(1, min(span - 1, int(round(pick_position * span))))
    shape = np.concatenate([np.linspace(0.0, 1.0, apex, endpoint=False), np.linspace(1.0, 0.0, span - apex)])
    shape -= shape.mean()
    scrape = rng.uniform(-1.0, 1.0, size=span) * (0.15 + 0.35 * velocity)
    pole = 0.75 - 0.6 * velocity
    burst = shape + lfilter([1.0 - pole], [1.0, -pole], scrape)
    burst = np.pad(burst, (0, 4))
    excitation = np.zeros(length)
    excitation[: min(len(burst), length)] = burst[: length]

    # 1 / (1 - g L(z) A(z) z^-N), with A(z) = (c + z^-1) / (1 + c z^-1):
    # numerator (1 + c z^-1); denominator (1 + c z^-1) - g z^-N (a + (1-a) z^-1)(c + z^-1)
    numerator = np.array([1.0, c])
    denominator = np.zeros(whole + 3)
    denominator[0] = 1.0
    denominator[1] += c
    product = np.array([a * c, a + (1 - a) * c, (1 - a)])
    denominator[whole : whole + 3] -= loop_gain * product
    string = lfilter(numerator, denominator, excitation)

    spec = PICKUPS.get(pickup, PICKUPS["bridge"])
    out = _comb(string, int(round(spec.position * period)))
    out = _resonant_lowpass(out, sample_rate, spec.resonance_hz, spec.q)

    # Lift the fretting hand: a short damping ramp, not a cut.
    release = min(length, int(0.04 * sample_rate))
    if release > 1:
        out[-release:] *= np.linspace(1.0, 0.0, release)

    peak = float(np.max(np.abs(out))) or 1.0
    target = 10 ** (TYPICAL_PEAK_DBFS / 20) * spec.output * (0.35 + 0.65 * velocity)
    return out * (target / peak)


def render_notes(
    notes,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    pickup: str = "bridge",
    tail: float = 0.5,
    seed: int | None = 0,
) -> np.ndarray:
    """Overlap-add a list of ``(start_s, duration_s, midi, velocity)`` notes."""
    notes = list(notes)
    if not notes:
        return np.zeros(int(tail * sample_rate))
    end = max(start + duration for start, duration, _, _ in notes) + tail
    out = np.zeros(int(end * sample_rate) + 1)
    rng = np.random.default_rng(seed)
    for start, duration, midi, velocity in notes:
        note = pluck(
            midi,
            duration,
            sample_rate=sample_rate,
            velocity=velocity,
            pickup=pickup,
            seed=int(rng.integers(0, 2**31)),
        )
        first = int(round(start * sample_rate))
        if first < 0:
            # Humanised timing can put the first note a hair before zero; a
            # negative index would wrap to the end of the buffer instead.
            note, first = note[-first:], 0
        if first >= len(out) or not len(note):
            continue
        out[first : first + len(note)] += note[: len(out) - first]
    return out

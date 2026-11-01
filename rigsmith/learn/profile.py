"""A guitar, learned: its notes, and what they say about it.

A :class:`GuitarProfile` is two things kept together:

* **Samples.** Every usable note from the recordings it was built from, cut
  out with a little pre-roll and its natural decay, labelled with pitch,
  string, fret and how hard it was played. That is a multisampled instrument,
  and :mod:`.synth` plays it.
* **A fingerprint.** What the instrument does to every note it plays - the
  long-term spectral balance, how hot the pickups are, how much hum rides
  along, whether it was in tune. Those are the properties a tone comparison is
  quietly sensitive to, and the fingerprint is where they become visible.

The level figures matter more than they look. An amp model's gain knob is
only meaningful relative to what arrives at its input, and a humbucker that
peaks at -6 dBFS drives the same model into a different place than a single
coil at -18. A profile records its own level so that whatever renders through
it - a synthesised part, a reamp - can arrive at the amp the way the guitar
would have.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .instrument import midi_to_note, tuning as get_tuning
from .segment import Note

#: Third-octave band centres for the fingerprint's spectral balance.
BAND_CENTRES = (
    63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800, 1000, 1250,
    1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500,
)

#: Pre-roll kept before each onset, so the pick attack is not clipped off.
PRE_ROLL_S = 0.005

#: Longest a single sample is kept. Past this a guitar note is noise floor.
MAX_SAMPLE_S = 6.0

#: Dynamic layers relative to the profile's own median peak, in dB.
SOFT_BELOW_DB = -6.0
HARD_ABOVE_DB = 4.0


@dataclass
class SampleNote:
    """One note kept as a sample."""

    sample_id: str
    file: str
    """Relative to the profile's directory."""
    midi: float
    string: int | None = None
    fret: int | None = None
    peak_db: float = -120.0
    rms_db: float = -120.0
    decay_db_s: float = 0.0
    centroid_hz: float = 0.0
    attack_centroid_hz: float = 0.0
    duration: float = 0.0
    technique: str = "pick"
    dynamic: str = "mf"
    source: str = ""
    """The take or file it was cut from."""
    flags: list[str] = field(default_factory=list)

    @property
    def nearest(self) -> int:
        return int(round(self.midi))

    @property
    def cents(self) -> float:
        return 100.0 * (self.midi - self.nearest)

    @property
    def name(self) -> str:
        return midi_to_note(self.midi)

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(name=self.name, cents=round(self.cents, 1))
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "SampleNote":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Fingerprint:
    """What the instrument does to every note it plays."""

    bands_db: list[float] = field(default_factory=list)
    """Spectral balance per third-octave band, relative to the loudest band."""
    centroid_hz: float = 0.0
    median_peak_db: float = -120.0
    p95_peak_db: float = -120.0
    noise_floor_db: float = -120.0
    hum_hz: float | None = None
    """50 or 60, when mains hum rides along - an amp with gain makes it loud."""
    hum_db: float | None = None
    tuning_cents: float | None = None
    """Median deviation of every pitched note from concert pitch."""

    def describe(self) -> str:
        bits = [
            f"peaks {self.median_peak_db:.1f} dBFS typical, {self.p95_peak_db:.1f} hard",
            f"noise floor {self.noise_floor_db:.0f} dBFS",
            f"brightness {self.centroid_hz:.0f} Hz",
        ]
        if self.hum_hz:
            bits.append(f"{self.hum_hz:.0f} Hz hum at {self.hum_db:.0f} dBFS")
        if self.tuning_cents is not None:
            bits.append(f"{self.tuning_cents:+.0f} cents from concert pitch")
        return ", ".join(bits)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["bands_hz"] = list(BAND_CENTRES)
        data["describe"] = self.describe()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Fingerprint":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class GuitarProfile:
    """One instrument (and pickup), learned from your playing."""

    profile_id: str
    guitar: str | None = None
    pickup: str | None = None
    tuning: str = "E standard"
    note: str | None = None
    samples: list[SampleNote] = field(default_factory=list)
    fingerprint: Fingerprint = field(default_factory=Fingerprint)
    sources: list[dict] = field(default_factory=list)
    has_voice: bool = False
    sample_rate: int = 48000
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    updated_at: str = ""

    # -- what it covers ---------------------------------------------------

    @property
    def label(self) -> str:
        bits = [p for p in (self.guitar, self.pickup) if p]
        return " ".join(bits) if bits else self.profile_id

    def pitches(self) -> list[int]:
        return sorted({s.nearest for s in self.samples if s.technique == "pick"})

    def coverage(self, *, frets: int = 24) -> dict:
        """A strings x frets grid of how many samples sit at each position.

        What the UI draws as a fretboard, and what tells you which part of the
        neck the profile is guessing about.
        """
        tune = get_tuning(self.tuning)
        grid = [[0] * (frets + 1) for _ in range(tune.string_count)]
        unplaced = 0
        for sample in self.samples:
            if sample.string and 1 <= sample.string <= tune.string_count and sample.fret is not None and 0 <= sample.fret <= frets:
                grid[sample.string - 1][sample.fret] += 1
            else:
                unplaced += 1
        filled = sum(1 for row in grid for count in row if count)
        return {
            "strings": tune.string_count,
            "frets": frets,
            "grid": grid,
            "filled": filled,
            "positions": tune.string_count * (frets + 1),
            "unplaced": unplaced,
        }

    def dynamics(self) -> dict[str, int]:
        counts = {"pp": 0, "mf": 0, "ff": 0}
        for sample in self.samples:
            counts[sample.dynamic] = counts.get(sample.dynamic, 0) + 1
        return counts

    def describe(self) -> str:
        cov = self.coverage(frets=12)
        pitches = self.pitches()
        span = (
            f"{midi_to_note(pitches[0])}-{midi_to_note(pitches[-1])}" if pitches else "no pitched notes"
        )
        lines = [
            f"{self.profile_id}  {self.label}  ({self.tuning})",
            f"  {len(self.samples)} sample(s), {span}, "
            f"{cov['filled']}/{cov['positions']} positions to fret 12"
            + ("  + voice model" if self.has_voice else ""),
            f"  {self.fingerprint.describe()}",
        ]
        if self.note:
            lines.append(f"  {self.note}")
        return "\n".join(lines)

    # -- choosing a sample --------------------------------------------------

    def choose(
        self,
        midi: float,
        *,
        peak_db: float | None = None,
        string: int | None = None,
        technique: str = "pick",
    ) -> tuple[SampleNote | None, float]:
        """The best sample for a note, and the shift it needs in semitones.

        Distance is mostly pitch - every semitone of shifting is audible - then
        the string, because the same pitch on a different string is a
        different timbre, then dynamics. A palm mute is only ever chosen for a
        palm mute and vice versa: they are not the same sound at any pitch.
        """
        pool = [s for s in self.samples if s.technique == technique] or [
            s for s in self.samples if s.technique == "pick"
        ]
        if not pool:
            return None, 0.0
        target_peak = peak_db if peak_db is not None else self.fingerprint.median_peak_db

        def distance(sample: SampleNote) -> float:
            shift = abs(midi - sample.midi)
            cost = shift * 1.0
            if string is not None and sample.string is not None and sample.string != string:
                cost += 0.6
            cost += abs((sample.peak_db or -30) - target_peak) / 12.0
            # Shifting down stretches the decay; shifting up shortens it.
            # Up is a little less objectionable on a plucked string.
            if sample.midi > midi:
                cost += 0.15 * shift
            return cost

        best = min(pool, key=distance)
        return best, float(midi - best.midi)

    # -- storage --------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "guitar": self.guitar,
            "pickup": self.pickup,
            "tuning": self.tuning,
            "note": self.note,
            "samples": [s.to_dict() for s in self.samples],
            "fingerprint": self.fingerprint.to_dict(),
            "sources": self.sources,
            "has_voice": self.has_voice,
            "sample_rate": self.sample_rate,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    def summary(self) -> dict:
        """The profile without its sample list - what a listing needs."""
        data = self.to_dict()
        data.pop("samples")
        data["sample_count"] = len(self.samples)
        data["coverage"] = self.coverage(frets=12)
        data["dynamics"] = self.dynamics()
        pitches = self.pitches()
        data["range"] = [midi_to_note(pitches[0]), midi_to_note(pitches[-1])] if pitches else None
        data["label"] = self.label
        data["describe"] = self.describe()
        resonance = self.resonance()
        data["resonance"] = resonance.to_dict() if resonance else None
        return data

    def resonance(self):
        """Where this guitar's pickup peaks, from its fingerprint - see
        :mod:`.pickup`. None until something has been learned."""
        from .pickup import estimate_from_bands

        bands = self.fingerprint.bands_db
        if not bands or not any(bands):
            return None
        return estimate_from_bands(BAND_CENTRES[: len(bands)], bands)

    @classmethod
    def from_dict(cls, data: dict) -> "GuitarProfile":
        profile = cls(
            profile_id=data["profile_id"],
            guitar=data.get("guitar"),
            pickup=data.get("pickup"),
            tuning=data.get("tuning") or "E standard",
            note=data.get("note"),
            samples=[SampleNote.from_dict(s) for s in data.get("samples", [])],
            fingerprint=Fingerprint.from_dict(data.get("fingerprint") or {}),
            sources=list(data.get("sources", [])),
            has_voice=bool(data.get("has_voice")),
            sample_rate=int(data.get("sample_rate") or 48000),
            created_at=data.get("created_at") or "",
            updated_at=data.get("updated_at") or "",
        )
        return profile

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = directory / "profile.json"
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def load(cls, directory: Path) -> "GuitarProfile":
        data = json.loads((directory / "profile.json").read_text(encoding="utf-8"))
        return cls.from_dict(data)


# --------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------


def slice_notes(
    audio: np.ndarray,
    sample_rate: int,
    notes: list[Note],
) -> list[tuple[Note, np.ndarray]]:
    """Cut each note out with pre-roll and its natural tail.

    A note keeps ringing past the point the segmenter called its end; cutting
    there would leave every sample with an abrupt stop that the sampler then
    plays back as a muted string. So each sample runs to the next onset (or a
    maximum), and fades over its last 20 ms.
    """
    cut: list[tuple[Note, np.ndarray]] = []
    pre = int(PRE_ROLL_S * sample_rate)
    fade = int(0.02 * sample_rate)
    for index, note in enumerate(notes):
        first = max(0, int(note.start * sample_rate) - pre)
        following = notes[index + 1].start if index + 1 < len(notes) else len(audio) / sample_rate
        last = min(
            len(audio),
            int((following - 0.002) * sample_rate),
            first + int(MAX_SAMPLE_S * sample_rate),
        )
        if last - first < int(0.05 * sample_rate):
            continue
        piece = np.array(audio[first:last], dtype=np.float64)
        if len(piece) > fade:
            piece[-fade:] *= np.linspace(1.0, 0.0, fade)
        cut.append((note, piece))
    return cut


def classify_dynamics(samples: list[SampleNote]) -> None:
    """Label pp/mf/ff by where each peak sits against the set's own median.

    Only for samples whose protocol did not already say - a dynamics ladder
    knows which note was meant to be soft better than the level does.
    """
    peaks = [s.peak_db for s in samples if s.technique == "pick"]
    if len(peaks) < 4:
        return
    median = float(np.median(peaks))
    for sample in samples:
        if "dynamic-labelled" in sample.flags:
            continue
        if sample.peak_db < median + SOFT_BELOW_DB:
            sample.dynamic = "pp"
        elif sample.peak_db > median + HARD_ABOVE_DB:
            sample.dynamic = "ff"
        else:
            sample.dynamic = "mf"


def fingerprint(
    audio: np.ndarray,
    sample_rate: int,
    notes: list[Note],
) -> Fingerprint:
    """Measure the instrument across a whole recording."""
    from .segment import _envelope_db

    audio = np.asarray(audio, dtype=np.float64)
    result = Fingerprint()
    if not len(audio):
        return result

    envelope = _envelope_db(audio, sample_rate)
    result.noise_floor_db = round(float(np.percentile(envelope, 5)), 1)

    # Spectral balance over the notes themselves, not the gaps between them.
    n_fft = 8192
    window = np.hanning(n_fft)
    power = np.zeros(n_fft // 2 + 1)
    frames = 0
    for note in notes:
        first = int(note.start * sample_rate)
        last = min(len(audio), int(min(note.end, note.start + 1.5) * sample_rate))
        for start in range(first, last - n_fft, n_fft // 2):
            chunk = audio[start : start + n_fft] * window
            power += np.abs(np.fft.rfft(chunk)) ** 2
            frames += 1
    if frames:
        freqs = np.fft.rfftfreq(n_fft, 1 / sample_rate)
        bands = []
        for centre in BAND_CENTRES:
            low, high = centre / 2 ** (1 / 6), centre * 2 ** (1 / 6)
            mask = (freqs >= low) & (freqs < high)
            energy = float(power[mask].sum()) if mask.any() else 0.0
            bands.append(10 * math.log10(max(energy, 1e-20)))
        top = max(bands)
        result.bands_db = [round(b - top, 2) for b in bands]
        total = float(power.sum())
        if total > 0:
            result.centroid_hz = round(float((freqs * power).sum() / total), 1)

    peaks = [n.peak_db for n in notes]
    if peaks:
        result.median_peak_db = round(float(np.median(peaks)), 1)
        result.p95_peak_db = round(float(np.percentile(peaks, 95)), 1)
    cents = [n.cents for n in notes if n.pitched]
    if len(cents) >= 3:
        result.tuning_cents = round(float(np.median(cents)), 1)

    hum = detect_hum(audio, sample_rate, notes)
    if hum:
        result.hum_hz, result.hum_db = hum
    return result


def detect_hum(audio, sample_rate, notes) -> tuple[float, float] | None:
    """Mains hum in the gaps between notes, if there is any to speak of.

    Looked for only where nothing is being played: in the gaps a 50 or 60 Hz
    line standing well above its neighbours is hum, while during a note it
    could just as easily be the note.
    """
    quiet = np.ones(len(audio), dtype=bool)
    for note in notes:
        first = max(0, int((note.start - 0.02) * sample_rate))
        last = min(len(audio), int((note.end + 0.3) * sample_rate))
        quiet[first:last] = False
    gaps = audio[quiet]
    if len(gaps) < sample_rate // 2:
        return None
    gaps = gaps[: sample_rate * 20]
    n_fft = 1 << int(math.log2(min(len(gaps), sample_rate * 2)))
    spectrum = np.abs(np.fft.rfft(gaps[:n_fft] * np.hanning(n_fft))) / (n_fft / 4)
    best = None
    for mains in (50.0, 60.0):
        level = 0.0
        for harmonic in (1, 2, 3):
            target = mains * harmonic
            index = int(round(target * n_fft / sample_rate))
            if index + 8 >= len(spectrum):
                continue
            line = float(spectrum[index - 1 : index + 2].max())
            around = float(np.median(np.concatenate([spectrum[index - 8 : index - 2], spectrum[index + 3 : index + 9]])))
            if around > 0 and line / around > 6:
                level = max(level, line)
        if level > 0 and (best is None or level > best[1]):
            best = (mains, level)
    if best is None:
        return None
    return best[0], round(20 * math.log10(max(best[1], 1e-9)), 1)

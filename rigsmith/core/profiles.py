"""Learned guitars: building them, keeping them, and playing parts on them.

The service layer over :mod:`rigsmith.learn`. A profile lives in its own
directory under the workspace - ``profile.json``, a ``samples/`` folder of cut
notes, and ``voice.json`` once a voice model has been fitted - so it can be
copied to another machine, or deleted, as one thing.

Learning is additive. Record the quick protocol tonight, the chromatic one
next week and the dynamics ladder after that, and each pass adds to the same
profile rather than replacing it. Re-learning the *same* take is refused,
because doubling every sample would quietly double the weight of that one
evening's playing.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import get_settings, require_writable
from .errors import Conflict, Invalid, NotFound
from .jobs import Progress


def _root() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    return settings.profiles_dir


def _dir(profile_id: str) -> Path:
    return _root() / profile_id


def slugify(text: str) -> str:
    from .takes import slugify as _slug

    return _slug(text)


# --------------------------------------------------------------------------
# keeping them
# --------------------------------------------------------------------------


def list_profiles():
    from ..learn.profile import GuitarProfile

    found = []
    root = _root()
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        if (directory / "profile.json").is_file():
            try:
                found.append(GuitarProfile.load(directory))
            except (OSError, ValueError, KeyError):
                continue
    found.sort(key=lambda p: p.updated_at or p.created_at, reverse=True)
    return found


def get(profile_id: str):
    from ..learn.profile import GuitarProfile

    directory = _dir(slugify(profile_id))
    if not (directory / "profile.json").is_file():
        known = ", ".join(p.profile_id for p in list_profiles()) or "none yet"
        raise NotFound(
            f"no guitar profile '{profile_id}'",
            hint=f"Profiles: {known}. Create one with:  rigsmith learn new <name>",
        )
    return GuitarProfile.load(directory)


def create(
    name: str,
    *,
    guitar: str | None = None,
    pickup: str | None = None,
    tuning: str | None = None,
    note: str | None = None,
):
    """Start an empty profile for one guitar and pickup."""
    from ..learn.instrument import tuning as get_tuning
    from ..learn.profile import GuitarProfile

    require_writable("create a guitar profile")
    profile_id = slugify(name)
    directory = _dir(profile_id)
    if (directory / "profile.json").is_file():
        raise Conflict(
            f"a profile called '{profile_id}' already exists",
            hint="Learn into it, or pick another name.",
        )
    tune = get_tuning(tuning)
    profile = GuitarProfile(
        profile_id=profile_id,
        guitar=guitar or name,
        pickup=pickup,
        tuning=tune.name,
        note=note,
    )
    profile.save(directory)
    return profile


def update(profile_id: str, **changes):
    require_writable("edit a guitar profile")
    profile = get(profile_id)
    editable = {"guitar", "pickup", "note", "tuning"}
    unknown = set(changes) - editable
    if unknown:
        raise Invalid(f"cannot change {', '.join(sorted(unknown))} on a profile")
    if changes.get("tuning"):
        from ..learn.instrument import tuning as get_tuning

        changes["tuning"] = get_tuning(changes["tuning"]).name
    for key, value in changes.items():
        if value is not None:
            setattr(profile, key, value)
    profile.save(_dir(profile.profile_id))
    return profile


def delete(profile_id: str) -> str:
    require_writable("delete a guitar profile")
    profile = get(profile_id)
    shutil.rmtree(_dir(profile.profile_id), ignore_errors=True)
    return f"Deleted profile '{profile.profile_id}' and its {len(profile.samples)} sample(s)."


def sample_dir(profile_id: str) -> Path:
    return _dir(slugify(profile_id))


def voice_model(profile_id: str):
    """The fitted voice model, or ``None`` if there is not one yet."""
    from ..learn.voice import VoiceModel

    path = _dir(slugify(profile_id)) / "voice.json"
    if not path.is_file():
        return None
    try:
        return VoiceModel.load(path)
    except (OSError, ValueError, KeyError):
        return None


# --------------------------------------------------------------------------
# learning
# --------------------------------------------------------------------------


@dataclass
class LearnReport:
    """What one pass of learning found and kept."""

    profile_id: str
    source: str
    protocol_id: str
    notes_found: int = 0
    samples_added: int = 0
    alignment: str = ""
    coverage: float | None = None
    concerns: list[str] = field(default_factory=list)
    voice_fitted: bool = False
    notes: list[dict] = field(default_factory=list)

    def render(self) -> str:
        lines = [
            f"{self.profile_id}: learned from {self.source} ({self.protocol_id})",
            f"  {self.notes_found} note(s) heard, {self.samples_added} kept as samples",
        ]
        if self.alignment:
            lines.extend("  " + line for line in self.alignment.splitlines())
        if self.voice_fitted:
            lines.append("  voice model refitted")
        lines.extend(f"  note: {c}" for c in self.concerns)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "source": self.source,
            "protocol_id": self.protocol_id,
            "notes_found": self.notes_found,
            "samples_added": self.samples_added,
            "alignment": self.alignment,
            "coverage": self.coverage,
            "concerns": self.concerns,
            "voice_fitted": self.voice_fitted,
            "notes": self.notes,
            "render": self.render(),
        }


def analyse(source, *, sensitivity: float = 1.0, protocol_id: str = "free", tuning: str | None = None):
    """Segment a recording and align it, without keeping anything.

    What the UI calls to draw a piano roll of what it heard, before you
    decide whether to learn from it.
    """
    from ..learn import protocols
    from ..learn.segment import segment, summarise
    from ..match.render_offline import load_audio
    from .takes import resolve

    path = resolve(source)
    audio, sample_rate = load_audio(path)
    notes = segment(audio, sample_rate, sensitivity=sensitivity)
    protocol = protocols.get(protocol_id, tuning=tuning)
    alignment = protocols.align(protocol, notes) if protocol.steps else None
    protocols.label(protocol, notes, alignment)
    return {
        "path": str(path),
        "duration": round(len(audio) / sample_rate, 3),
        "sample_rate": sample_rate,
        "notes": [n.to_dict() for n in notes],
        "summary": summarise(notes),
        "alignment": alignment.to_dict() if alignment else None,
        "alignment_text": alignment.describe(protocol) if alignment else None,
    }


def learn(
    profile_id: str,
    source,
    *,
    protocol_id: str = "free",
    frets: int | None = None,
    sensitivity: float = 1.0,
    fit_voice: bool = True,
    progress: Progress | None = None,
) -> LearnReport:
    """Cut a recording into notes and add them to a profile.

    Args:
        source: A take id or an audio path - a DI of the protocol being played.
        protocol_id: What was being played, so notes arrive labelled with
            their string and fret. ``free`` labels by pitch alone.
        fit_voice: Refit the voice model afterwards. A few seconds; worth it
            unless you are about to learn another pass straight away.
    """
    from ..learn import protocols, voice
    from ..learn.profile import SampleNote, classify_dynamics, fingerprint, slice_notes
    from ..learn.segment import segment
    from ..match.render_offline import load_audio, resample, save_audio
    from .takes import digest_of, resolve

    require_writable("learn into a guitar profile")
    profile = get(profile_id)
    directory = _dir(profile.profile_id)
    path = resolve(source)
    digest = digest_of(path)
    for previous in profile.sources:
        if digest and previous.get("digest") == digest:
            raise Conflict(
                f"this recording was already learned into '{profile.profile_id}'",
                hint="Learning it again would double the weight of every note in it.",
            )

    def step(fraction, message):
        if progress:
            progress.check()
            progress.update(fraction, message)

    step(0.05, f"reading {path.name}")
    audio, rate = load_audio(path)
    if rate != profile.sample_rate:
        audio = resample(audio, rate, profile.sample_rate)
        rate = profile.sample_rate

    step(0.2, "finding notes")
    notes = segment(audio, rate, sensitivity=sensitivity)
    protocol = protocols.get(protocol_id, tuning=profile.tuning, frets=frets)
    alignment = protocols.align(protocol, notes) if protocol.steps else None
    protocols.label(protocol, notes, alignment)

    report = LearnReport(
        profile_id=profile.profile_id,
        source=str(source),
        protocol_id=protocol.protocol_id,
        notes_found=len(notes),
    )
    if alignment:
        report.alignment = alignment.describe(protocol)
        report.coverage = round(alignment.coverage, 3)

    # Only notes worth playing back become samples: pitched, unclipped, and
    # for a protocol, ones that answered the step they were meant to.
    wrong = {j for _, j in (alignment.wrong if alignment else [])}
    step_for = {j: protocol.steps[i] for i, j in (alignment.matched if alignment else [])}
    keep = []
    for index, note in enumerate(notes):
        if "clipped" in note.flags:
            continue
        if index in wrong:
            continue
        technique = step_for[index].technique if index in step_for else "pick"
        if technique in ("strum", "harmonic"):
            continue
        if not note.pitched and technique != "palm-mute":
            continue
        keep.append((index, note, technique))

    step(0.45, f"cutting {len(keep)} sample(s)")
    sample_folder = directory / "samples"
    sample_folder.mkdir(parents=True, exist_ok=True)
    cut = dict((id(n), piece) for n, piece in slice_notes(audio, rate, notes))
    existing = len(profile.samples)
    added = []
    for offset, (index, note, technique) in enumerate(keep):
        piece = cut.get(id(note))
        if piece is None:
            continue
        number = existing + offset + 1
        position = f"s{note.string}f{note.fret}" if note.string else "free"
        sample_id = f"{number:04d}-{position}-{note.name.replace('#', 's')}"
        file_name = f"samples/{sample_id}.wav"
        save_audio(directory / file_name, piece, rate)
        sample = SampleNote(
            sample_id=sample_id,
            file=file_name,
            midi=float(note.midi) if note.pitched else float(
                (step_for[index].midi if index in step_for else None) or 40
            ),
            string=note.string,
            fret=note.fret,
            peak_db=note.peak_db,
            rms_db=note.rms_db,
            decay_db_s=note.decay_db_s,
            centroid_hz=note.centroid_hz,
            attack_centroid_hz=note.attack_centroid_hz,
            duration=round(len(piece) / rate, 3),
            technique=technique,
            source=str(source),
            flags=[f for f in note.flags if f not in ("pp", "mf", "ff")],
        )
        asked = step_for.get(index)
        if asked is not None and (asked.dynamic != "mf" or protocol.purpose == "velocity"):
            # The protocol said how hard; that beats inferring it from level.
            sample.dynamic = asked.dynamic
            sample.flags.append("dynamic-labelled")
        added.append(sample)

    profile.samples.extend(added)
    classify_dynamics(profile.samples)
    report.samples_added = len(added)

    step(0.65, "measuring the instrument")
    print_ = fingerprint(audio, rate, notes)
    profile.fingerprint = _merge_fingerprints(profile, print_)
    profile.sources.append(
        {
            "source": str(source),
            "path": str(path),
            "digest": digest,
            "protocol": protocol.protocol_id,
            "notes": len(notes),
            "kept": len(added),
            "coverage": report.coverage,
        }
    )

    report.concerns = _concerns(profile, notes, alignment, protocol)
    report.notes = [n.to_dict() for n in notes]

    if fit_voice and profile.samples:
        step(0.8, "fitting the voice model")
        model = fit_voice_model(profile)
        if model is not None and model.notes:
            model.save(directory / "voice.json")
            profile.has_voice = True
            report.voice_fitted = True

    profile.save(directory)
    step(1.0, f"{len(added)} sample(s) added")
    return report


def _merge_fingerprints(profile, new):
    """Combine a new pass's fingerprint with what the profile already knew.

    Levels and tuning are re-derived from every sample so far; the spectral
    balance is averaged in proportion to how many notes each pass had.
    """
    from ..learn.profile import Fingerprint

    old: Fingerprint = profile.fingerprint
    previous_notes = sum(s.get("notes", 0) for s in profile.sources)
    picks = [s for s in profile.samples if s.technique == "pick"]
    merged = Fingerprint(**{**old.__dict__})
    if old.bands_db and new.bands_db and previous_notes:
        total = previous_notes + 1
        weight = 1 / total
        merged.bands_db = [round(o * (1 - weight) + n * weight, 2) for o, n in zip(old.bands_db, new.bands_db)]
        merged.centroid_hz = round(old.centroid_hz * (1 - weight) + new.centroid_hz * weight, 1)
    else:
        merged.bands_db = new.bands_db
        merged.centroid_hz = new.centroid_hz
    if picks:
        peaks = [s.peak_db for s in picks]
        merged.median_peak_db = round(float(np.median(peaks)), 1)
        merged.p95_peak_db = round(float(np.percentile(peaks, 95)), 1)
        cents = [s.cents for s in picks]
        merged.tuning_cents = round(float(np.median(cents)), 1) if len(cents) >= 3 else new.tuning_cents
    merged.noise_floor_db = new.noise_floor_db if old.noise_floor_db <= -119 else min(old.noise_floor_db, new.noise_floor_db)
    merged.hum_hz = new.hum_hz or old.hum_hz
    merged.hum_db = new.hum_db if new.hum_hz else old.hum_db
    return merged


def _concerns(profile, notes, alignment, protocol) -> list[str]:
    from ..match.pitch import CENTS_TOLERANCE

    concerns = []
    clipped = [n for n in notes if "clipped" in n.flags]
    if clipped:
        concerns.append(
            f"{len(clipped)} note(s) clipped and were left out. Turn the "
            "interface's input gain down a little and record those again - a "
            "clipped DI is distortion before the amp has done anything."
        )
    tuning = profile.fingerprint.tuning_cents
    if tuning is not None and abs(tuning) > CENTS_TOLERANCE:
        direction = "sharp" if tuning > 0 else "flat"
        concerns.append(
            f"the guitar was about {abs(tuning):.0f} cents {direction}. Every "
            "part rendered from this profile will be too - retune and record "
            "again before trusting a comparison against a record."
        )
    # A string much quieter than the others is a pickup height problem, and
    # it changes how hard that string drives an amp.
    by_string: dict[int, list[float]] = {}
    for sample in profile.samples:
        if sample.string and sample.technique == "pick":
            by_string.setdefault(sample.string, []).append(sample.peak_db)
    if len(by_string) >= 3:
        medians = {s: float(np.median(v)) for s, v in by_string.items()}
        overall = float(np.median(list(medians.values())))
        for string, level in sorted(medians.items()):
            if level < overall - 6:
                concerns.append(
                    f"string {string} is {overall - level:.0f} dB quieter than the "
                    "others - often pickup height, and it will drive an amp less hard"
                )
    if alignment and alignment.missing and protocol.steps:
        concerns.append(
            f"{len(alignment.missing)} step(s) of the protocol were not heard; "
            "the sampler will shift neighbouring notes to cover them."
        )
    if profile.fingerprint.hum_hz:
        concerns.append(
            f"{profile.fingerprint.hum_hz:.0f} Hz hum at {profile.fingerprint.hum_db:.0f} "
            "dBFS between notes. A high-gain amp raises that floor a lot; renders "
            "can include it so a gate gets tested honestly."
        )
    return concerns


def fit_voice_model(profile):
    """Fit the harmonic voice model from a profile's picked samples."""
    from ..learn import voice
    from ..match.render_offline import load_audio

    directory = _dir(profile.profile_id)
    material = []
    # One sample per (string, pitch) is enough, and the loudest-but-unclipped
    # carries the most harmonic detail above the noise.
    best: dict[tuple, object] = {}
    for sample in profile.samples:
        if sample.technique != "pick":
            continue
        key = (sample.string, sample.nearest)
        if key not in best or (sample.dynamic == "mf" and best[key].dynamic != "mf"):
            best[key] = sample
    for sample in best.values():
        try:
            audio, rate = load_audio(directory / sample.file)
        except Exception:
            continue
        material.append((audio, sample.midi, sample.string))
    if not material:
        return None
    return voice.fit(material, profile.sample_rate)


def refit_voice(profile_id: str):
    require_writable("fit a voice model")
    profile = get(profile_id)
    model = fit_voice_model(profile)
    if model is None or not model.notes:
        raise Invalid(
            f"'{profile.profile_id}' has no picked samples to fit a voice to",
            hint="Learn a protocol into it first.",
        )
    model.save(_dir(profile.profile_id) / "voice.json")
    profile.has_voice = True
    profile.save(_dir(profile.profile_id))
    return model


def remove_sample(profile_id: str, sample_id: str):
    """Drop one sample - a buzzed fret, a note that rang into the next."""
    require_writable("edit a guitar profile")
    profile = get(profile_id)
    match = next((s for s in profile.samples if s.sample_id == sample_id), None)
    if match is None:
        raise NotFound(f"no sample '{sample_id}' in '{profile.profile_id}'")
    profile.samples.remove(match)
    path = _dir(profile.profile_id) / match.file
    if path.is_file():
        path.unlink()
    profile.save(_dir(profile.profile_id))
    return profile


# --------------------------------------------------------------------------
# playing
# --------------------------------------------------------------------------


def render_events(
    profile_id: str | None,
    events,
    *,
    out_path: str | Path | None = None,
    label: str = "part",
    noise: bool = False,
    use_voice: bool = True,
):
    """Play note events on a profile (or the synthetic string) into a wav."""
    from ..learn.synth import render
    from ..match.render_offline import save_audio

    require_writable("render a part")
    profile = get(profile_id) if profile_id else None
    voice = voice_model(profile.profile_id) if (profile and use_voice) else None
    rendered = render(
        events,
        profile,
        sample_dir=_dir(profile.profile_id) if profile else None,
        voice=voice,
        noise_floor_db=(profile.fingerprint.noise_floor_db if (profile and noise) else None),
    )
    settings = get_settings()
    settings.ensure_workspace()
    destination = Path(out_path) if out_path else settings.audio_dir / "parts" / f"{slugify(label)}-{profile.profile_id if profile else 'synthetic'}.wav"
    path = save_audio(destination, rendered.audio, rendered.sample_rate)
    return path, rendered


def render_text(profile_id: str | None, text: str, **kwargs):
    """Play a typed riff: ``start duration note [velocity]`` per line."""
    from ..learn.synth import parse_events

    events = parse_events(text)
    if not events:
        raise Invalid("no notes in that text", hint="One note per line: 0.0 0.5 E2 0.8")
    return render_events(profile_id, events, **kwargs)


def replay_on(profile_id: str, source, *, label: str | None = None, **kwargs):
    """Play the notes of one recording on a different guitar.

    Segment a DI you made on one instrument, then render the same notes at
    the same moments on another profile. That turns "which guitar" from a
    re-recording session into a button.
    """
    from ..learn.segment import segment
    from ..learn.synth import events_from_notes
    from ..match.render_offline import load_audio
    from .takes import resolve

    path = resolve(source)
    audio, rate = load_audio(path)
    events = events_from_notes(segment(audio, rate))
    if not events:
        raise Invalid(f"no pitched notes found in {path.name}")
    return render_events(profile_id, events, label=label or path.stem, **kwargs)


def preview_note(profile_id: str | None, midi: float, *, string: int | None = None, velocity: float = 0.75):
    """One note, for clicking a fret on the drawn fretboard and hearing it."""
    from ..learn.synth import NoteEvent

    return render_events(
        profile_id,
        [NoteEvent(0.0, 1.6, float(midi), velocity, string)],
        label=f"note-{int(round(midi))}-s{string or 0}",
        out_path=None,
    )


def click_track(protocol_id: str, *, tuning: str | None = None, frets: int | None = None, count_in: int = 4):
    """A metronome for a protocol: count-in, then one click per step.

    Recording to a click is what lets the aligner be strict about timing as
    well as pitch, and it keeps a long chromatic run from drifting into a
    hurry halfway up the neck.
    """
    from ..learn import protocols
    from ..match.render_offline import save_audio

    protocol = protocols.get(protocol_id, tuning=tuning, frets=frets)
    if not protocol.steps:
        raise Invalid("the free protocol has no steps to click")
    rate = 48000
    beat = protocol.spacing_s
    total = (count_in + len(protocol.steps) + 2) * beat
    out = np.zeros(int(total * rate))
    t = np.arange(int(0.03 * rate)) / rate
    high = np.sin(2 * np.pi * 1500 * t) * np.exp(-t / 0.008) * 0.5
    low = np.sin(2 * np.pi * 1000 * t) * np.exp(-t / 0.008) * 0.35
    for i in range(count_in + len(protocol.steps)):
        click = high if i < count_in or (i - count_in) % 4 == 0 else low
        first = int(i * beat * rate)
        out[first : first + len(click)] += click
    settings = get_settings()
    settings.ensure_workspace()
    path = save_audio(settings.audio_dir / "clicks" / f"{protocol.protocol_id}-{slugify(protocol.tuning)}.wav", out, rate)
    return path, protocol, count_in * beat


def demo_take(protocol_id: str = "quick", *, tuning: str | None = None, pickup: str = "bridge", seed: int = 0):
    """A synthetic recording of a protocol being played, for trying the flow.

    Rendered on the plucked-string model at the protocol's own spacing, with
    a little human looseness in timing and level. It exists so the learning
    flow can be walked end to end before a guitar is plugged in - and every
    surface that shows it says it was synthesised.
    """
    from ..learn import protocols
    from ..learn.pluck import render_notes
    from ..match.render_offline import save_audio

    protocol = protocols.get(protocol_id, tuning=tuning)
    rng = np.random.default_rng(seed)
    notes = []
    start = 1.0
    for step in protocol.steps:
        if step.midi is None:
            start += protocol.spacing_s
            continue
        level = {"pp": 0.35, "mf": 0.7, "ff": 0.95}.get(step.dynamic, 0.7)
        notes.append(
            (
                start + float(rng.normal(0, 0.02)),
                protocol.spacing_s * 0.85,
                step.midi + float(rng.normal(0, 0.03)),
                float(np.clip(level + rng.normal(0, 0.05), 0.1, 1.0)),
            )
        )
        start += protocol.spacing_s
    audio = render_notes(notes, pickup=pickup, seed=seed)
    audio += rng.normal(0, 10 ** (-78 / 20), len(audio))
    settings = get_settings()
    settings.ensure_workspace()
    path = save_audio(settings.audio_dir / "demo" / f"demo-{protocol.protocol_id}-{pickup}.wav", audio, 48000)
    return path, protocol


def render_list(profiles) -> str:
    if not profiles:
        return (
            "No guitar profiles yet.\n\n"
            "A profile is your guitar, learned: play a guided sequence once and "
            "any part can then be played on it - including a song's part "
            "transcribed from the record.\n"
            "  rigsmith learn new 'Les Paul bridge' --guitar 'Les Paul' --pickup bridge\n"
            "  rigsmith learn guide quick        # what to play\n"
            "  rigsmith learn from les-paul-bridge my-di.wav --protocol quick"
        )
    lines = [f"{len(profiles)} profile(s):"]
    for profile in profiles:
        lines.append(profile.describe())
    return "\n".join(lines)

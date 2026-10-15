"""A song's part, played on your guitar.

This is where learning meets the record. Give it a learned profile and any
description of the part - a tab's MIDI export, notes typed by hand, or just
the isolated stem - and it renders the DI your guitar would have produced
playing that part, *on the record's clock*: time zero of the DI is the start
of the region you chose, every note where the record has it.

That last property is what makes it more than a convenience. A candidate rig
rendered from this DI plays the same notes as the stem at the same moments,
so it can be compared frame against frame rather than only as a distribution
- see :func:`rigsmith.match.metrics.performance_distance`. Against a stem the
sample-aligned metrics still do not apply (the record's guitar is not your
guitar, and its amp is not your rig), but "is the chug at 1:02 as bright as
the record's chug at 1:02" is a question this can answer and nothing before
it could.

Where the notes come from is recorded on the result, in order of trust:

1. ``midi`` - a tab's notes, aligned to the record. The right notes.
2. ``text`` - notes typed in. As right as whoever typed them.
3. ``transcribed`` - estimated from the stem. Good for single-note parts,
   honest about chords, and says how much of the stem it explains.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..config import get_settings, require_writable
from .errors import Invalid
from .jobs import Progress


@dataclass
class PartResult:
    """A rendered part and an honest account of how it was made."""

    path: Path
    profile_id: str | None
    source: str
    """midi, text or transcribed."""
    region: str | None = None
    events: int = 0
    confidence: float | None = None
    method: str | None = None
    alignment: str | None = None
    rendered: str = ""
    warnings: list[str] = field(default_factory=list)
    take_id: str | None = None
    midi_path: Path | None = None
    events_path: Path | None = None

    def render(self) -> str:
        lines = [
            f"part DI: {self.path}",
            f"  notes from {self.source}"
            + (f" ({self.method})" if self.method else "")
            + (f", confidence {self.confidence:.0%}" if self.confidence is not None else ""),
            f"  played on {self.profile_id or 'the synthetic string'}",
        ]
        if self.region:
            lines.append(f"  covers {self.region} of the record; DI time zero is its start")
        if self.alignment:
            lines.append(f"  {self.alignment}")
        lines.extend("  " + line for line in self.rendered.splitlines())
        if self.take_id:
            lines.append(f"  filed as take '{self.take_id}'")
        if self.midi_path:
            lines.append(f"  notes saved as {self.midi_path.name} - open it in a tab editor to correct them")
        lines.extend(f"  note: {w}" for w in self.warnings)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "profile_id": self.profile_id,
            "source": self.source,
            "region": self.region,
            "events": self.events,
            "confidence": self.confidence,
            "method": self.method,
            "alignment": self.alignment,
            "rendered": self.rendered,
            "warnings": self.warnings,
            "take_id": self.take_id,
            "midi_path": str(self.midi_path) if self.midi_path else None,
            "events_path": str(self.events_path) if self.events_path else None,
            "render": self.render(),
        }


def _parts_dir() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    directory = settings.audio_dir / "parts"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def part_di(
    profile_id: str | None,
    *,
    stem=None,
    midi=None,
    track: int | None = None,
    text: str | None = None,
    region=None,
    mode: str = "auto",
    lowest: str = "auto",
    power_chords: bool = False,
    transpose: int = 0,
    label: str | None = None,
    noise: bool = False,
    file_as_take: bool = False,
    like: str | None = None,
    amount: float = 1.0,
    progress: Progress | None = None,
) -> PartResult:
    """Render a song's part as a DI on a learned guitar.

    Args:
        stem: The isolated guitar (or the mix, at a pinch). Required for
            transcription; used for alignment when MIDI is given.
        midi: A MIDI file of the part - usually a tab editor's export.
        track: Which MIDI track; by default the busiest guitar track.
        text: Notes typed as ``start duration note [velocity]`` lines.
        region: ``start:end`` of the record the part covers, or ``auto``.
        power_chords: Put fifths and octaves back on a transcribed rhythm
            part, for when the record is known to be power chords.
        file_as_take: Add the result to the takes library as a synthetic DI,
            so its id works anywhere a filename does.
        like: Move the DI toward another guitar's pickup - a learned
            guitar's id, or a type such as ``strat`` or ``p90`` - by
            ``amount`` (0-1). For a record made on a different guitar.
    """
    from ..learn import midifile
    from ..learn.align import align_events
    from ..learn.synth import parse_events
    from ..learn.transcribe import power_chords as expand_power_chords
    from ..learn.transcribe import transcribe
    from ..match.render_offline import load_audio
    from . import profiles, takes
    from .matching import audio_path, resolve_region

    require_writable("render a part")
    if midi is None and text is None and stem is None:
        raise Invalid(
            "nothing to take the notes from",
            hint="Give a stem to transcribe, a MIDI file of the part, or typed notes.",
        )

    def step(fraction, message):
        if progress:
            progress.check()
            progress.update(fraction, message)

    stem_audio = None
    sample_rate = 48000
    span = None
    if stem is not None:
        stem_path = audio_path(stem)
        stem_audio, sample_rate = load_audio(stem_path)
        span = resolve_region(region, stem_path)
        if span:
            stem_audio = span.slice(stem_audio, sample_rate, label="the stem")

    result_source = "transcribed"
    method = confidence = alignment_text = None
    warnings: list[str] = []
    if midi is not None:
        step(0.1, "reading the MIDI file")
        tracks = midifile.read(Path(midi))
        events = midifile.to_events(tracks, track=track, transpose=transpose)
        if not events:
            raise Invalid("that MIDI track has no notes", hint="\n".join(t.describe() for t in tracks))
        result_source = "midi"
        if stem_audio is not None:
            step(0.3, "aligning the tab to the record")
            aligned = align_events(events, stem_audio, sample_rate)
            events = aligned.events
            alignment_text = aligned.describe()
            if aligned.similarity_after < 0.45:
                warnings.append(
                    "the tab and the record agree poorly even after alignment - "
                    "check it is the right track, and the right key (transpose)"
                )
    elif text is not None:
        events = parse_events(text)
        result_source = "text"
    else:
        step(0.1, "transcribing the stem")
        transcription = transcribe(stem_audio, sample_rate, path=None, mode=mode, lowest=lowest)
        events = transcription.events
        method = f"{transcription.method}, {transcription.mode}"
        confidence = round(transcription.confidence, 3)
        warnings.extend(transcription.warnings)
        if power_chords:
            events = expand_power_chords(events)
    if transpose and midi is None:
        for event in events:
            event.midi += transpose

    step(0.6, f"playing {len(events)} note(s)")
    name = label or (Path(str(stem)).stem if stem is not None else "part")
    profile_label = profile_id or "synthetic"
    directory = _parts_dir()
    out = directory / f"{profiles.slugify(name)}-{profiles.slugify(profile_label)}.wav"
    path, rendered = profiles.render_events(profile_id, events, out_path=out, label=name, noise=noise, like=like, amount=amount)

    # Keep the notes beside the audio - as JSON for rigsmith, as MIDI for a
    # person with a tab editor who wants to fix the two wrong ones.
    events_path = out.with_suffix(".notes.json")
    events_path.write_text(
        json.dumps(
            {
                "source": result_source,
                "profile_id": profile_id,
                "region": span.describe() if span else None,
                "region_start": span.start if span else 0.0,
                "events": [e.to_dict() for e in events],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    midi_path = midifile.write(out.with_suffix(".mid"), events)

    result = PartResult(
        path=path,
        profile_id=profile_id,
        source=result_source,
        region=span.describe() if span else None,
        events=len(events),
        confidence=confidence,
        method=method,
        alignment=alignment_text,
        rendered=rendered.describe(),
        warnings=warnings + list(rendered.warnings),
        midi_path=midi_path,
        events_path=events_path,
    )

    if file_as_take:
        step(0.9, "filing it as a take")
        profile = profiles.get(profile_id) if profile_id else None
        take_id = f"{profiles.slugify(name)}-{profiles.slugify(profile_label)}-part"
        existing = {t.take_id for t in takes.list_takes()}
        if take_id in existing:
            takes.delete(take_id)
        take = takes.add(
            path,
            take_id=take_id,
            kind="di",
            origin="synth",
            guitar=(profile.guitar if profile else None),
            pickup=(profile.pickup if profile else None),
            tuning=(profile.tuning if profile else None),
            note=f"{name}: {result_source} notes played on {profile_label}",
            tags=["part", result_source],
            copy=False,
        )
        result.take_id = take.take_id
    step(1.0, "done")
    return result

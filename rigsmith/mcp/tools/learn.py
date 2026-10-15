"""Tools for learning the user's guitar and playing parts on it."""

from __future__ import annotations

from typing import Literal

from ...core import parts, profiles
from ...core.jobs import get_manager
from .._shared import tool

Protocol = Literal["quick", "chromatic", "dynamics", "techniques", "free"]


@tool
def list_guitar_profiles() -> str:
    """List the guitars rigsmith has learned from the user's playing.

    A profile is a guitar (and pickup) learned from a guided recording: its
    notes as samples, plus a fingerprint of level, brightness, hum and tuning.
    With one, any part can be rendered as a DI on *their* guitar.
    """
    return profiles.render_list(profiles.list_profiles())


@tool
def show_guitar_profile(profile_id: str) -> str:
    """One profile: how much of the neck it covers, and what it measured.

    Relay the concerns - an out-of-tune guitar or one quiet string changes
    every comparison made through it.
    """
    profile = profiles.get(profile_id)
    lines = [profile.describe()]
    grid = profile.coverage(frets=12)
    lines.append(f"  {grid['filled']}/{grid['positions']} string/fret positions sampled to fret 12")
    lines.append(f"  dynamics: {profile.dynamics()}")
    return "\n".join(lines)


@tool
def create_guitar_profile(
    name: str,
    guitar: str | None = None,
    pickup: str | None = None,
    tuning: str | None = None,
    note: str | None = None,
) -> str:
    """Start an empty profile for one guitar and pickup.

    Ask which guitar and pickup rather than guessing - the label is what
    makes a profile reusable later.
    """
    profile = profiles.create(name, guitar=guitar, pickup=pickup, tuning=tuning, note=note)
    return profile.describe() + "\n\nNext: show the user a protocol with protocol_steps."


@tool
def protocol_steps(protocol_id: Protocol = "quick", tuning: str | None = None, frets: int | None = None) -> str:
    """What the user should play to teach rigsmith their guitar.

    'quick' is four notes a string (a minute); 'chromatic' is every fret of
    every string (the most accurate); 'dynamics' adds soft and hard picking;
    'techniques' adds mutes and chords for checking gain staging.
    """
    from ...learn import protocols

    protocol = protocols.get(protocol_id, tuning=tuning, frets=frets)
    lines = [protocol.describe(), f"tuning {protocol.tuning}, one step every {protocol.spacing_s}s", ""]
    lines.extend(f"{i}. {step.prompt}" for i, step in enumerate(protocol.steps, start=1))
    return "\n".join(lines)


@tool
def learn_guitar(
    profile_id: str,
    source: str,
    protocol_id: Protocol = "free",
    frets: int | None = None,
) -> str:
    """Learn a recording of the user playing into a profile.

    ``source`` is a take id or a path to a DI of the protocol being played.
    Runs in the background; returns a job id to poll with job_status.
    """
    job = get_manager().submit(
        f"learn {source} into {profile_id}",
        lambda progress: profiles.learn(
            profile_id, source, protocol_id=protocol_id, frets=frets, progress=progress
        ),
    )
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def play_notes(profile_id: str, notes: str, label: str = "riff") -> str:
    """Render typed notes on a learned guitar into a DI wav.

    ``notes`` is one note per line: ``start duration note [velocity]``, e.g.
    ``0 0.5 E2 0.8``. Use profile 'synthetic' when nothing is learned yet.
    """
    path, rendered = profiles.render_text(
        None if profile_id == "synthetic" else profile_id, notes, label=label
    )
    return f"{path}\n{rendered.describe()}"


@tool
def render_part(
    profile_id: str,
    stem: str | None = None,
    midi_path: str | None = None,
    midi_track: int | None = None,
    notes: str | None = None,
    region: str | None = None,
    power_chords: bool = False,
    transpose: int = 0,
    label: str | None = None,
    file_as_take: bool = True,
) -> str:
    """Render a song's part as a DI on the user's guitar, on the record's clock.

    Give a ``midi_path`` (a tab editor's export - the right notes) and the
    ``stem`` to align it to; or just the ``stem`` to transcribe; or typed
    ``notes``. The DI's time zero is the region's start, so candidates
    rendered from it can be scored with mode='performance' against the same
    region of the stem. Runs in the background; returns a job id.

    Prefer a tab's MIDI for chord-heavy distorted parts - transcription is
    reliable on single-note parts and approximate on distorted chords, and
    the result says how much of the stem its notes explain.
    """
    job = get_manager().submit(
        f"render part {label or stem or midi_path}",
        lambda progress: parts.part_di(
            None if profile_id == "synthetic" else profile_id,
            stem=stem,
            midi=midi_path,
            track=midi_track,
            text=notes,
            region=region,
            power_chords=power_chords,
            transpose=transpose,
            label=label,
            file_as_take=file_as_take,
            progress=progress,
        ),
    )
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def transcribe_stem(stem: str, region: str | None = None, mode: Literal["auto", "mono", "poly"] = "auto") -> str:
    """Estimate the notes of a guitar stem: what sounds when.

    Reports how much of the stem's pitched energy the notes explain. Below
    about 60%, check the notes or ask the user for a tab's MIDI.
    """
    from ...core.matching import audio_path, resolve_region
    from ...learn.transcribe import describe_events, transcribe
    from ...match.render_offline import load_audio

    path = audio_path(stem)
    audio, rate = load_audio(path)
    span = resolve_region(region, path)
    if span:
        audio = span.slice(audio, rate, label="the stem")
    result = transcribe(audio, rate, mode=mode)
    return result.describe() + "\n\n" + describe_events(result.events)

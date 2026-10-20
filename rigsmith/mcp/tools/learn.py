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
def play_notes(profile_id: str, notes: str, label: str = "riff", like: str | None = None, amount: float = 1.0) -> str:
    """Render typed notes on a learned guitar into a DI wav.

    ``notes`` is either the quick form - ``@140 E2 G2 A2:4 (E2+B2)x2 -``,
    eighths unless ``:4``/``:16``/``:8.``, ``+`` a chord, ``-`` a rest, ``x2``
    a repeat, ``!`` an accent - or one ``start duration note [velocity]`` per
    line. Use profile 'synthetic' when nothing is learned yet. ``like`` moves
    the result toward another pickup (see swap_pickup) by ``amount``.
    """
    path, rendered = profiles.render_text(
        None if profile_id == "synthetic" else profile_id, notes, label=label, like=like, amount=amount
    )
    return f"{path}\n{rendered.describe()}"


@tool
def estimate_pickup(source: str) -> str:
    """Where a guitar's pickup resonates - a learned guitar's id, a take id, or a file.

    The resonant peak of pickup plus cable (around 4-5 kHz and sharp for a
    single coil, 2-3 kHz and broad for a humbucker) is most of what makes a
    clean DI sound like one guitar rather than another.
    """
    known = {p.profile_id for p in profiles.list_profiles()}
    resonance = profiles.resonance_of(source) if source in known else profiles.pickup_of(source)
    return resonance.describe()


@tool
def swap_pickup(
    source: str,
    like: str,
    amount: float = 1.0,
    profile_id: str | None = None,
    file_as_take: bool = True,
) -> str:
    """Move a DI toward another guitar's pickup, for a record made on a different guitar.

    ``like`` is a learned guitar's id or a type: strat, tele, p90, paf,
    hot-humbucker, active, filtertron - a recipe's description such as
    "Fender Stratocaster" or "EMG 81" works too. ``amount`` 0-1 goes part
    way. Give ``profile_id`` when the DI was played on a learned guitar; its
    pickup is then measured rather than estimated. The result stays
    sample-aligned with the source, so performance-mode scoring still works.
    """
    result = profiles.swap_di(source, like, amount=amount, profile_id=profile_id, file_as_take=file_as_take)
    return f"{result['path']}\n  {result['pickup']}" + (f"\n  take {result['take_id']}" if result["take_id"] else "")


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
    like: str | None = None,
    amount: float = 1.0,
    mode: Literal["auto", "mono", "poly", "arpeggio"] = "auto",
) -> str:
    """Render a song's part as a DI on the user's guitar, on the record's clock.

    Give a ``midi_path`` (a tab editor's export - the right notes) and the
    ``stem`` to align it to; or just the ``stem`` to transcribe; or typed
    ``notes``. The DI's time zero is the region's start, so candidates
    rendered from it can be scored with mode='performance' against the same
    region of the stem. Runs in the background; returns a job id.

    Prefer a tab's MIDI for chord-heavy distorted parts - transcription is
    reliable on single-note parts and arpeggios and approximate on distorted
    chords, and the result says how much of the stem its notes explain.
    ``mode`` overrides how the stem is read when the result says it guessed
    wrong (``arpeggio``: one note per pick, left to ring). ``like`` moves
    the DI toward the record's guitar when it was not the user's (a type such
    as strat or p90, or another learned guitar), by ``amount``.
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
            like=like,
            amount=amount,
            mode=mode,
            progress=progress,
        ),
    )
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def transcribe_stem(stem: str, region: str | None = None, mode: Literal["auto", "mono", "poly", "arpeggio"] = "auto") -> str:
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

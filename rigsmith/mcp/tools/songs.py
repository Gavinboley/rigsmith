"""Tools for songs: find one, map its parts, pick what to chase."""

from __future__ import annotations

from typing import Literal

from ...core import songs
from ...core.jobs import get_manager
from .._shared import tool


@tool
def list_songs() -> str:
    """Songs in the workspace, with how many parts each has mapped and chosen."""
    return songs.render_list(songs.list_songs())


@tool
def search_song(query: str) -> str:
    """Look a song up online: title, artist, album, year (accuracy mode only).

    For metadata and research leads - it does not fetch the song. The user's
    own copy is what gets mapped and matched.
    """
    found = songs.lookup(query)
    return "\n".join(m.describe() for m in found) or "Nothing found."


@tool
def create_song(
    title: str,
    artist: str | None = None,
    mix_path: str | None = None,
    stem_path: str | None = None,
    lookup: bool = False,
) -> str:
    """Start a song from the user's own audio of it.

    ``mix_path`` is their copy of the record; ``stem_path`` an isolated guitar
    if they have one. ``lookup`` fills in album, year and credits online.
    """
    song = songs.create(title, artist=artist, mix=mix_path, stem=stem_path)
    if lookup:
        song = songs.enrich(song.song_id)
    return song.describe() + f"\n\nNext: analyse_song('{song.song_id}')"


@tool
def analyse_song(song_id: str) -> str:
    """Map a song: sections, guitar layers (double-tracked, centred lead...),
    and a tone sketch for every section x layer part. Background job."""
    job = get_manager().submit(f"map {song_id}", lambda progress: songs.analyse(song_id, progress=progress))
    return f"Started job {job.id}. Check it with job_status('{job.id}'), then show_song."


@tool
def isolate_song_guitar(song_id: str) -> str:
    """Separate the guitar stem from a song's mix with Demucs. Background job."""
    job = get_manager().submit(f"isolate {song_id}", lambda progress: songs.isolate(song_id, progress=progress))
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def show_song(song_id: str) -> str:
    """A song's map: sections, layers, every part with its sketch and best result."""
    return songs.get(song_id).describe()


@tool
def choose_parts(song_id: str, part_ids: list[str] | None = None, only: bool = False, one_per_sound: bool = False) -> str:
    """Pick the parts to chase. Ask the user which parts they care about -
    the solo, the verse riff - rather than choosing for them. ``one_per_sound``
    chooses a part for each sound the song has (a clean intro, a driven
    chorus, a lead) and only those - for a rig with a scene per sound."""
    if one_per_sound:
        song = songs.choose_sounds(song_id)
    else:
        song = songs.choose(song_id, list(part_ids or []), only=only)
    return f"{len(song.chosen())} chosen: " + ", ".join(p.part_id for p in song.chosen())


@tool
def song_brief(song_id: str, part_id: str | None = None) -> str:
    """What is known about a song or one part - the starting point for gear
    research. Includes measured tone sketches, so research can be checked
    against the sound."""
    return songs.brief(song_id, part_id)


@tool
def part_target(song_id: str, part_id: str) -> str:
    """Cut a part's audio out (its own layer, its own seconds) as a target
    to score candidates against."""
    return str(songs.part_audio(song_id, part_id))


@tool
def render_song_part_di(
    song_id: str,
    part_id: str,
    profile_id: str,
    midi_path: str | None = None,
    midi_track: int | None = None,
    power_chords: bool = False,
    like: str | None = None,
    amount: float = 1.0,
    reading: Literal["auto", "mono", "poly", "arpeggio"] | None = None,
) -> str:
    """Render one part as a DI on the user's guitar, on the part's clock.

    Then candidates rendered from it can be scored against part_target in
    mode='performance'. ``like`` moves it toward the record's guitar (strat,
    p90, paf, ... or a learned guitar) by ``amount``. ``reading`` corrects
    how the stem is read into notes when the result says it guessed wrong
    (``arpeggio``: one note per pick, left to ring); the part keeps it for
    later renders. Background job.
    """
    job = get_manager().submit(
        f"render {part_id} on {profile_id}",
        lambda progress: songs.render_part_di(
            song_id, part_id, None if profile_id == "synthetic" else profile_id,
            midi=midi_path, track=midi_track, power_chords=power_chords,
            like=like, amount=amount, reading=reading, progress=progress,
        ),
    )
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def song_part_notes(song_id: str, part_id: str) -> str:
    """The notes a part's rendered DI plays: how they were heard, the range,
    and the first forty. Check them before tuning against the DI - a lead
    read an octave out, or chords where the record picks an arpeggio, is
    fixed by rendering again with the right ``reading``, not by the rig."""
    from ...learn.instrument import midi_to_note
    from ...learn.synth import NoteEvent
    from ...learn.transcribe import describe_events

    notes = songs.part_notes(song_id, part_id)
    events = [NoteEvent.from_dict(e) for e in notes["events"]]
    if not events:
        return f"{notes['label']}: the DI plays no notes"
    low, high = min(e.midi for e in events), max(e.midi for e in events)
    heard = f", heard as {notes['heard_as']}" if notes.get("heard_as") else ""
    head = f"{notes['label']}: {len(events)} notes from {notes['source']}{heard}, {midi_to_note(low)} to {midi_to_note(high)}"
    return head + "\n" + describe_events(events)


@tool
def rename_song_section(song_id: str, index: int, name: str) -> str:
    """Correct a section's guessed name (the map's names are a guess)."""
    songs.rename_section(song_id, index, name)
    return f"section {index} is now '{name}'"


@tool
def add_song_audio(song_id: str, mix_path: str | None = None, stem_path: str | None = None) -> str:
    """Give an existing song its audio: the user's copy of the record (mix)
    and/or an isolated guitar stem. Map it again afterwards with analyse_song."""
    if not mix_path and not stem_path:
        return "give a mix_path, a stem_path, or both"
    song = songs.attach(song_id, mix=mix_path, stem=stem_path)
    return song.describe()


@tool
def look_up_song(song_id: str) -> str:
    """Fill in a song's album, year, artwork and recording credits (who played
    guitar) from the online lookup. Never fetches the audio. Accuracy mode only."""
    return songs.enrich(song_id).describe()


@tool
def align_take_to_part(song_id: str, part_id: str, take: str) -> str:
    """Put the user's own recording of a part (a take id or an audio path),
    played along with the record or to a click at its tempo, on the part's
    clock - one offset and one tempo ratio - and make it the part's DI, so
    tuning compares it frame by frame. A take that does not follow the record
    is refused with the reason."""
    result = songs.take_for_part(song_id, part_id, take)
    text = ("Aligned: " if result["aligned"] else "Not aligned: ") + result["describe"]
    if result.get("take_id"):
        text += f"\nfiled as take {result['take_id']}"
    return text


@tool
def set_song_tempo(song_id: str, bpm: float | None = None, factor: float | None = None) -> str:
    """Correct a song's tempo: an exact bpm, or factor 0.5/2 to halve or
    double the detected one (its known mistake is an octave). Re-times every
    part's delay note value; drafts built after it carry the new tempo."""
    song = songs.set_tempo(song_id, bpm, factor=factor)
    return f"{song.title}: {song.structure['tempo']:g} BPM (set by hand)"


@tool
def find_song_file(title: str, artist: str | None = None) -> str:
    """Find the user's own copy of a song in their music library, by name.

    Local only - nothing leaves the machine. Use the path it returns as the
    song's mix (create_song / run_autopilot) instead of asking for a file.
    """
    from ...core import songs as song_core

    matches = song_core.find_local(title, artist)
    if not matches:
        roots = ", ".join(str(r) for r in song_core.music_dirs()) or "no music folder found"
        return f"nothing in the library matches '{title}' (looked in {roots})"
    return "\n".join(f"{m['path']}  (score {m['score']}{', artist matches' if m['artist_match'] else ''})" for m in matches)


@tool
def setup_status() -> str:
    """What the user has set up and what is still missing, with the next step.

    Their guitar learned, a song added, the Core paired, the hardware loop
    wired and calibrated, and a model to answer. Ask this before suggesting
    something that needs a missing piece - and to answer "what should I do
    next?".
    """
    from ...core import setup

    return setup.render(setup.checklist())


@tool
def app_mode() -> str:
    """Whether rigsmith is in accuracy mode (network and Claude allowed) or
    local mode (nothing leaves the machine). In local mode, online lookups
    and TONE3000 refuse - work from what is in the workspace."""
    from ...core import appsettings

    settings = appsettings.load()
    return f"{settings.mode}: {appsettings.MODES[settings.mode]}"

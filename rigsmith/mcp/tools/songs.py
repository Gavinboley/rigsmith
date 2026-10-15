"""Tools for songs: find one, map its parts, pick what to chase."""

from __future__ import annotations

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
def choose_parts(song_id: str, part_ids: list[str], only: bool = False) -> str:
    """Pick the parts to chase. Ask the user which parts they care about -
    the solo, the verse riff - rather than choosing for them."""
    song = songs.choose(song_id, part_ids, only=only)
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
) -> str:
    """Render one part as a DI on the user's guitar, on the part's clock.

    Then candidates rendered from it can be scored against part_target in
    mode='performance'. ``like`` moves it toward the record's guitar (strat,
    p90, paf, ... or a learned guitar) by ``amount``. Background job.
    """
    job = get_manager().submit(
        f"render {part_id} on {profile_id}",
        lambda progress: songs.render_part_di(
            song_id, part_id, None if profile_id == "synthetic" else profile_id,
            midi=midi_path, track=midi_track, power_chords=power_chords,
            like=like, amount=amount, progress=progress,
        ),
    )
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def rename_song_section(song_id: str, index: int, name: str) -> str:
    """Correct a section's guessed name (the map's names are a guess)."""
    songs.rename_section(song_id, index, name)
    return f"section {index} is now '{name}'"


@tool
def app_mode() -> str:
    """Whether rigsmith is in accuracy mode (network and Claude allowed) or
    local mode (nothing leaves the machine). In local mode, online lookups
    and TONE3000 refuse - work from what is in the workspace."""
    from ...core import appsettings

    settings = appsettings.load()
    return f"{settings.mode}: {appsettings.MODES[settings.mode]}"

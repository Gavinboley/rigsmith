"""``rigsmith song`` - map a record into the parts you can chase."""

from __future__ import annotations

import click

from ..clikit import _emit, handled
from . import progress_bar


@click.group("song")
def group() -> None:
    """Songs: find one, map its sections and guitar layers, pick the parts."""


@group.command("list")
@click.option("--json", "as_json", is_flag=True)
@handled
def song_list(as_json: bool) -> None:
    """Every song in the workspace."""
    from ..core import songs

    found = songs.list_songs()
    _emit(songs.render_list(found), as_json=as_json, payload=[s.to_dict() for s in found])


@group.command("search")
@click.argument("query")
@handled
def song_search(query: str) -> None:
    """Look a song up online: album, year, a preview (accuracy mode only)."""
    from ..core import songs

    for match in songs.lookup(query):
        click.echo(f"  {match.describe()}")


@group.command("new")
@click.argument("title")
@click.option("--artist")
@click.option("--album")
@click.option("--year", type=int)
@click.option("--mix", help="Your copy of the song (path or take id).")
@click.option("--stem", help="An isolated guitar stem, if you have one.")
@click.option("--link", is_flag=True, help="Point at the files instead of copying them in.")
@click.option("--lookup", is_flag=True, help="Fill in album, year and credits online.")
@handled
def song_new(title, artist, album, year, mix, stem, link, lookup) -> None:
    """Start a song."""
    from ..core import songs

    song = songs.create(title, artist=artist, album=album, year=year, mix=mix, stem=stem, copy=not link)
    if lookup:
        song = songs.enrich(song.song_id)
    click.echo(song.describe())
    click.echo(f"\nNext:  rigsmith song analyse {song.song_id}")


@group.command("add-audio")
@click.argument("song_id")
@click.option("--mix")
@click.option("--stem")
@click.option("--link", is_flag=True)
@handled
def song_add_audio(song_id, mix, stem, link) -> None:
    """Give a song its mix or its guitar stem."""
    from ..core import songs

    click.echo(songs.attach(song_id, mix=mix, stem=stem, copy=not link).describe())


@group.command("lookup")
@click.argument("song_id")
@handled
def song_lookup(song_id) -> None:
    """Fill in metadata, artwork and recording credits online."""
    from ..core import songs

    song = songs.enrich(song_id)
    click.echo(song.describe())
    guitarists = (song.credits or {}).get("guitarists") or []
    for person in guitarists:
        click.echo(f"  guitar: {person['name']} ({', '.join(person['instruments'])})")


@group.command("preview")
@click.argument("song_id")
@handled
def song_preview(song_id) -> None:
    """Use the store's 30-second preview as the mix (to explore before you have the file)."""
    from ..core import songs

    click.echo(songs.preview(song_id).describe())


@group.command("isolate")
@click.argument("song_id")
@handled
def song_isolate(song_id) -> None:
    """Separate the guitar stem from the mix (slow; needs Demucs)."""
    from ..core import songs

    with progress_bar("separating") as bar:
        song = songs.isolate(song_id, progress=bar)
    click.echo(song.describe())


@group.command("analyse")
@click.argument("song_id")
@handled
def song_analyse(song_id) -> None:
    """Map sections, guitar layers, and a tone sketch for every part."""
    from ..core import songs

    with progress_bar("mapping") as bar:
        song = songs.analyse(song_id, progress=bar)
    click.echo(song.describe())


@group.command("show")
@click.argument("song_id")
@click.option("--json", "as_json", is_flag=True)
@handled
def song_show(song_id, as_json) -> None:
    """A song's map."""
    from ..core import songs

    song = songs.get(song_id)
    _emit(song.describe(), as_json=as_json, payload=song.to_dict())


@group.command("part")
@click.argument("song_id")
@click.argument("part_id")
@handled
def song_part(song_id, part_id) -> None:
    """One part: what it measures like, and what that suggests."""
    from ..core import songs
    from ..song.describe import ToneSketch

    song = songs.get(song_id)
    part = song.part(part_id)
    click.echo(f"{part.label}  {part.region}s  presence {part.presence:.0%}")
    click.echo(ToneSketch.from_dict(part.sketch).describe())
    for entry in part.history[-5:]:
        click.echo(f"  tried {entry['label']}: {entry['composite']:.4f} ({entry['mode']})")


@group.command("choose")
@click.argument("song_id")
@click.argument("part_ids", nargs=-1, required=True)
@click.option("--only", is_flag=True, help="Un-choose everything else.")
@click.option("--drop", is_flag=True, help="Un-choose these instead.")
@handled
def song_choose(song_id, part_ids, only, drop) -> None:
    """Pick the parts to chase."""
    from ..core import songs

    song = songs.choose(song_id, list(part_ids), chosen=not drop, only=only)
    click.echo(f"{len(song.chosen())} part(s) chosen: " + ", ".join(p.part_id for p in song.chosen()))


@group.command("rename-section")
@click.argument("song_id")
@click.argument("index", type=int)
@click.argument("name")
@handled
def song_rename_section(song_id, index, name) -> None:
    """Correct a section's guessed name (verse, chorus, solo...)."""
    from ..core import songs

    songs.rename_section(song_id, index, name)
    click.echo(f"section {index} is now '{name}'")


@group.command("target")
@click.argument("song_id")
@click.argument("part_id")
@handled
def song_target(song_id, part_id) -> None:
    """Cut a part's audio out, to score candidates against."""
    from ..core import songs

    click.echo(songs.part_audio(song_id, part_id))


@group.command("di")
@click.argument("song_id")
@click.argument("part_id")
@click.argument("profile_id")
@click.option("--midi", "midi_path", help="A tab's MIDI export for this part.")
@click.option("--track", type=int)
@click.option("--notes", "text")
@click.option("--power-chords", is_flag=True)
@click.option("--like", help="The record's guitar, when it was not yours: strat, p90, paf, a learned guitar...")
@click.option("--amount", type=click.FloatRange(0, 1), default=1.0, show_default=True, help="How far toward --like.")
@handled
def song_di(song_id, part_id, profile_id, midi_path, track, text, power_chords, like, amount) -> None:
    """Render a part as a DI on your guitar, on the part's own clock."""
    from ..core import songs

    with progress_bar("rendering") as bar:
        result = songs.render_part_di(
            song_id, part_id, None if profile_id == "synthetic" else profile_id,
            midi=midi_path, track=track, text=text, power_chords=power_chords,
            like=like, amount=amount, progress=bar,
        )
    click.echo(result.render())


@group.command("brief")
@click.argument("song_id")
@click.argument("part_id", required=False)
@handled
def song_brief(song_id, part_id) -> None:
    """Everything known about a song or part - what a researcher starts from."""
    from ..core import songs

    click.echo(songs.brief(song_id, part_id))


@group.command("rm")
@click.argument("song_id")
@click.option("--yes", is_flag=True)
@handled
def song_rm(song_id, yes) -> None:
    """Delete a song and its folder."""
    from ..core import songs

    if not yes:
        click.confirm(f"Delete '{song_id}' and everything in its folder?", abort=True)
    click.echo(songs.delete(song_id))

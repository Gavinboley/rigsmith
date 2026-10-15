"""``rigsmith learn`` - teach rigsmith your guitar, then play parts on it."""

from __future__ import annotations

import json
from pathlib import Path

import click

from ..clikit import _emit, handled
from . import progress_bar


@click.group("learn")
def group() -> None:
    """Learn your guitar from a guided recording, then play parts on it."""


@group.command("list")
@click.option("--json", "as_json", is_flag=True)
@handled
def learn_list(as_json: bool) -> None:
    """Every learned guitar profile."""
    from ..core import profiles

    found = profiles.list_profiles()
    _emit(profiles.render_list(found), as_json=as_json, payload=[p.summary() for p in found])


@group.command("new")
@click.argument("name")
@click.option("--guitar", help="The instrument, e.g. 'Les Paul Standard'.")
@click.option("--pickup", help="bridge, neck, middle, both...")
@click.option("--tuning", help="'E standard', 'drop D', or the open strings low to high.")
@click.option("--note", help="Anything worth remembering about it.")
@handled
def learn_new(name, guitar, pickup, tuning, note) -> None:
    """Start an empty profile for one guitar and pickup."""
    from ..core import profiles

    profile = profiles.create(name, guitar=guitar, pickup=pickup, tuning=tuning, note=note)
    click.echo(profile.describe())
    click.echo(f"\nNext:  rigsmith learn guide quick --tuning '{profile.tuning}'")


@group.command("show")
@click.argument("profile_id")
@click.option("--json", "as_json", is_flag=True)
@click.option("--samples", is_flag=True, help="List every sample too.")
@handled
def learn_show(profile_id: str, as_json: bool, samples: bool) -> None:
    """One profile: coverage, fingerprint, sources."""
    from ..core import profiles

    profile = profiles.get(profile_id)
    if as_json:
        _emit("", as_json=True, payload=profile.to_dict() if samples else profile.summary())
        return
    click.echo(profile.describe())
    grid = profile.coverage(frets=12)
    click.echo("\n  fret  " + " ".join(f"{f:>2}" for f in range(13)))
    for string in range(1, grid["strings"] + 1):
        row = grid["grid"][string - 1]
        click.echo(f"  s{string}    " + " ".join(" #" if c else " ." for c in row))
    for source in profile.sources:
        click.echo(f"  from {Path(source['path']).name}: {source['kept']} sample(s), {source['protocol']}")
    if samples:
        for sample in profile.samples:
            click.echo(
                f"  {sample.sample_id:<24} {sample.name:<4} {sample.cents:+5.0f}c "
                f"{sample.peak_db:6.1f} dB  {sample.dynamic}  {sample.technique}"
            )


@group.command("tunings")
@handled
def learn_tunings() -> None:
    """Tunings rigsmith knows by name."""
    from ..learn.instrument import tunings

    for tune in tunings():
        click.echo(f"  {tune.describe()}")


@group.command("guide")
@click.argument("protocol_id", default="quick")
@click.option("--tuning", help="Your tuning.")
@click.option("--frets", type=int, help="How far up the neck (chromatic only).")
@click.option("--click", "with_click", is_flag=True, help="Also write a click track to play along to.")
@handled
def learn_guide(protocol_id: str, tuning, frets, with_click: bool) -> None:
    """What to play for a protocol: quick, chromatic, dynamics, techniques, free."""
    from ..core import profiles
    from ..learn import protocols

    protocol = protocols.get(protocol_id, tuning=tuning, frets=frets)
    click.echo(protocol.describe())
    click.echo(f"tuning: {protocol.tuning}   one step every {protocol.spacing_s:.1f}s\n")
    for number, step in enumerate(protocol.steps, start=1):
        click.echo(f"  {number:3}. {step.prompt}  ({step.label}, {step.dynamic})")
    if with_click:
        path, _, count_in = profiles.click_track(protocol_id, tuning=tuning, frets=frets)
        click.echo(f"\nclick track: {path}  ({count_in:.1f}s count-in)")
    click.echo(
        "\nRecord it as a DI (interface input, or the Core's USB channel 5), then:\n"
        f"  rigsmith learn from <profile> <recording> --protocol {protocol.protocol_id}"
    )


@group.command("from")
@click.argument("profile_id")
@click.argument("source")
@click.option("--protocol", "protocol_id", default="free", show_default=True)
@click.option("--frets", type=int)
@click.option("--sensitivity", default=1.0, show_default=True, help="Above 1 finds softer notes.")
@click.option("--no-voice", is_flag=True, help="Skip refitting the voice model.")
@handled
def learn_from(profile_id, source, protocol_id, frets, sensitivity, no_voice) -> None:
    """Learn a recording (take id or path) into a profile."""
    from ..core import profiles

    with progress_bar("learning") as bar:
        report = profiles.learn(
            profile_id, source, protocol_id=protocol_id, frets=frets,
            sensitivity=sensitivity, fit_voice=not no_voice, progress=bar,
        )
    click.echo(report.render())


@group.command("analyse")
@click.argument("source")
@click.option("--protocol", "protocol_id", default="free", show_default=True)
@click.option("--tuning")
@click.option("--json", "as_json", is_flag=True)
@handled
def learn_analyse(source, protocol_id, tuning, as_json) -> None:
    """Show the notes in a recording without learning anything."""
    from ..core import profiles
    from ..learn.segment import Note

    result = profiles.analyse(source, protocol_id=protocol_id, tuning=tuning)
    if as_json:
        _emit("", as_json=True, payload=result)
        return
    for data in result["notes"]:
        click.echo("  " + Note.from_dict(data).describe())
    click.echo(f"\n{json.dumps(result['summary'])}")
    if result["alignment_text"]:
        click.echo(result["alignment_text"])


@group.command("play")
@click.argument("profile_id")
@click.argument("notes", required=False)
@click.option("--file", "notes_file", type=click.Path(exists=True, path_type=Path), help="A text file of notes.")
@click.option("--out", type=click.Path(path_type=Path))
@click.option("--noise", is_flag=True, help="Include the guitar's own noise floor and hum.")
@handled
def learn_play(profile_id, notes, notes_file, out, noise) -> None:
    """Play typed notes on a profile: 'start dur note [vel]' per line.

    Use 'synthetic' as the profile to hear the generic string.
    """
    from ..core import profiles

    text = notes_file.read_text(encoding="utf-8") if notes_file else (notes or "")
    text = text.replace(";", "\n")
    path, rendered = profiles.render_text(
        None if profile_id == "synthetic" else profile_id, text, out_path=out, noise=noise
    )
    click.echo(f"{path}\n{rendered.describe()}")


@group.command("note")
@click.argument("profile_id")
@click.argument("note")
@click.option("--string", type=int)
@handled
def learn_note(profile_id, note, string) -> None:
    """Render one note from a profile, to hear what it learned."""
    from ..core import profiles
    from ..learn.instrument import note_to_midi

    midi = float(note) if note.replace(".", "", 1).isdigit() else note_to_midi(note)
    path, rendered = profiles.preview_note(profile_id, midi, string=string)
    click.echo(f"{path}\n{rendered.describe()}")


@group.command("replay")
@click.argument("profile_id")
@click.argument("source")
@handled
def learn_replay(profile_id, source) -> None:
    """Play the notes of one recording on a different guitar profile."""
    from ..core import profiles

    path, rendered = profiles.replay_on(profile_id, source)
    click.echo(f"{path}\n{rendered.describe()}")


@group.command("part")
@click.argument("profile_id")
@click.option("--stem", help="The record's guitar stem (or a take id).")
@click.option("--midi", "midi_path", type=click.Path(exists=True, path_type=Path), help="A tab's MIDI export.")
@click.option("--track", type=int, help="MIDI track number.")
@click.option("--notes", "text", help="Typed notes instead.")
@click.option("--region", help="start:end of the record, or 'auto'.")
@click.option("--mode", type=click.Choice(["auto", "mono", "poly"]), default="auto", show_default=True)
@click.option("--power-chords", is_flag=True, help="Put fifths back on a transcribed rhythm part.")
@click.option("--transpose", type=int, default=0)
@click.option("--label")
@click.option("--take", "as_take", is_flag=True, help="File the result in the takes library.")
@handled
def learn_part(profile_id, stem, midi_path, track, text, region, mode, power_chords, transpose, label, as_take) -> None:
    """Render a song's part as a DI on your guitar, on the record's clock.

    Use 'synthetic' as the profile when you have not learned a guitar yet.
    """
    from ..core import parts

    with progress_bar("rendering the part") as bar:
        result = parts.part_di(
            None if profile_id == "synthetic" else profile_id,
            stem=stem, midi=midi_path, track=track, text=text, region=region,
            mode=mode, power_chords=power_chords, transpose=transpose,
            label=label, file_as_take=as_take, progress=bar,
        )
    click.echo(result.render())
    if stem:
        click.echo(
            "\nThe DI plays the record's notes on its clock, so score candidates "
            "rendered from it with --mode performance."
        )


@group.command("transcribe")
@click.argument("stem")
@click.option("--region")
@click.option("--mode", type=click.Choice(["auto", "mono", "poly"]), default="auto", show_default=True)
@click.option("--lowest", default="auto", show_default=True, help="Lowest string, e.g. E2, or auto.")
@click.option("--midi-out", type=click.Path(path_type=Path), help="Write the notes as MIDI.")
@handled
def learn_transcribe(stem, region, mode, lowest, midi_out) -> None:
    """Estimate the notes of a guitar stem."""
    from ..core.matching import audio_path, resolve_region
    from ..learn import midifile
    from ..learn.transcribe import describe_events, transcribe
    from ..match.render_offline import load_audio

    path = audio_path(stem)
    audio, rate = load_audio(path)
    span = resolve_region(region, path)
    if span:
        audio = span.slice(audio, rate, label="the stem")
    result = transcribe(audio, rate, mode=mode, lowest=lowest)
    click.echo(result.describe())
    click.echo(describe_events(result.events))
    if midi_out:
        click.echo(f"\nwrote {midifile.write(midi_out, result.events)}")


@group.command("demo")
@click.option("--protocol", "protocol_id", default="quick", show_default=True)
@click.option("--into", "profile_id", help="Learn it into this profile (created if missing).")
@click.option("--pickup", default="bridge", show_default=True, type=click.Choice(["bridge", "neck", "middle", "bridge-single", "neck-single"]))
@handled
def learn_demo(protocol_id, profile_id, pickup) -> None:
    """A synthetic recording of a protocol, to walk the flow with no guitar."""
    from ..core import profiles
    from ..core.errors import NotFound

    path, protocol = profiles.demo_take(protocol_id, pickup=pickup)
    click.echo(f"synthetic {protocol.name} take: {path}")
    if profile_id:
        try:
            profiles.get(profile_id)
        except NotFound:
            profiles.create(profile_id, guitar="Synthetic demo", pickup=pickup, note="made by 'learn demo' - not a real guitar")
        report = profiles.learn(profile_id, str(path), protocol_id=protocol_id)
        click.echo(report.render())


@group.command("voice")
@click.argument("profile_id")
@handled
def learn_voice(profile_id) -> None:
    """Refit a profile's voice model from its samples."""
    from ..core import profiles

    model = profiles.refit_voice(profile_id)
    click.echo(f"voice model fitted from {len(model.notes)} note(s)")


@group.command("rm-sample")
@click.argument("profile_id")
@click.argument("sample_id")
@handled
def learn_rm_sample(profile_id, sample_id) -> None:
    """Drop one bad sample (a buzz, a note that rang into the next)."""
    from ..core import profiles

    profile = profiles.remove_sample(profile_id, sample_id)
    click.echo(f"removed {sample_id}; {len(profile.samples)} sample(s) left")


@group.command("rm")
@click.argument("profile_id")
@click.option("--yes", is_flag=True)
@handled
def learn_rm(profile_id, yes) -> None:
    """Delete a profile and its samples."""
    from ..core import profiles

    if not yes:
        click.confirm(f"Delete profile '{profile_id}' and every sample in it?", abort=True)
    click.echo(profiles.delete(profile_id))

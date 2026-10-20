"""``rigsmith autopilot`` - from a song to a rig in one run."""

from __future__ import annotations

import time

import click

from ..clikit import handled


@click.group("autopilot")
def group() -> None:
    """Song in, rig out: map, research, render your part, try amps, tune, build."""


@group.command("run")
@click.argument("title")
@click.option("--artist")
@click.option("--mix", help="Your copy of the song.")
@click.option("--stem", help="An isolated guitar stem, if you have one.")
@click.option("--song", "song_id", help="An existing song instead of a title.")
@click.option("--part", "parts", multiple=True, help="Part id(s) to chase; default: the most prominent.")
@click.option("--profile", "profile_id", help="Your guitar profile; default from settings.")
@click.option("--di", help="A DI of the part you already have.")
@click.option("--midi", help="A tab's MIDI export for the part.")
@click.option("--bench", type=click.Choice(["virtual", "device"]), default="virtual", show_default=True)
@click.option("--route", "route_id", help="Loop route for the device bench.")
@click.option("--candidates", default=5, show_default=True)
@click.option("--budget", "tune_budget", default=24, show_default=True)
@click.option("--no-research", is_flag=True)
@click.option("--like", help="The record's guitar (strat, p90, paf, a learned guitar...); default: what the research says.")
@click.option("--keep-my-pickup", is_flag=True, help="Do not move the DI toward the researched guitar.")
@click.option("--render-di", is_flag=True, help="Render the DI even when the part has your own take lined up.")
@click.option("--ear-habits", is_flag=True, help="Start the amp from the knobs you keep turning after autopilot ('autopilot habits').")
@click.option("--whole-song", is_flag=True, help="With no --part: a part for each sound the song has, and one rig with a scene for each.")
@click.option("--detach", is_flag=True, help="Start it and return; check with 'autopilot show'.")
@handled
def autopilot_run(title, artist, mix, stem, song_id, parts, profile_id, di, midi, bench, route_id, candidates, tune_budget, no_research, like, keep_my_pickup, render_di, ear_habits, whole_song, detach) -> None:
    """Chase TITLE's guitar tone, start to finish."""
    from ..core import autopilot

    request = autopilot.Request(
        title=title, artist=artist, mix=mix, stem=stem, song_id=song_id, parts=list(parts),
        profile_id=profile_id, di=di, midi=midi, bench=bench, route_id=route_id,
        candidates=candidates, tune_budget=tune_budget, research=not no_research,
        like=like, match_guitar=not keep_my_pickup, use_part_take=not render_di, ear_habits=ear_habits,
        whole_song=whole_song,
    )
    run = autopilot.start(request)
    click.echo(f"autopilot {run.run_id} started.")
    if bench == "device":
        click.echo(f"On the Core: about {autopilot.core_minutes(tune_budget):.0f} minutes a part - every amp, cab and setting is reamped in real time.")
    if not detach:
        _follow(run.run_id)


@group.command("demo")
@click.option("--profile", "profile_id", help="Your guitar profile; default from settings.")
@click.option("--bench", type=click.Choice(["virtual", "device"]), default="virtual", show_default=True)
@click.option("--route", "route_id", help="Loop route for the device bench.")
@click.option("--one-part", is_flag=True, help="Only the most prominent part, not one for each of its three guitars.")
@click.option("--detach", is_flag=True, help="Start it and return; check with 'autopilot show'.")
@handled
def autopilot_demo(profile_id, bench, route_id, one_part, detach) -> None:
    """Try autopilot on a demo song made here - and see how close it gets.

    The demo's three guitars went through known settings of the simulated
    amp, so each part's run ends by saying which voicing it chose and how far
    each knob is from the one the song was made with - and the song gets one
    rig with a scene for each.
    """
    from ..core import demo

    run = demo.start(profile_id=profile_id, bench=bench, route_id=route_id, whole_song=not one_part)
    click.echo(f"autopilot {run.run_id} started on the demo song.")
    if not detach:
        _follow(run.run_id)


def _follow(run_id: str) -> None:
    """Print each stage as it moves, then the whole run."""
    from ..core import autopilot

    shown = {}
    while True:
        current = autopilot.get(run_id)
        for name in autopilot.STAGES:
            stage = current.stages[name]
            key = (stage.state, stage.summary)
            if shown.get(name) != key and stage.state != "waiting":
                shown[name] = key
                click.echo(f"  {stage.state:<8} {name:<11} {stage.summary}")
        if current.state != "running":
            break
        time.sleep(0.5)
    click.echo("\n" + autopilot.get(run_id).describe())


@group.command("habits")
@click.option("--bench", type=click.Choice(["virtual", "device"]), default="device", show_default=True)
@handled
def autopilot_habits(bench) -> None:
    """The knobs you keep turning the same way after autopilot."""
    from ..core import habits

    found = habits.habits(bench)
    looked = sum(1 for where, _ in habits.turns() if where == bench)
    if not found:
        click.echo(f"No habits yet on the {bench} bench ({looked} finished rig(s) looked at; a habit needs {habits.MIN_RIGS} turned the same way).")
        return
    for habit in found:
        click.echo(f"  {habit.describe()}")
    click.echo("Start a run from them with --ear-habits.")


@group.command("listen")
@click.argument("run_id")
@handled
def autopilot_listen(run_id) -> None:
    """Apply RUN_ID's blind A/B: the draft follows your ears, the recipe learns."""
    from ..core import autopilot

    run = autopilot.listen_back(run_id)
    for note in run.notes[-2:]:
        click.echo(note)


@group.command("song-rig")
@click.argument("run_ids", nargs=-1, required=True)
@click.option("--name", help="What to call the rig.")
@handled
def autopilot_song_rig(run_ids, name) -> None:
    """One rig with a scene per part, from finished runs of one song (up to five)."""
    from ..core import autopilot

    click.echo(autopilot.render_song_rig(autopilot.song_rig(list(run_ids), name=name)))


@group.command("list")
@handled
def autopilot_list() -> None:
    """Past runs."""
    from ..core import autopilot

    for run in autopilot.list_runs():
        best = run.results[0]["label"] if run.results else "-"
        click.echo(f"  {run.run_id}  {run.state:<8} {run.request.bench:<8} {run.request.title[:30]:<30} {best}")


@group.command("show")
@click.argument("run_id")
@handled
def autopilot_show(run_id) -> None:
    """One run, stage by stage."""
    from ..core import autopilot

    click.echo(autopilot.get(run_id).describe())


@group.command("resume")
@click.argument("run_id")
@click.option("--from", "from_stage", help="Re-run from this stage on.")
@handled
def autopilot_resume(run_id, from_stage) -> None:
    """Pick a run back up."""
    from ..core import autopilot

    autopilot.resume(run_id, from_stage=from_stage)
    click.echo(f"resumed {run_id}")

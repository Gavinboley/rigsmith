"""``rigsmith tune`` - turn a rig's knobs towards a target by listening."""

from __future__ import annotations

import click

from ..clikit import _emit, handled
from . import progress_bar


@click.group("tune")
def group() -> None:
    """Search knob settings against a target - offline, or live on the Core through the loop."""


@group.command("run")
@click.argument("target")
@click.option("--di", help="The DI to render through (take id or path); defaults to your latest DI.")
@click.option("--region", help="start:end of the target, or auto.")
@click.option("--renderer", type=click.Choice(["virtual", "loop"]), default="virtual", show_default=True)
@click.option("--route", "route_id", help="Loop route (with --renderer loop).")
@click.option("--draft", "draft_id", help="Tune this draft's amp/NAM/EQ blocks.")
@click.option("--knob", "knobs", multiple=True, help="Only these, e.g. Amp.GainA. Repeatable.")
@click.option("--budget", default=30, show_default=True, help="How many settings to try.")
@click.option("--method", type=click.Choice(["auto", "pattern", "optuna", "random"]), default="auto", show_default=True)
@click.option("--mode", type=click.Choice(["distribution", "performance", "aligned"]), default="distribution", show_default=True)
@click.option("--song", "song_id", help="Record the result on this song's part (with --part).")
@click.option("--part", "part_id")
@handled
def tune_run(target, di, region, renderer, route_id, draft_id, knobs, budget, method, mode, song_id, part_id) -> None:
    """Search for the settings that bring a rig closest to TARGET."""
    from ..core import tuning

    song = {"song_id": song_id, "part_id": part_id} if song_id and part_id else None
    with progress_bar("tuning") as bar:
        run = tuning.tune(
            target, di, region=region, renderer=renderer, route_id=route_id, draft_id=draft_id,
            knobs=list(knobs) or None, budget=budget, method=method, mode=mode, song=song, progress=bar,
        )
    click.echo(run.describe())
    if draft_id:
        click.echo(f"\nApply it with:  rigsmith tune apply {run.run_id} {draft_id}")


@group.command("list")
@click.option("--json", "as_json", is_flag=True)
@handled
def tune_list(as_json) -> None:
    """Past tuning runs."""
    from ..core import tuning

    runs = tuning.list_runs()
    text = "\n".join(
        f"  {r.run_id}  {r.renderer:7} {r.mode:12} best {((r.result or {}).get('best') or {}).get('loss', float('nan')):.4f}  {r.target}"
        for r in runs
    ) or "No tuning runs yet."
    _emit(text, as_json=as_json, payload=[r.to_dict() for r in runs])


@group.command("show")
@click.argument("run_id")
@handled
def tune_show(run_id) -> None:
    """One run: where it started, where it ended, and the EQ it suggests."""
    from ..core import tuning

    click.echo(tuning.get_run(run_id).describe())


@group.command("apply")
@click.argument("run_id")
@click.argument("draft_id")
@click.option("--eq", "with_eq", is_flag=True, help="Also set the amp's slot EQ from the match.")
@handled
def tune_apply(run_id, draft_id, with_eq) -> None:
    """Put a run's best settings into a draft (writing to the device stays a separate, gated step)."""
    from ..core import tuning

    click.echo(tuning.apply(run_id, draft_id, with_eq=with_eq))


@group.command("eq")
@click.argument("target")
@click.argument("candidate")
@handled
def tune_eq(target, candidate) -> None:
    """The EQ that would close the gap between a candidate and the target."""
    from ..core.matching import audio_path
    from ..match.render_offline import load_audio, resample
    from ..tune.eq import match

    t, rate = load_audio(audio_path(target))
    c, crate = load_audio(audio_path(candidate))
    if crate != rate:
        c = resample(c, crate, rate)
    click.echo(match(t, c, rate).describe())

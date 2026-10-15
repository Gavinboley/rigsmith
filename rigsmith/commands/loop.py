"""``rigsmith loop`` - play a DI through the real Core and hear what it does."""

from __future__ import annotations

import click

from ..clikit import _emit, handled
from . import progress_bar


@click.group("loop")
def group() -> None:
    """The hardware loop: interface out -> Core -> back in, aligned and calibrated."""


@group.command("devices")
@click.option("--virtual", is_flag=True, help="List the simulated bench instead.")
@click.option("--json", "as_json", is_flag=True)
@handled
def loop_devices(virtual: bool, as_json: bool) -> None:
    """Audio devices this machine can see."""
    from ..core import loop

    found = loop.devices("virtual" if virtual else None)
    _emit(
        "\n".join(f"  {d.describe()}" for d in found) or "No audio devices.",
        as_json=as_json,
        payload=[d.to_dict() for d in found],
    )


@group.command("suggest")
@click.option("--virtual", is_flag=True)
@click.option("--adopt", help="Save the suggestion with this id as a route.")
@handled
def loop_suggest(virtual: bool, adopt: str | None) -> None:
    """Routes that make sense for what is plugged in."""
    from ..core import loop

    backend = "virtual" if virtual else None
    if adopt:
        route = loop.adopt_suggestion(adopt, backend=backend)
        click.echo(f"saved and made default:\n{route.describe()}")
        return
    found = loop.suggest_routes(backend)
    if not found:
        click.echo(
            "Nothing to suggest - no interface or HeadRush is visible.\n"
            "Connect them, or try the simulated bench:  rigsmith loop demo"
        )
        return
    for route in found:
        click.echo(route.describe())
        if route.note:
            click.echo(f"  wiring: {route.note}")
        click.echo("")
    click.echo("Save one with:  rigsmith loop suggest --adopt <id>")


@group.command("routes")
@click.option("--json", "as_json", is_flag=True)
@handled
def loop_routes(as_json: bool) -> None:
    """Saved routes; * marks the default."""
    from ..core import loop

    found = loop.list_routes()
    _emit(loop.render_routes(found, loop.store().default_id()), as_json=as_json, payload=[r.to_dict() for r in found])


@group.command("route")
@click.argument("route_id")
@click.option("--kind", type=click.Choice(["analog", "usb-reamp", "hybrid", "custom"]), default="analog", show_default=True)
@click.option("--out", "output_device", required=True, help="Output device name (substring is fine).")
@click.option("--in", "input_device", help="Input device; defaults to the output device.")
@click.option("--out-ch", "output_channels", multiple=True, type=int, help="Output channel(s), from 1.")
@click.option("--in-ch", "input_channels", multiple=True, type=int, help="Return channel(s), from 1.")
@click.option("--ref", "reference_channel", type=int, help="Channel carrying the DI as the Core received it.")
@click.option("--rate", "sample_rate", default=48000, show_default=True)
@click.option("--hostapi", help="e.g. 'Windows WASAPI' or 'ASIO'.")
@click.option("--default", "make_default", is_flag=True)
@handled
def loop_route(route_id, kind, output_device, input_device, output_channels, input_channels, reference_channel, sample_rate, hostapi, make_default) -> None:
    """Save a route by hand."""
    from ..core import loop

    route = loop.create_route(
        route_id,
        kind=kind,
        output_device=output_device,
        input_device=input_device,
        output_channels=list(output_channels) or None,
        input_channels=list(input_channels) or None,
        reference_device=(input_device or output_device) if reference_channel else None,
        reference_channel=reference_channel,
        sample_rate=sample_rate,
        hostapi=hostapi,
        make_default=make_default,
    )
    click.echo(route.describe())


@group.command("rm-route")
@click.argument("route_id")
@handled
def loop_rm_route(route_id: str) -> None:
    """Forget a route."""
    from ..core import loop

    loop.delete_route(route_id)
    click.echo(f"forgot {route_id}")


@group.command("default")
@click.argument("route_id")
@handled
def loop_default(route_id: str) -> None:
    """Make a route the one reamps use."""
    from ..core import loop

    loop.set_default(route_id)
    click.echo(f"{route_id} is now the default route")


@group.command("demo")
@handled
def loop_demo() -> None:
    """Set up the simulated bench - an interface and a Core that do not exist."""
    from ..core import loop

    for route in loop.demo_bench():
        click.echo(route.describe())
    click.echo("\nEvery capture made on these is labelled simulated.")


@group.command("tone")
@click.option("--route", "route_id")
@click.option("--freq", default=440.0, show_default=True)
@click.option("--level", default=-18.0, show_default=True, help="dBFS.")
@handled
def loop_tone(route_id, freq, level) -> None:
    """Send a tone round the loop - the quickest wiring check there is."""
    from ..core import loop

    result = loop.test_tone(route_id, frequency=freq, level_db=level)
    click.echo(result["message"])


@group.command("bench")
@handled
def loop_bench() -> None:
    """Check the Core's loaded rig has an Amp and a Cab for autopilot to switch. Read only."""
    from ..core import tuning

    click.echo(tuning.bench_check().describe())


@group.command("bench-rig")
@handled
def loop_bench_rig() -> None:
    """Make a draft with one amp and one cab - the device bench's rig. Writes nothing."""
    from ..core import tuning

    draft_id = tuning.bench_rig_draft()
    click.echo(f"draft '{draft_id}': one amp, one cab.")
    click.echo(f"Write it the usual way (backup first):  rigsmith rig write {draft_id}")


@group.command("level")
@click.argument("device")
@click.option("--channel", default=1, show_default=True)
@click.option("--seconds", default=3.0, show_default=True)
@handled
def loop_level(device, channel, seconds) -> None:
    """Play hard for a few seconds: is DEVICE's input level right for a DI? Saves nothing."""
    from ..core import loop

    click.echo(f"Listening to {device} channel {channel} for {seconds:.0f}s - play hard now.")
    result = loop.input_level(device, channel=channel, seconds=seconds)
    click.echo(f"{result['verdict']}: peak {result['peak_db']} dBFS, noise floor {result['noise_floor_db']} dBFS")
    click.echo(f"  {result['advice']}")


@group.command("meter")
@click.option("--route", "route_id")
@handled
def loop_meter(route_id) -> None:
    """Input levels on the route's return right now."""
    from ..core import loop

    for level in loop.meter(route_id)["levels"]:
        flag = "  CLIPPING" if level["clipping"] else ""
        click.echo(f"  in {level['channel']}: peak {level['peak_db']:.1f} dBFS, rms {level['rms_db']:.1f}{flag}")


@group.command("calibrate")
@click.option("--route", "route_id")
@click.option("--di", help="A DI of your playing (take id or path) to set the level with.")
@click.option("--profile", "profile_id", help="Or use a learned profile's own notes.")
@click.option("--direct", help="The guitar played straight into the Core, recorded (take id or path).")
@click.option("--direct-is-reference", is_flag=True, help="--direct was recorded on the reference channel (Core USB 5).")
@click.option("--dry-run", is_flag=True, help="Measure, but do not change the route's send gain.")
@handled
def loop_calibrate(route_id, di, profile_id, direct, direct_is_reference, dry_run) -> None:
    """Measure latency, noise, response, and the send level that matches your guitar."""
    from ..core import loop

    with progress_bar("calibrating") as bar:
        result = loop.calibrate(
            route_id, di=di, profile_id=profile_id, direct=direct,
            direct_is_reference=direct_is_reference, apply=not dry_run, progress=bar,
        )
    click.echo(result.describe())


@group.command("reamp")
@click.argument("di")
@click.option("--route", "route_id")
@click.option("--label", default="loop", show_default=True)
@click.option("--take", "as_take", is_flag=True, help="File the return in the takes library.")
@handled
def loop_reamp(di, route_id, label, as_take) -> None:
    """Send a DI (take id or path) round the loop and keep what comes back."""
    from ..core import loop

    with progress_bar("reamping") as bar:
        result = loop.reamp(di, route_id=route_id, label=label, file_as_take=as_take, progress=bar)
    click.echo(result.render())


@group.command("record")
@click.argument("seconds", type=float)
@click.option("--device", required=True, help="Input device, e.g. your interface.")
@click.option("--channel", default=1, show_default=True)
@click.option("--name", "take_id", default="recording", show_default=True)
@click.option("--guitar")
@click.option("--pickup")
@click.option("--tuning")
@click.option("--note")
@click.option("--virtual", is_flag=True)
@handled
def loop_record(seconds, device, channel, take_id, guitar, pickup, tuning, note, virtual) -> None:
    """Record your guitar from an interface input straight into the takes library."""
    from ..core import loop

    with progress_bar("recording") as bar:
        take = loop.record_input(
            seconds, device=device, channel=channel, take_id=take_id,
            guitar=guitar, pickup=pickup, tuning=tuning, note=note,
            backend="virtual" if virtual else None, progress=bar,
        )
    click.echo(take.render())


@group.command("assess")
@click.argument("target")
@click.argument("capture")
@handled
def loop_assess(target, capture) -> None:
    """Sample-aligned comparison of two renditions of the same DI."""
    from ..core import loop

    click.echo(loop.assess(target, capture).render())


@group.command("agreement")
@click.argument("di")
@click.option("--nam", "nam_path")
@click.option("--ir", "ir_path")
@click.option("--route", "route_id")
@handled
def loop_agreement(di, nam_path, ir_path, route_id) -> None:
    """How well the offline renderer predicts the Core, for one capture/IR."""
    from ..core import loop

    with progress_bar("comparing offline and hardware") as bar:
        assessment, hardware = loop.model_agreement(di, nam_path=nam_path, ir_path=ir_path, route_id=route_id, progress=bar)
    click.echo(assessment.render())
    click.echo(f"\nhardware capture: {hardware.path}")

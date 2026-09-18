"""Command line interface.

A thin adapter over :mod:`rigsmith.core`: parse arguments, call core, print
the result. No orchestration lives here.

It mirrors the MCP tool surface, which it previously only claimed to - the
entire rig-authoring and tone-matching paths were agent-only, so a human
could not build a rig or run a single comparison without an MCP client.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sys
from pathlib import Path

import click

from . import __version__
from .config import Settings, get_settings, set_settings
from .core.errors import RigsmithError
from .core.takes import KINDS as TAKE_KINDS
from .core.takes import ORIGINS as TAKE_ORIGINS


def _run(coro):
    return asyncio.run(coro)


def handled(func):
    """Turn a core error into a styled message and the right exit code."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RigsmithError as exc:
            click.echo(click.style(exc.message, fg="red"), err=True)
            if exc.hint:
                click.echo(exc.hint, err=True)
            sys.exit(exc.exit_code)

    return wrapper


def _audio_label(reference) -> str:
    """A short name for something that may be a path or a take id."""
    text = str(reference)
    stem = Path(text).stem
    return stem or text


def _emit(text: str, *, as_json: bool = False, payload=None) -> None:
    if as_json and payload is not None:
        click.echo(json.dumps(payload, indent=2, default=str))
    else:
        click.echo(text)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="rigsmith")
@click.option("--device", "device_host", metavar="HOST", help="Device host or IP, skipping discovery.")
@click.option("--workspace", type=click.Path(path_type=Path), help="Override the rigsmith workspace directory.")
@click.option("--readonly", is_flag=True, help="Disable every write for this invocation.")
def main(device_host: str | None, workspace: Path | None, readonly: bool) -> None:
    """Build and test HeadRush Core guitar tones.

    Nothing here modifies or reflashes device firmware. Writes to the device
    go through the same interfaces the official Remote Editor uses, and are
    gated behind backup, diff, and confirmation.
    """
    settings = Settings()
    if device_host:
        settings.device_host = device_host
    if workspace:
        settings.workspace = workspace
    if readonly:
        settings.readonly = True
    set_settings(settings)


# ==========================================================================
# top level
# ==========================================================================


@main.command()
@click.option("--no-sweep", is_flag=True, help="Skip the subnet sweep fallback.")
@click.option("--timeout", default=2.0, show_default=True, help="Per-host HTTP timeout, seconds.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
@handled
def discover(no_sweep: bool, timeout: float, as_json: bool) -> None:
    """Find the HeadRush on the local network."""
    from dataclasses import asdict

    from .core import devices

    found = _run(devices.discover(include_sweep=not no_sweep, timeout=timeout))
    if as_json:
        click.echo(json.dumps([asdict(d) for d in found], indent=2))
        return
    click.echo(devices.render_discovery(found))
    if not found:
        sys.exit(1)


@main.command()
@click.option("--dump-app", is_flag=True, help="Archive the served bundle alongside the inventory.")
@click.option("--out", type=click.Path(path_type=Path), help="Output directory.")
@click.option("--max-assets", default=200, show_default=True, help="Cap on files fetched.")
@handled
def probe(dump_app: bool, out: Path | None, max_assets: int) -> None:
    """Fetch the Remote Editor bundle and extract its API surface."""
    from .core import devices

    result = _run(
        devices.probe(max_assets=max_assets, save_assets=dump_app, out_dir=out)
    )
    click.echo(result.render())


@main.command()
@handled
def doctor() -> None:
    """Report what rigsmith can and cannot do in this environment."""
    from .core import captures
    from .core.workspace import inspect as inspect_workspace
    from .device.discovery import local_subnets, resolve_hostnames

    settings = get_settings()
    click.echo(f"rigsmith {__version__}")
    click.echo(f"workspace: {settings.workspace}")
    click.echo(f"read-only: {settings.readonly}")
    click.echo("")

    checks = [
        ("httpx (device HTTP)", "httpx", "required - pip install httpx"),
        ("websockets (device WS)", "websockets", "required - pip install websockets"),
        ("zeroconf (mDNS discovery)", "zeroconf", "pip install zeroconf - without it, discovery falls back to hostname + sweep"),
        ("mcp (agent tools)", "mcp", "pip install 'rigsmith[mcp]' to expose tools to an agent"),
        ("fastapi (web UI)", "fastapi", "pip install 'rigsmith[ui]' for the browser interface"),
        ("mido (MIDI)", "mido", "pip install 'rigsmith[midi]' for rig/scene switching and verification"),
        ("sounddevice (hardware reamp)", "sounddevice", "pip install 'rigsmith[audio]' for hardware-in-the-loop testing"),
        ("soundfile (audio I/O)", "soundfile", "pip install 'rigsmith[audio]'"),
        ("scipy (IR convolution)", "scipy", "pip install 'rigsmith[match]' for offline rendering"),
        ("demucs (guitar isolation)", "demucs", "pip install 'rigsmith[separation]' to pull a guitar stem out of a record"),
        ("neural-amp-modeler (offline NAM)", "nam", "pip install neural-amp-modeler to render captures without hardware"),
        ("playwright (UI tests)", "playwright", "pip install 'rigsmith[browser]' - only needed to run the browser tests"),
    ]
    for label, module, why in checks:
        try:
            __import__(module)
            click.echo(click.style("  ok  ", fg="green") + label)
        except ImportError:
            click.echo(click.style("  --  ", fg="yellow") + label)
            click.echo(f"        {why}")

    click.echo("")
    click.echo(captures.auth_status().render())

    click.echo("")
    hostnames = resolve_hostnames()
    if hostnames:
        for name, addr in hostnames:
            click.echo(f"  mDNS: {name} -> {addr}")
    else:
        click.echo("  mDNS: no HeadRush .local hostname resolves right now")
    subnets = ", ".join(str(n) for n in local_subnets()) or "none detected"
    click.echo(f"  subnets to sweep: {subnets}")

    report = inspect_workspace()
    click.echo(f"  workspace holds {report.file_count} files")

    # What is actually in there, because "which of my tones have I checked"
    # is the question a person opens this tool with.
    from .rig.catalog import get_catalog
    from .core import recipes as core_recipes
    from .core import rigs as core_rigs

    catalog = get_catalog()
    if catalog.is_verified:
        click.echo(f"  catalog: {len(catalog.models)} models from {catalog.source}")
    else:
        click.echo(
            "  catalog: unverified - model ids and parameter ranges are not "
            "checked. Run: rigsmith catalog extract"
        )

    try:
        drafts = core_rigs.list_drafts()
        click.echo(f"  rigs: {len(drafts)} draft(s)")
    except RigsmithError:
        pass

    try:
        found = core_recipes.list_recipes()
    except RigsmithError:
        found = []
    if found:
        verified = sum(1 for _, recipe in found if recipe.verified)
        click.echo(
            f"  recipes: {len(found)}, {verified} verified by ear"
            + ("" if verified == len(found) else
               f", {len(found) - verified} still only reasoned about")
        )


@main.command()
@handled
def serve() -> None:
    """Run the MCP server over stdio.

    Register it with Claude Code:

      claude mcp add rigsmith -- rigsmith serve
    """
    from .mcp.server import main as run_server

    run_server()


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind address. Loopback only by default.")
@click.option("--port", default=0, show_default=True, help="0 picks a free port automatically.")
@click.option("--no-browser", is_flag=True, help="Do not open a browser tab.")
@handled
def ui(host: str, port: int, no_browser: bool) -> None:
    """Launch the local web interface."""
    from .web.server import run_ui

    run_ui(host=host, port=port, open_browser=not no_browser)


# ==========================================================================
# rig authoring
# ==========================================================================


@main.group()
def rig() -> None:
    """Build and edit rigs."""


@rig.command("new")
@click.argument("name")
@click.option("--path", "signal_path", default="straight", show_default=True,
              type=click.Choice(["straight", "middle_split_3_4_3", "middle_split_5_3_3", "middle_split_7_2_3"]))
@click.option("--notes", help="What tone you are chasing.")
@handled
def rig_new(name: str, signal_path: str, notes: str | None) -> None:
    """Start a new rig draft."""
    from .core import rigs

    result = rigs.create(name, signal_path=signal_path, notes=notes)
    click.echo(result.summary)
    click.echo(f"\nAdd blocks with:  rigsmith rig add {result.draft.draft_id} nam 'Amp name'")


@rig.command("list")
@handled
def rig_list() -> None:
    """List drafts in progress."""
    from .core import rigs

    drafts = rigs.list_drafts()
    if not drafts:
        click.echo("No drafts. Create one with:  rigsmith rig new \"My Rig\"")
        return
    for draft in drafts:
        click.echo(
            f"  {draft.draft_id:24} '{draft.rig.name}'  "
            f"{len(draft.rig.blocks)}/14 blocks, {len(draft.rig.scenes)} scenes"
        )


@rig.command("show")
@click.argument("draft_id")
@click.option("--json", "as_json", is_flag=True)
@handled
def rig_show(draft_id: str, as_json: bool) -> None:
    """Print a draft in full."""
    from .core import rigs
    from .rig.codec import rig_to_dict

    draft = rigs.get(draft_id)
    if as_json:
        click.echo(json.dumps(rig_to_dict(draft.rig), indent=2))
        return
    click.echo(draft.rig.describe())
    click.echo(f"\nchain: {draft.rig.signal_chain()}")


@rig.command("add")
@click.argument("draft_id")
@click.argument("category", type=click.Choice(["amp", "cab", "ir", "clone", "nam", "effect"]))
@click.argument("model_id")
@click.option("--slot", type=int, help="1-14. Omit to append.")
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE", help="Repeatable.")
@handled
def rig_add(draft_id: str, category: str, model_id: str, slot: int | None, params: tuple[str, ...]) -> None:
    """Add a block to the chain."""
    from .core import rigs

    parsed = {}
    for item in params:
        if "=" not in item:
            raise click.BadParameter(f"--param must be KEY=VALUE, got '{item}'")
        key, _, value = item.partition("=")
        try:
            parsed[key] = json.loads(value)
        except json.JSONDecodeError:
            parsed[key] = value

    click.echo(rigs.add_block(draft_id, category, model_id, slot=slot, params=parsed).render())


@rig.command("rm")
@click.argument("draft_id")
@click.argument("slot", type=int)
@handled
def rig_rm(draft_id: str, slot: int) -> None:
    """Remove the block in a slot."""
    from .core import rigs

    click.echo(rigs.remove_block(draft_id, slot).render())


@rig.command("move")
@click.argument("draft_id")
@click.argument("from_slot", type=int)
@click.argument("to_slot", type=int)
@handled
def rig_move(draft_id: str, from_slot: int, to_slot: int) -> None:
    """Move a block to a different slot."""
    from .core import rigs

    click.echo(rigs.move_block(draft_id, from_slot, to_slot).render())


@rig.command("set")
@click.argument("draft_id")
@click.argument("slot", type=int)
@click.argument("name")
@click.argument("value")
@handled
def rig_set(draft_id: str, slot: int, name: str, value: str) -> None:
    """Set one parameter on one block."""
    from .core import rigs

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        parsed = value
    click.echo(rigs.set_param(draft_id, slot, name, parsed).render(chain=False))


@rig.command("toggle")
@click.argument("draft_id")
@click.argument("slot", type=int)
@click.option("--off", is_flag=True, help="Bypass instead of enable.")
@handled
def rig_toggle(draft_id: str, slot: int, off: bool) -> None:
    """Enable or bypass a block in the rig's base state."""
    from .core import rigs

    click.echo(rigs.toggle_block(draft_id, slot, not off).render())


@rig.command("doubling")
@click.argument("draft_id")
@click.argument("slot", type=int)
@click.argument("mode", type=click.Choice(["none", "stereo", "switch"]))
@click.option("--with", "second", help="Second model for the doubled slot.")
@handled
def rig_doubling(draft_id: str, slot: int, mode: str, second: str | None) -> None:
    """Configure 2X doubling on an amp/cab/IR/clone/NAM slot."""
    from .core import rigs

    click.echo(rigs.set_doubling(draft_id, slot, mode, second).render(chain=False))


@rig.command("scene")
@click.argument("draft_id")
@click.argument("name")
@click.option("--on", "on_slots", multiple=True, type=int, help="Slots to switch on. Repeatable.")
@click.option("--off", "off_slots", multiple=True, type=int, help="Slots to switch off. Repeatable.")
@click.option("--clear", "no_change_slots", multiple=True, type=int, help="Slots to drop from the scene.")
@click.option("--fs", "footswitch", type=int, help="Footswitch 1-5.")
@click.option("--reset", is_flag=True, help="Discard the scene's existing configuration first.")
@handled
def rig_scene(draft_id, name, on_slots, off_slots, no_change_slots, footswitch, reset) -> None:
    """Create or update a scene. Updates merge unless --reset is given."""
    from .core import rigs

    click.echo(
        rigs.configure_scene(
            draft_id, name,
            on_slots=list(on_slots), off_slots=list(off_slots),
            no_change_slots=list(no_change_slots),
            footswitch=footswitch, reset=reset,
        ).render(chain=False)
    )


@rig.command("rm-scene")
@click.argument("draft_id")
@click.argument("name")
@handled
def rig_rm_scene(draft_id: str, name: str) -> None:
    """Remove a scene."""
    from .core import rigs

    click.echo(rigs.delete_scene(draft_id, name).render(chain=False))


@rig.command("validate")
@click.argument("draft_id")
@handled
def rig_validate(draft_id: str) -> None:
    """Check a draft for problems."""
    from .core import rigs

    result = rigs.validate(draft_id)
    header = click.style("VALID", fg="green") if result.ok else click.style(
        f"INVALID - {len(result.errors)} error(s)", fg="red"
    )
    click.echo(header)
    click.echo(result.report())
    if not result.ok:
        sys.exit(1)


@rig.command("preview")
@click.argument("draft_id")
@handled
def rig_preview(draft_id: str) -> None:
    """Show what writing this draft would change on the device."""
    from .core import rigs

    difference = rigs.preview(draft_id)
    result = rigs.validate(draft_id)
    click.echo(difference.render())
    click.echo(f"\nsummary: {difference.summary()}")
    click.echo(f"est. CPU: ~{result.estimated_cpu:.0%}")
    if not result.ok:
        click.echo(click.style(f"\nBLOCKED: {len(result.errors)} error(s)", fg="red"))
        for finding in result.errors:
            click.echo(f"  {finding}")


@rig.command("export")
@click.argument("draft_id")
@click.option("--out", type=click.Path(path_type=Path), help="Destination file.")
@handled
def rig_export(draft_id: str, out: Path | None) -> None:
    """Write a draft to a JSON file."""
    from .core import rigs

    click.echo(f"Exported to {rigs.export(draft_id, out)}")


@rig.command("write")
@click.argument("draft_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option(
    "--no-backup",
    is_flag=True,
    help="Write even if the device's USB storage is not mounted. The slot's "
         "current contents are then unrecoverable.",
)
@handled
def rig_write(draft_id: str, yes: bool, no_backup: bool) -> None:
    """Write a draft to the device, after a backup and a diff."""
    from .core import rigs

    # Show what would change, without the dry-run's own "not written" line -
    # it reads as a refusal when you are about to confirm.
    dry = rigs.write(draft_id, confirm=False)
    click.echo(dry.diff.render())
    click.echo(f"\nsummary: {dry.diff.summary()}")
    click.echo(f"est. CPU: ~{dry.validation.estimated_cpu:.0%}")

    if not dry.validation.ok:
        click.echo(click.style(f"\n{dry.message}", fg="red"), err=True)
        sys.exit(1)

    if not yes and not click.confirm("\nWrite this to the device?", default=False):
        click.echo("Not written.")
        return

    # Discovery sweeps the subnet when nothing answers, which is ten seconds
    # or more of silence otherwise.
    click.echo("")
    click.echo("Backing up and looking for the device...")
    result = rigs.write(draft_id, confirm=True, allow_without_backup=no_backup)
    click.echo("")
    if result.backup_path:
        click.echo(f"Backup taken first: {result.backup_path}")
    message = result.message
    if "waive the backup requirement" in message:
        # Core stays surface-agnostic; the flag name belongs here.
        message += "\n\nOn this command that is:  --no-backup"
    click.echo(click.style(message, fg="green" if result.written else "yellow"))
    if result.report is not None:
        click.echo("")
        click.echo(result.report.render())
    if not result.written:
        sys.exit(1)


@rig.command("delete")
@click.argument("draft_id")
@click.option("--yes", is_flag=True)
@handled
def rig_delete(draft_id: str, yes: bool) -> None:
    """Discard a draft."""
    from .core import rigs

    if not yes and not click.confirm(f"Delete draft '{draft_id}'?", default=False):
        return
    click.echo(rigs.delete(draft_id))


@rig.command("duplicate")
@click.argument("draft_id")
@click.option("--name", help="Name for the copy.")
@handled
def rig_duplicate(draft_id: str, name: str | None) -> None:
    """Fork a draft so a variant can be tried."""
    from .core import rigs

    click.echo(rigs.duplicate(draft_id, name).summary)


# ==========================================================================
# device
# ==========================================================================


@main.group()
def device() -> None:
    """Device storage, backup, and MIDI control."""


@device.command("status")
@click.option("--path", type=click.Path(path_type=Path))
@handled
def device_status(path: Path | None) -> None:
    """Show whether the device's USB storage is mounted."""
    from .core import devices

    click.echo(devices.storage_status(path).describe())


@device.command("backup")
@click.option("--path", type=click.Path(path_type=Path), help="Explicit mount path.")
@click.option("--to", "destination", type=click.Path(path_type=Path))
@handled
def device_backup(path: Path | None, destination: Path | None) -> None:
    """Copy everything off the device into a timestamped backup."""
    from .core import devices

    click.echo(devices.backup(path, destination).describe())


@device.command("install-nam")
@click.argument("nam_file", type=click.Path(exists=True, path_type=Path))
@click.option("--path", type=click.Path(path_type=Path))
@click.option("--overwrite", is_flag=True)
@handled
def device_install_nam(nam_file: Path, path: Path | None, overwrite: bool) -> None:
    """Copy a NAM capture onto the device over USB."""
    from .core import devices

    click.echo(devices.install_capture(nam_file, device_path=path, overwrite=overwrite))


@device.group("midi")
def device_midi() -> None:
    """Control and verify the device over MIDI."""


@device_midi.command("ports")
@handled
def midi_ports_cmd() -> None:
    """List MIDI ports."""
    from .core import devices

    click.echo(devices.render_midi_ports())


@device_midi.command("select")
@click.argument("program", type=int)
@click.option("--port")
@click.option("--channel", default=0, show_default=True)
@handled
def midi_select_cmd(program: int, port: str | None, channel: int) -> None:
    """Load a rig by MIDI Program Change."""
    from .core import devices

    click.echo(devices.select_rig(program, port=port, channel=channel))


@device_midi.command("footswitch")
@click.argument("number", type=int)
@click.option("--port")
@click.option("--channel", default=0, show_default=True)
@handled
def midi_fs_cmd(number: int, port: str | None, channel: int) -> None:
    """Press a footswitch (1-5) - also how scenes are activated."""
    from .core import devices

    click.echo(devices.press_footswitch(number, port=port, channel=channel))


@device_midi.command("tap")
@click.argument("bpm", type=float)
@click.option("--taps", default=4, show_default=True, help="More taps, steadier average.")
@click.option("--port")
@click.option("--channel", default=0, show_default=True)
@handled
def midi_tap_cmd(bpm: float, taps: int, port: str | None, channel: int) -> None:
    """Set the tempo by tapping it, the way a foot would.

    Delay and modulation times on a record are the song's tempo, and there is
    no "set BPM" message - CC64 is a tap and the device averages what it gets.
    """
    from .core import devices

    click.echo(devices.tap_tempo(bpm, taps=taps, port=port, channel=channel))


@device_midi.command("block")
@click.argument("slot", type=int)
@click.option("--off", is_flag=True)
@click.option("--port")
@click.option("--channel", default=0, show_default=True)
@handled
def midi_block_cmd(slot: int, off: bool, port: str | None, channel: int) -> None:
    """Turn a block on or off over MIDI."""
    from .core import devices

    click.echo(devices.set_block(slot, not off, port=port, channel=channel))


@device_midi.command("verify")
@click.argument("draft_id")
@click.option("--program", type=int, help="Select this rig first.")
@click.option("--port")
@handled
def midi_verify_cmd(draft_id: str, program: int | None, port: str | None) -> None:
    """Drive a draft's rig over MIDI to confirm the device responds."""
    from .core import devices

    click.echo(devices.verify_rig(draft_id, program=program, port=port).render())


# ==========================================================================
# NAM captures and TONE3000
# ==========================================================================


@main.group()
def nam() -> None:
    """Inspect Neural Amp Modeler captures."""


@nam.command("inspect")
@click.argument("capture", type=click.Path(exists=True, path_type=Path))
@handled
def nam_inspect(capture: Path) -> None:
    """Report a capture's architecture, size, calibration, and compatibility."""
    from .core import captures as core_captures

    click.echo(core_captures.render_inspection(core_captures.inspect(capture)))


@nam.command("scan")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@handled
def nam_scan(directory: Path) -> None:
    """Summarise every capture in a directory."""
    from .core import captures as core_captures

    click.echo(core_captures.render_scan(core_captures.scan(directory), directory))


@nam.command("rank")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--make", help="e.g. Marshall")
@click.option("--model", "gear_model", help="e.g. JCM800")
@click.option("--tone", "tone_type", help="clean, crunch, overdrive, hi_gain, fuzz")
@handled
def nam_rank(directory: Path, make: str | None, gear_model: str | None, tone_type: str | None) -> None:
    """Rank a local capture library against a tone target."""
    from .core import captures as core_captures

    ranked = core_captures.rank_local(
        directory, gear_make=make, gear_model=gear_model, tone_type=tone_type
    )
    if not ranked:
        click.echo(f"No usable captures under {directory}.")
        return
    for model, score, reasons in ranked[:15]:
        click.echo(f"  {score:5.1f}  {model.name or '(unnamed)'}")
        for reason in reasons:
            click.echo(f"         - {reason}")


@main.group()
def tone3000() -> None:
    """Authenticate with and search TONE3000."""


@tone3000.command("status")
@handled
@click.option("--offline", is_flag=True, help="Do not ask the API who you are.")
def tone3000_status_cmd(offline: bool) -> None:
    """Show whether TONE3000 is usable, and what is missing.

    Confirms the credentials against the API unless --offline, because a
    token the server will not accept looks identical on disk to one it will.
    """
    from .core import captures as core_captures

    status = (
        core_captures.auth_status() if offline else _run(core_captures.identify())
    )
    click.echo(status.render())


@tone3000.command("login")
@click.option("--port", default=0, show_default=True, help="Callback port. 0 picks a free one.")
@click.option("--no-browser", is_flag=True, help="Print the URL instead of opening a browser.")
@click.option("--timeout", default=300.0, show_default=True, help="Seconds to wait.")
@handled
def tone3000_login(port: int, no_browser: bool, timeout: float) -> None:
    """Log in to TONE3000 via OAuth.

    Opens a browser and catches the redirect on a loopback listener, so
    nothing has to be pasted by hand. Requires TONE3000_CLIENT_ID.
    """
    from .core import captures as core_captures

    def show(url: str) -> None:
        click.echo("Open this URL to authorise rigsmith:\n")
        click.echo(f"  {url}\n")
        click.echo("Waiting for the redirect...")

    core_captures.login(
        port=port, timeout_s=timeout, open_browser=not no_browser, on_url=show
    )
    # Ask who that was, so "authenticated" reads as "authenticated as someone".
    status = _run(core_captures.identify())
    click.echo(click.style("\n" + status.render(), fg="green"))


@tone3000.command("search")
@click.argument("query", default="")
@click.option("--make", help="Manufacturer filter.")
@click.option("--architecture", default="2", type=click.Choice(["1", "2", "any"]), show_default=True)
@click.option("--size", "sizes", multiple=True, help="e.g. lite. Repeatable.")
@click.option("--calibrated", is_flag=True, help="Only captures with dBu levels.")
@click.option("--limit", default=15, show_default=True)
@handled
def tone3000_search(query, make, architecture, sizes, calibrated, limit) -> None:
    """Search TONE3000 for NAM captures."""
    from .core import captures as core_captures

    tones = _run(
        core_captures.search(
            query, make=make, architecture=architecture,
            sizes=list(sizes) or None, calibrated=calibrated, limit=limit,
        )
    )
    click.echo(core_captures.render_search(tones, query))


@tone3000.command("download")
@click.argument("tone_id")
@click.option("--architecture", default="2", type=click.Choice(["1", "2", "any"]), show_default=True)
@click.option("--out", type=click.Path(path_type=Path), help="Where to put the files.")
@click.option("--limit", default=4, show_default=True, help="Cap on models from one tone.")
@click.option(
    "--any-size",
    is_flag=True,
    help="Keep sizes the Core cannot run - still usable for offline rendering.",
)
@click.option("--install", is_flag=True, help="Copy them to the device over USB afterwards.")
@handled
def tone3000_download(tone_id, architecture, out, limit, any_size, install) -> None:
    """Download a tone's captures, then check each against its own contents.

    Search tells you what TONE3000 believes about a capture. This reads the
    file, because those two can disagree - the architecture field is metadata
    somebody typed, and the weights are the fact.
    """
    from .core import captures as core_captures
    from .core import devices as core_devices

    results = _run(
        core_captures.download(
            tone_id,
            architecture=architecture,
            out_dir=out,
            limit=limit,
            loadable_only=not any_size,
        )
    )
    click.echo(core_captures.render_download(results))

    if install:
        click.echo("")
        for result in results:
            if result.loadable is False:
                click.echo(f"  skipped {result.path.name} - the Core will not load it")
                continue
            click.echo("  " + core_devices.install_capture(result.path))


# ==========================================================================
# your own recordings
# ==========================================================================


@main.group()
def take() -> None:
    """Your own playing: DIs, wet references, loops off the pedal.

    Every comparison here is against a record made on somebody else's guitar.
    The one variable you can remove is the instrument - play the part
    yourself, keep it, and render candidates through that.
    """


@take.command("add")
@click.argument("path", type=click.Path(exists=True, path_type=Path))
@click.option("--id", "take_id", help="Name it. Defaults to the filename.")
@click.option(
    "--kind",
    default="di",
    type=click.Choice(sorted(TAKE_KINDS)),
    show_default=True,
    help="di reamps through anything; everything else is a reference.",
)
@click.option("--origin", default="file", type=click.Choice(sorted(TAKE_ORIGINS)), show_default=True)
@click.option("--guitar", help="Which instrument. The reason this library exists.")
@click.option("--pickup", help="bridge, neck, middle, both.")
@click.option("--strings", help="e.g. '10-46 nickel'.")
@click.option("--tuning", help="e.g. 'E standard', 'drop D'.")
@click.option("--capo", type=int)
@click.option("--note", help="What you played, in your own words.")
@click.option("--tag", "tags", multiple=True)
@click.option("--no-copy", is_flag=True, help="Leave the file where it is.")
@handled
def take_add(path, take_id, kind, origin, guitar, pickup, strings, tuning, capo, note, tags, no_copy) -> None:
    """Add a recording to the library."""
    from .core import takes

    added = takes.add(
        path, take_id=take_id, kind=kind, origin=origin, guitar=guitar,
        pickup=pickup, strings=strings, tuning=tuning, capo=capo, note=note,
        tags=list(tags), copy=not no_copy,
    )
    click.echo(added.render())
    if not guitar:
        click.echo(
            click.style(
                "\nNo guitar recorded. Which instrument it was is the whole "
                "point of keeping these - add it with:  rigsmith take edit "
                f"{added.take_id} --guitar '...'",
                fg="yellow",
            )
        )


@take.command("list")
@click.option("--kind", type=click.Choice(sorted(TAKE_KINDS)))
@click.option("--guitar")
@handled
def take_list(kind, guitar) -> None:
    """List your recordings."""
    from .core import takes

    click.echo(takes.render_list(takes.list_takes(kind=kind, guitar=guitar)))


@take.command("show")
@click.argument("take_id")
@handled
def take_show(take_id) -> None:
    """Show one take in full."""
    from .core import takes

    click.echo(takes.get(take_id).render())


@take.command("edit")
@click.argument("take_id")
@click.option("--kind", type=click.Choice(sorted(TAKE_KINDS)))
@click.option("--guitar")
@click.option("--pickup")
@click.option("--strings")
@click.option("--tuning")
@click.option("--capo", type=int)
@click.option("--note")
@handled
def take_edit(take_id, **changes) -> None:
    """Change a take's labels. The audio is never touched."""
    from .core import takes

    click.echo(takes.update(take_id, **{k: v for k, v in changes.items() if v is not None}).render())


@take.command("rm")
@click.argument("take_id")
@click.option("--delete-file", is_flag=True, help="Also delete the audio.")
@handled
def take_rm(take_id, delete_file) -> None:
    """Forget a take."""
    from .core import takes

    click.echo(takes.delete(take_id, remove_file=delete_file))


@take.command("record")
@click.option("--seconds", default=15.0, show_default=True)
@click.option("--id", "take_id", help="Name it. Defaults to 'take'.")
@click.option("--guitar", help="Which instrument.")
@click.option("--pickup")
@click.option("--tuning")
@click.option("--note")
@handled
def take_record(seconds, take_id, guitar, pickup, tuning, note) -> None:
    """Record a performance over USB and keep both halves of it.

    The Core sends the wet tone and the dry DI at once. Play the part once;
    the DI then reamps through as many candidates as you like.
    """
    from .core import takes

    _, _, message = takes.record(
        seconds, take_id=take_id, guitar=guitar, pickup=pickup,
        tuning=tuning, note=note,
    )
    click.echo(message)


@take.command("compare")
@click.argument("target", metavar="AUDIO")
@click.option("--nam", "nam_path", type=click.Path(exists=True, path_type=Path),
              help="Render each guitar through this capture first.")
@click.option("--ir", "ir_path", type=click.Path(exists=True, path_type=Path))
@click.option("--region", metavar="START:END", help="Span of the target, or 'auto'.")
@click.option("--take", "take_ids", multiple=True, help="Limit to these takes.")
@handled
def take_compare(target, nam_path, ir_path, region, take_ids) -> None:
    """Rank your instruments against a target, one variable at a time.

    The capture sweep answers "which amp gets closest". This answers the
    question underneath it: which guitar should you be holding. Same rig,
    same target, and the one difference no amount of capture hunting fixes.
    """
    from .core import matching

    results = matching.compare_guitars(
        target,
        nam_path=nam_path,
        ir_path=ir_path,
        take_ids=list(take_ids) or None,
        target_region=region,
    )
    click.echo(
        matching.render_guitar_comparison(
            results, through_a_rig=bool(nam_path or ir_path)
        )
    )


@take.command("pull")
@click.option("--guitar", help="Which instrument these were played on.")
@click.option("--pickup")
@click.option("--kind", default="loop", type=click.Choice(sorted(TAKE_KINDS)), show_default=True)
@click.option("--list", "listing", is_flag=True, help="Show what is there without importing.")
@click.option("--only", multiple=True, help="Import only these filenames.")
@handled
def take_pull(guitar, pickup, kind, listing, only) -> None:
    """Pull recordings off the device's USB storage.

    Put the Core into Menu > USB Transfer first. Whatever the pedal recorded
    has already been through the pedal, so these arrive as loops rather than
    DIs - reamping through one would stack two amps.
    """
    from .core import devices

    if listing:
        volume, found = devices.list_device_audio()
        click.echo(devices.render_device_audio(volume, found))
        return

    imported, skipped = devices.import_device_audio(
        kind=kind, guitar=guitar, pickup=pickup, only=list(only) or None
    )
    click.echo(devices.render_import(imported, skipped))


# ==========================================================================
# tone matching
# ==========================================================================


@main.group()
def match() -> None:
    """Test a rig against the source recording."""


@match.command("devices")
@handled
def match_devices() -> None:
    """List audio interfaces, flagging the HeadRush."""
    from .core import matching

    click.echo(matching.render_audio_devices())


@match.command("isolate")
@click.argument("audio", type=click.Path(exists=True, path_type=Path))
@click.option("--out", type=click.Path(path_type=Path))
@handled
def match_isolate(audio: Path, out: Path | None) -> None:
    """Separate a guitar stem from a mixed recording (slow)."""
    from .core import matching

    with click.progressbar(length=100, label="separating") as bar:
        state = {"last": 0}

        class Reporter:
            def update(self, fraction=None, message=""):
                if fraction is not None:
                    step = int(fraction * 100) - state["last"]
                    if step > 0:
                        bar.update(step)
                        state["last"] += step

            def log(self, message):
                pass

            @property
            def cancelled(self):
                return False

            def check(self):
                pass

        result = matching.isolate_guitar(audio, out_dir=out, progress=Reporter())
    click.echo(result.describe())


@match.command("capture-di")
@click.option("--seconds", default=15.0, show_default=True)
@click.option("--name", default="take", show_default=True)
@handled
def match_capture_di(seconds: float, name: str) -> None:
    """Record one performance: the wet tone and the dry DI together."""
    from .core import matching

    click.echo(f"Recording {seconds:.0f}s - play now.")
    capture, paths = matching.capture_di(seconds, name=name)
    click.echo(capture.describe())
    for kind, path in paths.items():
        click.echo(f"{kind}: {path}")


@match.command("reamp")
@click.argument("di", metavar="AUDIO")
@click.option("--name", default="candidate", show_default=True)
@handled
def match_reamp(di: Path, name: str) -> None:
    """Play a DI through the Core's loaded rig and record the result."""
    from .core import matching

    click.echo(matching.reamp(di, label=name).render())


@match.command("render")
@click.argument("di", metavar="AUDIO")
@click.option("--nam", "nam_path", type=click.Path(exists=True, path_type=Path))
@click.option("--ir", "ir_path", type=click.Path(exists=True, path_type=Path))
@click.option("--truncate", type=int, help="Shorten the IR, e.g. 1024.")
@click.option("--name", default="candidate", show_default=True)
@handled
def match_render(di, nam_path, ir_path, truncate, name) -> None:
    """Render a DI through a capture and IR offline, no hardware."""
    from .core import matching

    click.echo(
        matching.render_offline(
            di, nam_path=nam_path, ir_path=ir_path, ir_truncate=truncate, label=name
        ).render()
    )


@match.command("search")
@click.argument("di", metavar="AUDIO")
@click.argument("target", metavar="AUDIO")
@click.argument("captures_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--ir", "ir_path", type=click.Path(exists=True, path_type=Path))
@click.option("--mode", default="distribution", type=click.Choice(["aligned", "distribution"]), show_default=True)
@click.option(
    "--region",
    metavar="START:END",
    help="Span of the target to rank against (41.2:58.0), or 'auto'.",
)
@click.option(
    "--di-region",
    metavar="START:END",
    help="The matching span of the DI. One span fits every render of it.",
)
@handled
def match_search(di, target, captures_dir, ir_path, mode, region, di_region) -> None:
    """Render a DI through every capture in a directory and rank them.

    The whole offline loop in one command, with no hardware needed. DI and
    TARGET accept a take id as readily as a path - so once the library holds
    your playing, this is a name rather than a filename.
    """
    from .core import matching

    found = {p.stem: p for p in sorted(Path(captures_dir).glob("*.nam"))}
    if not found:
        click.echo(f"No .nam files under {captures_dir}.")
        sys.exit(1)

    click.echo(f"Rendering {len(found)} candidate(s)...")
    with click.progressbar(length=100, label="rendering") as bar:
        state = {"last": 0}

        class Reporter:
            def update(self, fraction=None, message=""):
                if fraction is not None:
                    step = int(fraction * 100) - state["last"]
                    if step > 0:
                        bar.update(step)
                        state["last"] += step

            def log(self, message):
                pass

            @property
            def cancelled(self):
                return False

            def check(self):
                pass

        result = matching.run_offline_search(
            di,
            target,
            found,
            ir_path=ir_path,
            mode=mode,
            target_region=region,
            candidate_region=di_region if di_region else (region if mode == "aligned" else None),
            progress=Reporter(),
        )
    click.echo("")
    click.echo(result.render())


@match.command("score")
@click.argument("target", metavar="AUDIO")
@click.argument("candidates", nargs=-1, required=True, metavar="AUDIO...")
@click.option("--mode", default="distribution", type=click.Choice(["aligned", "distribution"]), show_default=True)
@click.option(
    "--region",
    metavar="START:END",
    help="Span of the target to score, in seconds (41.2:58.0), or 'auto' to use its busiest passage.",
)
@click.option(
    "--candidate-region",
    metavar="START:END",
    help="The matching span of each candidate, when it sits elsewhere in time.",
)
@handled
def match_score(
    target: Path,
    candidates: tuple[Path, ...],
    mode: str,
    region: str | None,
    candidate_region: str | None,
) -> None:
    """Score rendered candidates against a target and rank them.

    A whole track averages its clean intro together with its solo, so
    --region is worth reaching for whenever the passage you care about is
    only part of the file.
    """
    from .core import matching

    if mode == "aligned" and region and not candidate_region:
        # Aligned mode is the same performance on both sides, so one span
        # describes both. Distribution mode is not - the solo sits at 2:41 of
        # the record and 0:04 of the DI - so there the default stays "all".
        candidate_region = region

    scores = matching.score(
        target,
        {_audio_label(c): c for c in candidates},
        mode=mode,
        target_region=region,
        candidate_region=candidate_region,
    )
    click.echo(matching.render_ranking(scores))


@match.command("explain")
@click.argument("target", metavar="AUDIO")
@click.argument("candidate", metavar="AUDIO")
@click.option("--region", metavar="START:END", help="Span of the target to compare (41.2:58.0), or 'auto'.")
@click.option("--candidate-region", metavar="START:END")
@handled
def match_explain(target, candidate, region, candidate_region) -> None:
    """Say where a candidate differs from the target, band by band.

    The natural follow-up to `match score`. A composite says which candidate
    is closest; this says what is still wrong with it, in the terms an amp's
    controls are labelled in.
    """
    from .core import matching

    differences = matching.compare_bands(
        target,
        candidate,
        target_region=region,
        candidate_region=candidate_region,
    )
    click.echo(matching.render_bands(differences, label=_audio_label(candidate)))


@match.group("ab")
def match_ab() -> None:
    """Blind A/B listening - the arbiter."""


@match_ab.command("start")
@click.argument("session_id")
@click.argument("target_label")
@click.argument("candidates", nargs=-1, required=True, metavar="AUDIO...")
@click.option("--pairs", type=int, help="Cap the number of trials.")
@handled
def ab_start(session_id, target_label, candidates, pairs) -> None:
    """Start a blind listening test over candidate renders."""
    from .core import matching

    session = matching.create_ab(
        session_id,
        target_label,
        {_audio_label(c): str(c) for c in candidates},
        pairs=pairs,
    )
    click.echo(f"Created '{session_id}' with {len(session.trials)} trial(s).\n")

    leaks = matching.ab_leaks(session)
    if leaks:
        click.echo(click.style(
            f"  WARNING: {len(leaks)} clip(s) could not be copied to a blinded "
            "name, so their paths still identify them. Treat this session's "
            "result with suspicion.", fg="yellow"))
        click.echo("")

    click.echo(session.render_next())


@match_ab.command("list")
@handled
def ab_list() -> None:
    """List listening sessions."""
    from .core import matching

    sessions = matching.list_ab_sessions()
    if not sessions:
        click.echo("No A/B sessions.")
        return
    for session_id in sessions:
        session = matching.load_ab(session_id)
        state = "complete" if session.complete else "in progress"
        click.echo(
            f"  {session_id:24} {session.answered_count}/{len(session.trials)} "
            f"trials, {state}"
        )


@match_ab.command("next")
@click.argument("session_id")
@handled
def ab_next(session_id: str) -> None:
    """Show the next unanswered trial."""
    from .core import matching

    click.echo(matching.load_ab(session_id).render_next())


@match_ab.command("answer")
@click.argument("session_id")
@click.argument("trial_id", type=int)
@click.argument("verdict", type=click.Choice(["left", "right", "tie"]))
@click.option("--confidence", type=click.IntRange(1, 5))
@click.option("--comment")
@handled
def ab_answer(session_id, trial_id, verdict, confidence, comment) -> None:
    """Record a verdict for one trial."""
    from .core import matching

    session = matching.answer_ab(
        session_id, trial_id, verdict, confidence=confidence, comment=comment
    )
    if session.complete:
        click.echo(session.render_standings())
    else:
        click.echo(session.render_next())


@match_ab.command("results")
@click.argument("session_id")
@handled
def ab_results_cmd(session_id: str) -> None:
    """Unblind a session and show the standings."""
    from .core import matching

    click.echo(matching.load_ab(session_id).render_standings())


# ==========================================================================
# firmware, workspace, jobs
# ==========================================================================


@main.group()
def firmware() -> None:
    """Read-only static analysis of a firmware image.

    Never writes to, patches, or reflashes a device - these commands only
    open local files.
    """


@firmware.command("analyse")
@click.argument("image", type=click.Path(exists=True, path_type=Path))
@click.option("--unpack-to", type=click.Path(path_type=Path))
@click.option("--strings", "mine", is_flag=True, help="Mine extracted files for catalog strings.")
@handled
def firmware_analyse(image: Path, unpack_to: Path | None, mine: bool) -> None:
    """Identify a firmware image's container and classify what is inside."""
    from .protocol.firmware import analyse_firmware, mine_catalog_strings

    report = analyse_firmware(image, unpack_to=unpack_to)
    click.echo(report.render())
    if mine and report.files:
        click.echo("\nCatalog string leads:")
        for hint, values in mine_catalog_strings(report.files).items():
            click.echo(f"  [{hint}] {len(values)} match(es)")
            for value in values[:5]:
                click.echo(f"    {value[:100]}")


@main.group()
def recipe() -> None:
    """Research a tone, then compile it into a rig."""


@recipe.command("new")
@click.argument("title")
@click.option("--artist")
@click.option("--album")
@click.option("--year", type=int)
@click.option("--summary", help="What the tone actually sounds like.")
@click.option("--guitar")
@click.option("--tuning")
@handled
def recipe_new(title, artist, album, year, summary, guitar, tuning) -> None:
    """Start a tone recipe for a specific recording."""
    from .core import recipes

    recipe_id, obj = recipes.create(
        title, artist=artist, album=album, year=year,
        summary=summary, guitar=guitar, tuning=tuning,
    )
    click.echo(f"Created recipe '{recipe_id}' for {obj.title}.")
    click.echo(
        "\nAdd gear with:\n"
        f"  rigsmith recipe gear {recipe_id} amp \"Marshall JMP\" "
        "--confidence reported --source \"Guitar World interview\""
    )


@recipe.command("list")
@handled
def recipe_list() -> None:
    """List saved recipes."""
    from .core import recipes

    found = recipes.list_recipes()
    if not found:
        click.echo("No recipes. Start one with:  rigsmith recipe new \"Song name\"")
        return
    for recipe_id, obj in found:
        click.echo(
            f"  {recipe_id:28} {obj.title:28} "
            f"{len(obj.gear)} gear, confidence {obj.confidence:.0%}"
        )


@recipe.command("show")
@click.argument("recipe_id")
@handled
def recipe_show(recipe_id: str) -> None:
    """Show a recipe in full, with sources and confidence."""
    from .core import recipes

    click.echo(recipes.get(recipe_id).render())


@recipe.command("gear")
@click.argument("recipe_id")
@click.argument("role")
@click.argument("name")
@click.option("--category", default="effect",
              type=click.Choice(["amp", "cab", "ir", "clone", "nam", "effect"]), show_default=True)
@click.option("--confidence", default="guessed",
              type=click.Choice(["stated", "reported", "inferred", "guessed"]), show_default=True)
@click.option("--make")
@click.option("--model")
@click.option("--source", "source_title", help="Required for stated/reported.")
@click.option("--source-url")
@click.option("--source-note", help="What the source actually says.")
@click.option("--param", "params", multiple=True, metavar="KEY=VALUE")
@click.option("--alt", "alternatives", multiple=True, help="Other candidates.")
@click.option("--note")
@handled
def recipe_gear(recipe_id, role, name, category, confidence, make, model,
                source_title, source_url, source_note, params, alternatives, note) -> None:
    """Record one element of the chain and the evidence for it.

    stated and reported require a source - they claim the choice is
    documented, and an undocumented claim dressed as one is worse than an
    honest guess.
    """
    from .core import recipes

    settings = {}
    for item in params:
        if "=" not in item:
            raise click.BadParameter(f"--param must be KEY=VALUE, got '{item}'")
        key, _, value = item.partition("=")
        try:
            settings[key] = json.loads(value)
        except json.JSONDecodeError:
            settings[key] = value

    obj = recipes.add_gear(
        recipe_id, role, name, category=category, make=make, model=model,
        settings=settings, confidence=confidence, source_title=source_title,
        source_url=source_url, source_note=source_note,
        alternatives=list(alternatives), note=note,
    )
    click.echo(obj.role(role).render())
    click.echo("\n" + obj.confidence_report())


@recipe.command("scene")
@click.argument("recipe_id")
@click.argument("name")
@click.option("--on", "roles_on", multiple=True, help="Gear roles active. Repeatable.")
@click.option("--off", "roles_off", multiple=True)
@click.option("--fs", "footswitch", type=int)
@handled
def recipe_scene(recipe_id, name, roles_on, roles_off, footswitch) -> None:
    """Describe a part of the song by which gear is active."""
    from .core import recipes

    obj = recipes.add_scene(
        recipe_id, name, roles_on=list(roles_on),
        roles_off=list(roles_off), footswitch=footswitch,
    )
    click.echo(next(s for s in obj.scenes if s.name == name).render())


@recipe.command("question")
@click.argument("recipe_id")
@click.argument("question")
@handled
def recipe_question(recipe_id: str, question: str) -> None:
    """Record something the research could not establish."""
    from .core import recipes

    obj = recipes.add_question(recipe_id, question)
    click.echo(f"Noted. {len(obj.open_questions)} open question(s).")


@recipe.command("measured")
@click.argument("recipe_id")
@click.argument("label")
@click.argument("target")
@click.option("--kind", default="score", type=click.Choice(["score", "listen"]), show_default=True)
@click.option("--mode", type=click.Choice(["aligned", "distribution"]))
@click.option("--composite", type=float, help="The composite score, for --kind score.")
@click.option("--region", help="The passage compared, if not the whole file.")
@click.option("--won/--lost", default=None, help="Did it come first?")
@click.option("--trials", type=int, help="How many A/B trials informed a verdict.")
@click.option("--note")
@handled
def recipe_measured(recipe_id, label, target, kind, mode, composite, region, won, trials, note) -> None:
    """Record what testing this recipe actually produced.

    Confidence says how well-sourced a claim is. This says whether acting on
    it worked - a different question, and the one a recipe cannot answer on
    its own. Only a listening verdict marks a recipe verified; a metric
    result is a ranking.
    """
    from .core import recipes

    obj = recipes.record_measurement(
        recipe_id, label, target, kind=kind, mode=mode, composite=composite,
        region=region, won=won, trials=trials, note=note,
    )
    click.echo(f"Recorded: {obj.measurements[-1].describe()}")
    click.echo(
        click.style("This recipe is verified by ear.", fg="green")
        if obj.verified
        else "Still unverified - only a blind listening verdict settles it."
    )


@recipe.command("resolve")
@click.argument("recipe_id")
@click.argument("role")
@click.argument("model_id")
@handled
def recipe_resolve(recipe_id: str, role: str, model_id: str) -> None:
    """Bind a gear role to a real device model or NAM capture."""
    from .core import recipes

    recipes.resolve_gear(recipe_id, role, model_id)
    click.echo(f"'{role}' now resolves to {model_id}.")


@recipe.command("compile")
@click.argument("recipe_id")
@click.option("--name", help="Rig name, if different from the recipe title.")
@handled
def recipe_compile(recipe_id: str, name: str | None) -> None:
    """Build a draft rig from a recipe."""
    from .core import recipes

    click.echo(recipes.compile_to_draft(recipe_id, name=name).render())


@recipe.command("delete")
@click.argument("recipe_id")
@click.option("--yes", is_flag=True)
@handled
def recipe_delete(recipe_id: str, yes: bool) -> None:
    """Delete a saved recipe."""
    from .core import recipes

    if not yes and not click.confirm(f"Delete recipe '{recipe_id}'?", default=False):
        return
    click.echo(recipes.delete(recipe_id))


@main.group()
def protocol() -> None:
    """Capture the device's undocumented Remote Editor protocol."""


@protocol.command("maps")
@click.argument("name", required=False)
@handled
def protocol_maps_cmd(name: str | None) -> None:
    """Show the protocol maps rigsmith knows how to speak.

    A map expresses one device and firmware's API as data, so the client can
    be finished before the real protocol is known. Unverified maps are
    invented or inferred and say so.
    """
    from .protocol import map as protocol_map

    if name:
        click.echo(protocol_map.get(name).render())
        return

    found = protocol_map.available()
    if not found:
        click.echo("No protocol maps installed.")
        return
    for candidate in found:
        mark = (click.style("verified", fg="green") if candidate.verified
                else click.style("unverified", fg="yellow"))
        click.echo(
            f"  {candidate.name:14} firmware {candidate.firmware or '?':12} "
            f"{len(candidate.operations):2} ops  {mark}"
        )
    click.echo("\nInspect one with:  rigsmith protocol maps <name>")


@protocol.command("simulate")
@click.option("--host", default="127.0.0.1", show_default=True)
@click.option("--port", default=0, show_default=True, help="0 picks a free port.")
@click.option("--replay", type=click.Path(exists=True, path_type=Path),
              help="Serve a captured traffic.jsonl instead of the invented API.")
@handled
def protocol_simulate(host: str, port: int, replay: Path | None) -> None:
    """Run a stand-in HeadRush, so the device path works without hardware.

    This is NOT the real protocol - nobody has captured that yet. It is an
    invented API of roughly the right shape, so discovery, probing, catalog
    extraction and recording can all be exercised today.

    Point the other commands at it with --device, for example:

      rigsmith --device 127.0.0.1:8100 probe --dump-app

    With --replay it serves a session captured from a real device, at which
    point it stops being a guess and becomes a faithful fixture.
    """
    from .protocol.simulator import run_simulator

    def ready(url: str) -> None:
        click.echo(f"Simulated HeadRush on {url}")
        if not replay:
            click.echo(click.style(
                "  This is an invented API, not the real device protocol.",
                fg="yellow"))
        address = url.replace("http://", "")
        click.echo(f"\n  rigsmith --device {address} probe --dump-app")
        click.echo("\n  Ctrl-C to stop.\n")

    run_simulator(host=host, port=port, recording=replay, on_ready=ready)


@protocol.command("record")
@click.option("--upstream", help="Device base URL. Discovered if omitted.")
@click.option("--port", default=0, show_default=True, help="0 picks a free port.")
@click.option("--out", type=click.Path(path_type=Path), help="Where to write the log.")
@click.option("--no-browser", is_flag=True)
@handled
def protocol_record(upstream: str | None, port: int, out: Path | None, no_browser: bool) -> None:
    """Proxy the Remote Editor and log every request it makes.

    Open the printed address instead of the device's own, use the editor
    normally, then stop with Ctrl-C. Doing one action at a time makes it
    obvious which calls that action produces.
    """
    from .core import devices
    from .protocol.recorder import record

    if not upstream:
        upstream = _run(devices.find()).base_url

    click.echo(f"Proxying {upstream}")

    def ready(url: str) -> None:
        click.echo(f"\nOpen {url} and use the editor as normal.")
        click.echo("Press Ctrl-C when you have captured enough.\n")

    recording = record(
        upstream, port=port, out_dir=out, open_browser=not no_browser, on_ready=ready
    )
    click.echo("\n" + recording.summary())


@main.group()
def catalog() -> None:
    """Extract and inspect the device's model catalog."""


@catalog.command("extract")
@click.option("--capture", "capture_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="An archived Remote Editor bundle from 'rigsmith probe --dump-app'.")
@click.option("--firmware", "firmware_dir", type=click.Path(exists=True, file_okay=False, path_type=Path),
              help="An unpacked firmware tree from 'rigsmith firmware analyse --unpack-to'.")
@click.option("--out", type=click.Path(path_type=Path), help="Where to save the catalog.")
@handled
def catalog_extract(capture_dir: Path | None, firmware_dir: Path | None, out: Path | None) -> None:
    """Harvest the real model catalog from a bundle and/or firmware.

    Until a catalog is extracted, validation runs in lenient unverified mode
    and cannot check model ids or parameter ranges.
    """
    from .protocol.catalog_extract import extract

    if not capture_dir and not firmware_dir:
        raise click.UsageError(
            "give --capture, --firmware, or both.\n"
            "Get a capture with:   rigsmith probe --dump-app\n"
            "Get firmware with:    rigsmith firmware analyse IMAGE --unpack-to DIR"
        )
    click.echo(extract(capture_dir=capture_dir, firmware_dir=firmware_dir, save_to=out).render())


@catalog.command("status")
@handled
def catalog_status() -> None:
    """Show how complete the current model catalog is."""
    from .rig.catalog import get_catalog

    cat = get_catalog(reload=True)
    click.echo(f"source: {cat.source}")
    click.echo(f"models: {len(cat.models)}")
    click.echo(f"verified: {cat.is_verified}")
    if cat.captured_at:
        click.echo(f"captured: {cat.captured_at}")
    click.echo("")
    for line in cat.completeness_report():
        click.echo(line)
    for warning in cat.warnings[:5]:
        click.echo(click.style(f"  {warning}", fg="yellow"))


@main.group()
def workspace() -> None:
    """Inspect and clean the rigsmith workspace."""


@workspace.command("status")
@click.option("--json", "as_json", is_flag=True)
@handled
def workspace_status(as_json: bool) -> None:
    """Show what has accumulated in the workspace."""
    from .core.workspace import inspect as inspect_workspace

    report = inspect_workspace()
    _emit(report.render(), as_json=as_json, payload=report.to_dict())


@workspace.command("prune")
@click.argument("areas", nargs=-1)
@click.option("--older-than", type=float, metavar="DAYS")
@click.option("--apply", is_flag=True, help="Actually delete. Without this it is a dry run.")
@handled
def workspace_prune(areas: tuple[str, ...], older_than: float | None, apply: bool) -> None:
    """Clean up workspace content. Dry run unless --apply is given."""
    from .core.workspace import prune

    result = prune(list(areas) or None, older_than_days=older_than, dry_run=not apply)
    click.echo(result.render())


@main.group()
def jobs() -> None:
    """Background jobs."""


@jobs.command("list")
@click.option("--active", is_flag=True, help="Only jobs still running.")
@handled
def jobs_list(active: bool) -> None:
    """List background jobs."""
    from .core.jobs import get_manager

    found = get_manager().list_jobs(active_only=active)
    if not found:
        click.echo("No jobs.")
        return
    for job in found:
        click.echo(f"  {job.id}  {job.render()}")


@jobs.command("show")
@click.argument("job_id")
@handled
def jobs_show(job_id: str) -> None:
    """Show one job's state and result."""
    from .core.jobs import get_manager

    click.echo(get_manager().get(job_id).render())


if __name__ == "__main__":
    main()

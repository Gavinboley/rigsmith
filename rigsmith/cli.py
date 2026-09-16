"""Command line interface for rigsmith.

Mirrors the MCP tool surface so anything an agent can do, a human can do too -
and so the protocol work can be driven before the MCP server exists.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import click

from . import __version__
from .config import Settings, get_settings, set_settings


def _run(coro):
    return asyncio.run(coro)


def _echo_device(info, index: int | None = None) -> None:
    prefix = f"  [{index}] " if index is not None else "  "
    mark = click.style("OK", fg="green") if info.confirmed else click.style("??", fg="yellow")
    click.echo(f"{prefix}{mark} {info.describe()}")
    if info.evidence:
        click.echo(f"       matched: {', '.join(info.evidence)}")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="rigsmith")
@click.option("--device", "device_host", metavar="HOST", help="Device host or IP, skipping discovery.")
@click.option("--workspace", type=click.Path(path_type=Path), help="Override the rigsmith workspace directory.")
@click.option("--readonly", is_flag=True, help="Disable every write for this invocation.")
def main(device_host: str | None, workspace: Path | None, readonly: bool) -> None:
    """AI tone authoring for the HeadRush Core.

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


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


@main.command()
@click.option("--no-sweep", is_flag=True, help="Skip the subnet sweep fallback.")
@click.option("--timeout", default=2.0, show_default=True, help="Per-host HTTP timeout, seconds.")
@click.option("--zeroconf-timeout", default=4.0, show_default=True, help="How long to browse mDNS.")
@click.option("--json", "as_json", is_flag=True, help="Emit JSON instead of text.")
def discover(no_sweep: bool, timeout: float, zeroconf_timeout: float, as_json: bool) -> None:
    """Find the HeadRush on the local network.

    Tries the documented .local hostname first, then mDNS service browsing,
    then a sweep of the local subnet.
    """
    from dataclasses import asdict

    from .device.discovery import discover as run_discover

    devices = _run(
        run_discover(
            include_sweep=not no_sweep,
            timeout=timeout,
            zeroconf_timeout=zeroconf_timeout,
        )
    )

    if as_json:
        click.echo(json.dumps([asdict(d) for d in devices], indent=2))
        return

    if not devices:
        click.echo(click.style("No devices answered.", fg="red"))
        click.echo(
            "\nChecklist:\n"
            "  1. Core powered on and on the same Wi-Fi network as this machine\n"
            "  2. Global Settings > Wi-Fi > HeadRush Remote = On\n"
            "  3. Same band - a 2.4G/5G split or guest VLAN will hide it\n"
            "  4. No VPN active on this machine\n"
            "  5. Still stuck? Double-tap 'HeadRush Remote' on the device to\n"
            "     read its IP, then rerun with:  rigsmith --device <ip> discover"
        )
        sys.exit(1)

    confirmed = [d for d in devices if d.confirmed]
    click.echo(f"Found {len(devices)} responding host(s), {len(confirmed)} confirmed HeadRush:\n")
    for i, info in enumerate(devices):
        _echo_device(info, i)

    if not confirmed:
        click.echo(
            "\n"
            + click.style("Note: ", fg="yellow")
            + "hosts answered HTTP but none fingerprinted as HeadRush.\n"
            "      If one of the above is the Core, rerun with --device <host>."
        )


# --------------------------------------------------------------------------
# protocol
# --------------------------------------------------------------------------


@main.command()
@click.option("--dump-app", is_flag=True, help="Archive the served bundle alongside the inventory.")
@click.option("--out", type=click.Path(path_type=Path), help="Output directory (default: workspace captures).")
@click.option("--max-assets", default=200, show_default=True, help="Cap on files fetched.")
@click.option("--timeout", default=15.0, show_default=True, help="HTTP timeout, seconds.")
def probe(dump_app: bool, out: Path | None, max_assets: int, timeout: float) -> None:
    """Fetch the Remote Editor bundle and extract its API surface.

    This is read-only: it requests the same files a browser would when you
    open the editor, then mines them for endpoints, WebSocket URLs, and
    message types.
    """
    from datetime import datetime

    from .device.discovery import find_device
    from .protocol.capture import capture

    settings = get_settings()
    device = _run(find_device())
    if device is None:
        click.echo(click.style("No device found. Run 'rigsmith discover' first.", fg="red"))
        sys.exit(1)

    click.echo(f"Probing {device.base_url} ...")

    if out is None:
        settings.ensure_workspace()
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out = settings.captures_dir / stamp

    inventory = _run(
        capture(
            device.base_url,
            out_dir=out,
            max_assets=max_assets,
            timeout=timeout,
            save_assets=dump_app,
        )
    )

    click.echo(f"\n{inventory.summary()}")
    if inventory.device_title:
        click.echo(f"title:    {inventory.device_title}")
    if inventory.firmware:
        click.echo(f"firmware: {inventory.firmware}")

    if inventory.endpoints:
        click.echo("\nEndpoints (strongest evidence first):")
        for ep in inventory.endpoints[:40]:
            methods = ",".join(ep.methods) or "-"
            click.echo(f"  {ep.evidence:8} {methods:16} {ep.path}")
        if len(inventory.endpoints) > 40:
            click.echo(f"  ... {len(inventory.endpoints) - 40} more in inventory.json")

    if inventory.websockets:
        click.echo("\nWebSockets:")
        for url in inventory.websockets:
            click.echo(f"  {url}")

    if inventory.message_types:
        preview = ", ".join(inventory.message_types[:25])
        click.echo(f"\nMessage types: {preview}")

    for note in inventory.notes:
        click.echo("\n" + click.style("note: ", fg="yellow") + note)

    click.echo(f"\nWritten to {out}")


@main.command()
def doctor() -> None:
    """Report what rigsmith can and cannot do in this environment."""
    settings = get_settings()

    click.echo(f"rigsmith {__version__}")
    click.echo(f"workspace: {settings.workspace}")
    click.echo(f"read-only: {settings.readonly}")
    click.echo("")

    checks: list[tuple[str, str, str]] = []

    def probe_import(label: str, module: str, why: str) -> None:
        try:
            __import__(module)
            checks.append(("ok", label, ""))
        except ImportError:
            checks.append(("missing", label, why))

    probe_import("httpx (device HTTP)", "httpx", "required - pip install httpx")
    probe_import("websockets (device WS)", "websockets", "required - pip install websockets")
    probe_import("zeroconf (mDNS discovery)", "zeroconf", "pip install zeroconf - without it, discovery falls back to hostname + sweep")
    probe_import("mcp (MCP server)", "mcp", "pip install 'rigsmith[mcp]' to expose tools to an agent")
    probe_import("mido (MIDI verification)", "mido", "pip install 'rigsmith[midi]' for rig/scene switching and write verification")
    probe_import("sounddevice (hardware reamp)", "sounddevice", "pip install 'rigsmith[audio]' for hardware-in-the-loop tone testing")
    probe_import("soundfile (audio I/O)", "soundfile", "pip install 'rigsmith[audio]'")
    probe_import("scipy (IR convolution)", "scipy", "pip install 'rigsmith[match]' for offline rendering")
    probe_import("playwright (editor fallback)", "playwright", "pip install 'rigsmith[browser]' for the browser-driven fallback transport")

    for state, label, why in checks:
        mark = click.style("  ok  ", fg="green") if state == "ok" else click.style(" --   ", fg="yellow")
        click.echo(f"{mark} {label}")
        if why:
            click.echo(f"        {why}")

    click.echo("")
    from .device.discovery import local_subnets, resolve_hostnames

    hostnames = resolve_hostnames()
    if hostnames:
        for name, addr in hostnames:
            click.echo(f"  mDNS: {name} -> {addr}")
    else:
        click.echo("  mDNS: no HeadRush .local hostname resolves right now")
    click.echo(f"  subnets to sweep: {', '.join(str(n) for n in local_subnets()) or 'none detected'}")


# --------------------------------------------------------------------------
# device storage
# --------------------------------------------------------------------------


@main.group()
def device() -> None:
    """Device storage: backup, restore, and file transfer over USB."""


@device.command("status")
@click.option("--path", type=click.Path(path_type=Path), help="Explicit mount path.")
def device_status(path: Path | None) -> None:
    """Show whether the device's USB storage is mounted, and what is on it."""
    from .device.usbmsc import DeviceStorageError, find_device_volume

    try:
        click.echo(find_device_volume(path).describe())
    except DeviceStorageError as exc:
        click.echo(click.style(str(exc), fg="yellow"))
        sys.exit(1)


@device.command("backup")
@click.option("--path", type=click.Path(path_type=Path), help="Explicit mount path.")
@click.option("--to", "destination", type=click.Path(path_type=Path), help="Backup directory.")
def device_backup(path: Path | None, destination: Path | None) -> None:
    """Copy everything off the device into a timestamped local backup.

    Reads only; the device is not modified.
    """
    from .device.usbmsc import DeviceStorageError, find_device_volume, timestamped_backup

    settings = get_settings()
    settings.ensure_workspace()
    try:
        volume = find_device_volume(path)
        result = timestamped_backup(destination or settings.backups_dir, volume)
    except DeviceStorageError as exc:
        click.echo(click.style(str(exc), fg="yellow"))
        sys.exit(1)
    click.echo(result.describe())


@device.command("install-nam")
@click.argument("nam_file", type=click.Path(exists=True, path_type=Path))
@click.option("--path", type=click.Path(path_type=Path), help="Explicit mount path.")
@click.option("--overwrite", is_flag=True, help="Replace an existing file of the same name.")
def device_install_nam(nam_file: Path, path: Path | None, overwrite: bool) -> None:
    """Copy a NAM capture onto the device over USB."""
    from .device.usbmsc import DeviceStorageError, find_device_volume, install_nam

    try:
        volume = find_device_volume(path)
        click.echo(install_nam(nam_file, volume, overwrite=overwrite))
    except DeviceStorageError as exc:
        click.echo(click.style(str(exc), fg="red"))
        sys.exit(1)


# --------------------------------------------------------------------------
# firmware
# --------------------------------------------------------------------------


@main.group()
def firmware() -> None:
    """Read-only static analysis of a firmware image.

    Never writes to, patches, or reflashes a device - these commands only
    open local files.
    """


@firmware.command("analyse")
@click.argument("image", type=click.Path(exists=True, path_type=Path))
@click.option("--unpack-to", type=click.Path(path_type=Path), help="Extract here, when the container allows it.")
@click.option("--strings", "mine", is_flag=True, help="Also mine extracted files for catalog strings.")
def firmware_analyse(image: Path, unpack_to: Path | None, mine: bool) -> None:
    """Identify an image's container, unpack it, and classify what is inside."""
    from .protocol.firmware import analyse_firmware, mine_catalog_strings

    report = analyse_firmware(image, unpack_to=unpack_to)
    click.echo(report.render())

    if mine and report.files:
        click.echo("\nCatalog string leads:")
        for hint, values in mine_catalog_strings(report.files).items():
            click.echo(f"  [{hint}] {len(values)} match(es)")
            for value in values[:5]:
                click.echo(f"    {value[:100]}")


# --------------------------------------------------------------------------
# NAM captures
# --------------------------------------------------------------------------


@main.group()
def nam() -> None:
    """Inspect Neural Amp Modeler captures."""


@nam.command("inspect")
@click.argument("capture", type=click.Path(exists=True, path_type=Path))
def nam_inspect(capture: Path) -> None:
    """Report a capture's architecture, size, calibration, and compatibility."""
    from .tone.nam import parse_nam

    try:
        model = parse_nam(capture)
    except ValueError as exc:
        click.echo(click.style(str(exc), fg="red"))
        sys.exit(1)

    click.echo(model.describe())
    click.echo(f"\ngeneration: {model.generation.value}")
    for item in model.generation_evidence:
        click.echo(f"  - {item}")
    click.echo(f"size: {model.size or 'unknown'}")
    for item in model.size_evidence:
        click.echo(f"  - {item}")

    verdict = {
        True: click.style("yes", fg="green"),
        False: click.style("NO - the Core does not accept this combination", fg="red"),
        None: click.style("cannot tell - generation or size unknown", fg="yellow"),
    }[model.headrush_compatible]
    click.echo(f"loadable on the Core: {verdict}")


@nam.command("scan")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
def nam_scan(directory: Path) -> None:
    """Summarise every capture in a directory."""
    from .tone.nam import scan_directory

    models = scan_directory(directory)
    if not models:
        click.echo(f"No readable .nam files under {directory}.")
        return

    click.echo(f"{len(models)} capture(s):")
    for model in models:
        click.echo(f"  {model.describe()}")

    a2 = sum(1 for m in models if m.generation.value == "A2")
    unknown = sum(1 for m in models if m.generation.value == "unknown")
    click.echo(f"\nA2: {a2}   A1: {len(models) - a2 - unknown}   undetermined: {unknown}")


# --------------------------------------------------------------------------
# rigs
# --------------------------------------------------------------------------


@main.group()
def rig() -> None:
    """Work with rig drafts."""


@rig.command("list")
def rig_list() -> None:
    """List drafts in progress."""
    from .core.drafts import get_store

    drafts = get_store().list_drafts()
    if not drafts:
        click.echo("No drafts. Create one with the MCP server or 'rigsmith rig new'.")
        return
    for draft in drafts:
        click.echo(
            f"  {draft.draft_id:24} '{draft.rig.name}'  "
            f"{len(draft.rig.blocks)}/14 blocks, {len(draft.rig.scenes)} scenes"
        )


@rig.command("show")
@click.argument("draft_id")
def rig_show(draft_id: str) -> None:
    """Print a draft in full."""
    from .core.drafts import get_store

    try:
        draft = get_store().get(draft_id)
    except KeyError as exc:
        click.echo(click.style(str(exc), fg="red"))
        sys.exit(1)
    click.echo(draft.rig.describe())
    click.echo(f"\nchain: {draft.rig.signal_chain()}")


@rig.command("validate")
@click.argument("draft_id")
def rig_validate(draft_id: str) -> None:
    """Check a draft for problems."""
    from .core.drafts import get_store
    from .rig.validate import validate_rig

    try:
        draft = get_store().get(draft_id)
    except KeyError as exc:
        click.echo(click.style(str(exc), fg="red"))
        sys.exit(1)

    result = validate_rig(draft.rig)
    header = click.style("VALID", fg="green") if result.ok else click.style(
        f"INVALID - {len(result.errors)} error(s)", fg="red"
    )
    click.echo(header)
    click.echo(result.report())
    if not result.ok:
        sys.exit(1)


# --------------------------------------------------------------------------
# MCP server
# --------------------------------------------------------------------------


@main.command()
def serve() -> None:
    """Run the MCP server over stdio.

    Register it with Claude Code:

      claude mcp add rigsmith -- python -m rigsmith.mcp.server
    """
    from .mcp.server import main as run_server

    run_server()


if __name__ == "__main__":
    main()

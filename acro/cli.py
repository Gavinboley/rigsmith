"""Command line interface for acro.

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
@click.version_option(__version__, prog_name="acro")
@click.option("--device", "device_host", metavar="HOST", help="Device host or IP, skipping discovery.")
@click.option("--workspace", type=click.Path(path_type=Path), help="Override the acro workspace directory.")
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
            "     read its IP, then rerun with:  acro --device <ip> discover"
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
        click.echo(click.style("No device found. Run 'acro discover' first.", fg="red"))
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
    """Report what acro can and cannot do in this environment."""
    settings = get_settings()

    click.echo(f"acro {__version__}")
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
    probe_import("mcp (MCP server)", "mcp", "pip install 'acro[mcp]' to expose tools to an agent")
    probe_import("mido (MIDI verification)", "mido", "pip install 'acro[midi]' for rig/scene switching and write verification")
    probe_import("sounddevice (hardware reamp)", "sounddevice", "pip install 'acro[audio]' for hardware-in-the-loop tone testing")
    probe_import("soundfile (audio I/O)", "soundfile", "pip install 'acro[audio]'")
    probe_import("scipy (IR convolution)", "scipy", "pip install 'acro[match]' for offline rendering")
    probe_import("playwright (editor fallback)", "playwright", "pip install 'acro[browser]' for the browser-driven fallback transport")

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


if __name__ == "__main__":
    main()

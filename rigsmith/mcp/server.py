"""The rigsmith MCP server.

Exposes tone research, rig authoring, and device control to an AI agent.

Design notes that matter for how the tools behave:

* Rigs are built as **drafts** across multiple calls, then previewed, then
  written. Nothing reaches the device until ``write_rig`` is called, and that
  step takes a backup and shows a diff first.
* Tools return readable text, because the agent has to reason about the
  result and a wall of JSON is harder to reason about than a signal chain.
* Where a fact is uncertain - an unverified catalog, an inferred NAM
  generation, an estimated CPU load - the tool says so. A confident wrong
  answer about which capture matches a record is the main failure mode this
  project has to avoid.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from .. import __version__
from ..config import ReadOnlyError, get_settings, require_writable
from ..rig.catalog import get_catalog
from ..rig.codec import save_rig
from ..rig.diff import diff_rigs
from ..rig.model import (
    Block,
    BlockCategory,
    Doubling,
    Rig,
    Scene,
    SceneBlockState,
    SignalPath,
)
from ..rig.validate import validate_rig as run_validation
from .session import get_store

INSTRUCTIONS = """\
rigsmith drives a HeadRush Core amp modeler over the local network so you can
recreate specific recorded guitar tones.

Typical flow for "make me the tone from <song>":

 1. Research the gear yourself (web search, interviews, rig rundowns). rigsmith
    does not browse for you - it gives you somewhere to put the conclusion.
 2. search_captures  - find a NAM capture of the amp. The Core runs A2 Full
    and A2 Lite plus all A1 sizes; A2 is preferred.
 3. create_rig       - start a draft.
 4. add_block        - build the chain: drives, amp or NAM, cab or IR, time FX.
 5. configure_scene  - map verse/chorus/lead to footswitches 1-5.
 6. validate_rig     - catch slot, CPU, and scene problems before writing.
 7. preview_changes  - see exactly what will change on the hardware.
 8. write_rig        - takes a backup, then writes.

Two things to keep in mind:

* The model catalog may be unverified until it has been extracted from a real
  device. When it is, model ids are suggestions, not guarantees - say so.
* A rig that scores well on metrics is not necessarily the right tone. Offer
  the listening test rather than declaring victory from a number.
"""


def _server():
    """Build the MCP server object, supporting both SDK generations."""
    try:  # MCP SDK v2
        from mcp.server.mcpserver import MCPServer

        return MCPServer(
            name="rigsmith",
            version=__version__,
            instructions=INSTRUCTIONS,
        )
    except ImportError:
        try:  # MCP SDK v1
            from mcp.server.fastmcp import FastMCP

            return FastMCP("rigsmith", instructions=INSTRUCTIONS)
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise SystemExit(
                "The MCP SDK is not installed. Run:  pip install 'rigsmith[mcp]'"
            ) from exc


mcp = _server()

try:  # MCP SDK v2 - the only exception type whose message reaches the agent
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - v1 or a future rename

    class ToolError(RuntimeError):  # type: ignore[no-redef]
        """Fallback when the SDK does not provide one."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def surface_errors(func):
    """Re-raise expected failures as ToolError so the agent sees the reason.

    Anything else raised inside a tool is reported by the SDK as a bare
    "Error executing tool <name>", which tells the agent nothing it can act
    on. "slot 2 is already occupied" is a correction; "error" is a dead end.
    """
    import functools
    import inspect

    expected = (ValueError, KeyError, LookupError, ReadOnlyError, OSError)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ToolError:
                raise
            except expected as exc:
                raise ToolError(str(exc)) from exc

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except expected as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def _draft(draft_id: str):
    try:
        return get_store().get(draft_id)
    except KeyError as exc:
        raise ToolError(str(exc).strip("'")) from exc


def _category(value: str) -> BlockCategory:
    try:
        return BlockCategory(value.lower())
    except ValueError:
        options = ", ".join(c.value for c in BlockCategory)
        raise ToolError(f"unknown block category '{value}'. Options: {options}")


def _catalog_caveat() -> str:
    catalog = get_catalog()
    if catalog.is_verified:
        return ""
    return (
        "\n\nNote: the model catalog is UNVERIFIED - it has not been extracted "
        "from a real device, so model ids here are placeholders. Run "
        "'rigsmith catalog extract' against hardware before relying on them."
    )


# --------------------------------------------------------------------------
# device
# --------------------------------------------------------------------------


@mcp.tool()
@surface_errors
async def discover_device(include_sweep: bool = True) -> str:
    """Find the HeadRush on the local network.

    Tries the documented `.local` hostname, then mDNS service browsing, then
    a sweep of the local subnet. The device must be powered on, joined to the
    same Wi-Fi network, with Global Settings > Wi-Fi > HeadRush Remote = On.

    Args:
        include_sweep: Also sweep the local subnet if nothing else answers.
            Slower, but works when mDNS is blocked.
    """
    from ..device.discovery import discover

    devices = await discover(include_sweep=include_sweep)
    if not devices:
        return (
            "No devices answered.\n\n"
            "Check: powered on; same Wi-Fi network and band; "
            "Global Settings > Wi-Fi > HeadRush Remote = On; no VPN active.\n"
            "You can also double-tap 'HeadRush Remote' on the device to read "
            "its IP, then set RIGSMITH_DEVICE to that address."
        )

    lines = [f"{len(devices)} host(s) answered:"]
    for info in devices:
        mark = "CONFIRMED" if info.confirmed else "unconfirmed"
        lines.append(f"  [{mark}] {info.describe()}")
    if not any(d.confirmed for d in devices):
        lines.append(
            "\nNone fingerprinted as HeadRush. If one of these is the Core, "
            "set RIGSMITH_DEVICE to its address."
        )
    return "\n".join(lines)


@mcp.tool()
@surface_errors
async def probe_protocol(max_assets: int = 200) -> str:
    """Fetch the Remote Editor's web bundle and extract its API surface.

    Read-only: requests the same files a browser would when opening the
    editor, then mines them for endpoints, WebSocket URLs, and message types.
    This is how rigsmith learns the device's undocumented protocol.
    """
    from ..device.discovery import find_device
    from ..protocol.capture import capture

    device = await find_device()
    if device is None:
        return "No device found. Run discover_device first."

    settings = get_settings()
    settings.ensure_workspace()
    from datetime import datetime

    out = settings.captures_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    inventory = await capture(device.base_url, out_dir=out, max_assets=max_assets)

    lines = [f"Probed {device.base_url}", inventory.summary()]
    if inventory.firmware:
        lines.append(f"firmware: {inventory.firmware}")
    if inventory.endpoints:
        lines.append("\nEndpoints:")
        for endpoint in inventory.endpoints[:30]:
            methods = ",".join(endpoint.methods) or "-"
            lines.append(f"  {endpoint.evidence:8} {methods:12} {endpoint.path}")
    if inventory.websockets:
        lines.append("\nWebSockets: " + ", ".join(inventory.websockets))
    if inventory.message_types:
        lines.append("\nMessage types: " + ", ".join(inventory.message_types[:30]))
    for note in inventory.notes:
        lines.append(f"\nNote: {note}")
    lines.append(f"\nSaved to {out}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# catalog
# --------------------------------------------------------------------------


@mcp.tool()
@surface_errors
def search_models(
    query: str = "",
    category: str | None = None,
    family: str | None = None,
    limit: int = 30,
) -> str:
    """Search the block catalog for amps, cabs, IRs, and effects.

    Args:
        query: Free text matched against model id, name, and subcategory.
        category: One of amp, cab, ir, clone, nam, effect.
        family: 'headrush' or 'revalver' for amps and cabs.
        limit: Maximum results.
    """
    catalog = get_catalog()
    cat = _category(category) if category else None
    results = catalog.search(query, category=cat, family=family, limit=limit)

    if not results:
        return f"No models matched '{query}'." + _catalog_caveat()

    lines = [f"{len(results)} model(s):"]
    for model in results:
        bits = [f"  {model.id}", f"({model.category.value}"]
        if model.family:
            bits.append(f"/{model.family}")
        if model.subcategory:
            bits.append(f"/{model.subcategory}")
        bits.append(")")
        if not model.verified:
            bits.append("[unverified]")
        lines.append(" ".join(bits).replace("( ", "(").replace(" )", ")"))
    return "\n".join(lines) + _catalog_caveat()


@mcp.tool()
@surface_errors
def describe_model(model_id: str) -> str:
    """Show a model's category, family, and full parameter list with ranges."""
    catalog = get_catalog()
    model = catalog.get(model_id)
    if model is None:
        return (
            f"'{model_id}' is not in the catalog."
            + _catalog_caveat()
            + "\n\nTry search_models to see what is available."
        )

    lines = [
        f"{model.name}  (id: {model.id})",
        f"  category: {model.category.value}"
        + (f" / {model.family}" if model.family else "")
        + (f" / {model.subcategory}" if model.subcategory else ""),
        f"  verified: {model.verified}",
    ]
    if model.cpu_cost is not None:
        lines.append(f"  est. CPU: ~{model.cpu_cost:.0%}")
    if model.params:
        lines.append("  parameters:")
        for param in model.params:
            rng = ""
            if param.minimum is not None or param.maximum is not None:
                rng = f" [{param.minimum}..{param.maximum}]"
            if param.choices:
                rng = f" {{{', '.join(param.choices[:8])}}}"
            unit = f" {param.unit}" if param.unit else ""
            lines.append(
                f"    {param.name:20} {param.kind:6}{rng}{unit}"
                + (f"  default={param.default}" if param.default is not None else "")
            )
    else:
        lines.append("  parameters: unknown (catalog not extracted from a device)")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# rig authoring
# --------------------------------------------------------------------------


@mcp.tool()
@surface_errors
def create_rig(
    name: str,
    signal_path: Literal[
        "straight", "middle_split_3_4_3", "middle_split_5_3_3", "middle_split_7_2_3"
    ] = "straight",
    notes: str | None = None,
) -> str:
    """Start a new rig draft.

    Returns a draft_id used by every other authoring tool. Nothing touches the
    device until write_rig.

    Args:
        name: Rig name as it will appear on the device.
        signal_path: 'straight' for a linear chain, or a middle-split path
            whose digits are the slot allocation (pre, split-per-lane, post).
        notes: Free text kept with the draft, e.g. what tone you are chasing.
    """
    rig = Rig(name=name, signal_path=SignalPath(signal_path), notes=notes)
    draft = get_store().create(rig)
    return (
        f"Created draft '{draft.draft_id}' for rig '{name}' "
        f"({signal_path}, 14 slots free).\n"
        f"Add blocks with add_block(draft_id='{draft.draft_id}', ...)."
    )


@mcp.tool()
@surface_errors
def list_drafts() -> str:
    """List rig drafts in progress, with block and scene counts.

    Drafts persist between sessions, so a rig started earlier is still
    here. Use the draft_id with explain_rig to see one in full.
    """
    drafts = get_store().list_drafts()
    if not drafts:
        return "No drafts. Start one with create_rig."
    lines = [f"{len(drafts)} draft(s):"]
    for draft in drafts:
        lines.append(
            f"  {draft.draft_id:24} '{draft.rig.name}'  "
            f"{len(draft.rig.blocks)}/14 blocks, {len(draft.rig.scenes)} scenes  "
            f"(updated {draft.updated_at})"
        )
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def explain_rig(draft_id: str) -> str:
    """Describe a draft in full: signal chain, blocks, parameters, and scenes."""
    draft = _draft(draft_id)
    lines = [draft.rig.describe(), "", f"chain: {draft.rig.signal_chain()}"]
    if draft.rig.notes:
        lines.append(f"notes: {draft.rig.notes}")

    params = [
        (b.slot, b.model_id, b.params)
        for b in sorted(draft.rig.blocks, key=lambda b: b.slot)
        if b.params
    ]
    if params:
        lines.append("\nparameters:")
        for slot, model_id, values in params:
            rendered = ", ".join(f"{k}={v}" for k, v in sorted(values.items()))
            lines.append(f"  slot {slot} ({model_id}): {rendered}")
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def add_block(
    draft_id: str,
    category: Literal["amp", "cab", "ir", "clone", "nam", "effect"],
    model_id: str,
    slot: int | None = None,
    params: dict[str, Any] | None = None,
    model_name: str | None = None,
    enabled: bool = True,
) -> str:
    """Add a block to a draft rig.

    Blocks run in slot order, so add them in signal-chain order: drives first,
    then the amp or NAM capture, then the cab or IR, then time-based effects.

    Args:
        draft_id: From create_rig.
        category: 'nam' for a Neural Amp Modeler capture, 'clone' for a
            HeadRush Amp Cloner capture, 'ir' for a cabinet impulse response.
        model_id: Catalog id, or the capture name for nam/clone/ir blocks.
        slot: 1-14. Omit to append to the next free slot.
        params: Model-specific parameters. For a NAM block, include
            {"size": "A2 Lite"} when known - it materially changes CPU cost.
        model_name: Human-readable label, if different from model_id.
        enabled: Whether the block starts active.
    """
    draft = _draft(draft_id)
    block = Block(
        slot=slot or 0,
        category=_category(category),
        model_id=model_id,
        model_name=model_name,
        params=dict(params or {}),
        enabled=enabled,
    )
    draft.rig.add_block(block)
    get_store().save(draft)

    result = run_validation(draft.rig)
    lines = [
        f"Added {model_id} to slot {block.slot}.",
        f"chain: {draft.rig.signal_chain()}",
        f"est. CPU: ~{result.estimated_cpu:.0%}",
    ]
    for finding in result.findings:
        if finding.severity in ("error", "warning"):
            lines.append(f"  {finding}")
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def remove_block(draft_id: str, slot: int) -> str:
    """Remove the block in a slot. Scene references to it are cleaned up."""
    draft = _draft(draft_id)
    block = draft.rig.remove_block(slot)
    get_store().save(draft)
    return (
        f"Removed {block.model_id} from slot {slot}.\n"
        f"chain: {draft.rig.signal_chain()}"
    )


@mcp.tool()
@surface_errors
def move_block(draft_id: str, from_slot: int, to_slot: int) -> str:
    """Move a block to a different position in the chain.

    Other blocks shift to close the gap, and scene references follow their
    blocks rather than staying pinned to slot numbers.
    """
    draft = _draft(draft_id)
    block = draft.rig.move_block(from_slot, to_slot)
    get_store().save(draft)
    return (
        f"Moved {block.model_id} to slot {block.slot}.\n"
        f"chain: {draft.rig.signal_chain()}"
    )


@mcp.tool()
@surface_errors
def set_param(draft_id: str, slot: int, name: str, value: float | str | bool) -> str:
    """Set one parameter on one block.

    Use the reserved names 'input_gain_db' and 'output_gain_db' for the
    universal advanced-block-edit gains (-12 to +12 dB); everything else is
    model-specific.
    """
    draft = _draft(draft_id)
    block = draft.rig.block_at(slot)
    if block is None:
        raise ToolError(f"no block in slot {slot}")

    if name in ("input_gain_db", "output_gain_db"):
        setattr(block, name, float(value))
    else:
        block.params[name] = value
    get_store().save(draft)

    result = run_validation(draft.rig)
    problems = [str(f) for f in result.findings if f.severity == "error"]
    message = f"slot {slot} ({block.model_id}): {name} = {value}"
    if problems:
        message += "\n" + "\n".join(problems)
    return message


@mcp.tool()
@surface_errors
def set_block_doubling(
    draft_id: str,
    slot: int,
    mode: Literal["none", "stereo", "switch"],
    second_model_id: str | None = None,
) -> str:
    """Configure 2X doubling on an amp, cab, IR, clone, or NAM slot.

    'stereo' runs both models at once for a wider image and costs twice the
    CPU. 'switch' puts two models in one slot and swaps between them with a
    footswitch.
    """
    draft = _draft(draft_id)
    block = draft.rig.block_at(slot)
    if block is None:
        raise ToolError(f"no block in slot {slot}")

    block.doubling = Doubling(mode)
    block.doubled_model_id = second_model_id if mode != "none" else None
    get_store().save(draft)

    result = run_validation(draft.rig)
    lines = [f"slot {slot}: 2X {mode}" + (f" with {second_model_id}" if second_model_id else "")]
    lines.append(f"est. CPU: ~{result.estimated_cpu:.0%}")
    lines.extend(str(f) for f in result.findings if f.severity == "error")
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def configure_scene(
    draft_id: str,
    name: str,
    on_slots: list[int] | None = None,
    off_slots: list[int] | None = None,
    footswitch: int | None = None,
    presets: dict[str, str] | None = None,
    replace: bool = True,
) -> str:
    """Create or update a Scene - HeadRush's snapshot.

    A scene turns blocks on or off with one footswitch press. Any slot you do
    not list is left alone (No Change), which is how you build a scene that
    only touches what it cares about.

    A single footswitch toggles between at most two scenes.

    Args:
        draft_id: From create_rig.
        name: Scene name, e.g. 'Clean verse'.
        on_slots: Slots to switch on.
        off_slots: Slots to switch off.
        footswitch: 1-5, the switch that activates it.
        presets: {slot: preset_name} to recall when the scene activates.
        replace: Overwrite an existing scene of the same name.
    """
    draft = _draft(draft_id)
    states: dict[int, SceneBlockState] = {}
    for slot in on_slots or []:
        states[int(slot)] = SceneBlockState.ON
    for slot in off_slots or []:
        states[int(slot)] = SceneBlockState.OFF

    scene = Scene(
        name=name,
        states=states,
        presets={int(k): v for k, v in (presets or {}).items()},
        footswitch=footswitch,
    )

    existing = draft.rig.scene_named(name)
    if existing is not None:
        if not replace:
            raise ToolError(f"scene '{name}' already exists; pass replace=True")
        draft.rig.scenes[draft.rig.scenes.index(existing)] = scene
    else:
        draft.rig.scenes.append(scene)
    get_store().save(draft)

    result = run_validation(draft.rig)
    lines = [f"Scene '{name}' set: on={sorted(on_slots or [])} off={sorted(off_slots or [])}"]
    if footswitch:
        lines.append(f"  footswitch {footswitch}")
    lines.extend(str(f) for f in result.findings if f.severity == "error")
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def validate_rig(draft_id: str) -> str:
    """Check a draft for problems before writing it to the device.

    Reports errors (which block a write), warnings (legal but probably
    unintended), and the estimated CPU load. The CPU figure is an estimate
    built on community measurements, not a vendor specification - the
    device's own meter is authoritative.
    """
    draft = _draft(draft_id)
    result = run_validation(draft.rig)
    header = "VALID" if result.ok else f"INVALID - {len(result.errors)} error(s)"
    return f"{header}\n{result.report()}"


@mcp.tool()
@surface_errors
def preview_changes(draft_id: str) -> str:
    """Show exactly what writing this draft would change on the device."""
    draft = _draft(draft_id)
    difference = diff_rigs(draft.baseline, draft.rig)
    result = run_validation(draft.rig)

    lines = [difference.render(), "", f"summary: {difference.summary()}"]
    lines.append(f"est. CPU: ~{result.estimated_cpu:.0%}")
    if not result.ok:
        lines.append(f"\nBLOCKED: {len(result.errors)} error(s) must be fixed first:")
        lines.extend(f"  {f}" for f in result.errors)
    elif result.warnings:
        lines.append("\nwarnings:")
        lines.extend(f"  {f}" for f in result.warnings)
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def export_rig(draft_id: str, path: str | None = None) -> str:
    """Write a draft to a JSON file.

    This is rigsmith's own format, readable and diffable - not the device's
    binary `.rig`. Useful for version control, sharing, and review.
    """
    draft = _draft(draft_id)
    require_writable(f"export draft '{draft_id}'")

    target = Path(path) if path else get_settings().workspace / "exports" / f"{draft_id}.json"
    save_rig(draft.rig, target)
    return f"Exported '{draft.rig.name}' to {target}"


@mcp.tool()
@surface_errors
async def write_rig(draft_id: str, confirm: bool = False) -> str:
    """Write a draft to the device.

    Validates, backs up, and shows a diff first. Requires confirm=True, so
    the diff can be reviewed before anything changes on the hardware.
    """
    draft = _draft(draft_id)
    require_writable(f"write rig '{draft.rig.name}' to the device")

    result = run_validation(draft.rig)
    if not result.ok:
        return (
            f"Refusing to write: {len(result.errors)} error(s).\n"
            + "\n".join(f"  {f}" for f in result.errors)
        )

    difference = diff_rigs(draft.baseline, draft.rig)
    if not confirm:
        return (
            "Not written - review and call again with confirm=True.\n\n"
            f"{difference.render()}\n\n"
            f"summary: {difference.summary()}\n"
            f"est. CPU: ~{result.estimated_cpu:.0%}"
        )

    from ..device.discovery import find_device

    device = await find_device()
    if device is None:
        return (
            "No device found, so nothing was written. The draft is safe.\n"
            "Run discover_device, or export_rig to a file and import it over USB."
        )

    # Back up before touching anything. If storage is not mounted we say so
    # rather than proceeding without a safety net.
    backup_line = ""
    from ..device.usbmsc import DeviceStorageError, find_device_volume, timestamped_backup

    settings = get_settings()
    settings.ensure_workspace()
    try:
        volume = find_device_volume()
        result = timestamped_backup(settings.backups_dir, volume)
        backup_line = f"Backup taken first: {result.file_count} file(s) -> {result.destination}\n\n"
    except DeviceStorageError:
        backup_line = (
            "No backup was taken - the device's USB storage is not mounted "
            "(Menu > USB Transfer). Mount it and rerun to get a safety net "
            "before writing.\n\n"
        )

    return (
        f"{backup_line}"
        "Device write is not implemented yet.\n\n"
        "The Remote Editor protocol has to be captured against real hardware "
        "first - run probe_protocol with the Core on the network, which "
        "records the endpoints this will use. Until then, use export_rig and "
        "import the result manually over USB.\n\n"
        f"Draft is valid and ready:\n{difference.render()}"
    )


# --------------------------------------------------------------------------
# NAM captures
# --------------------------------------------------------------------------


@mcp.tool()
@surface_errors
async def search_captures(
    query: str = "",
    make: str | None = None,
    tags: list[str] | None = None,
    architecture: Literal["1", "2", "any"] = "2",
    sizes: list[str] | None = None,
    calibrated: bool = False,
    limit: int = 15,
) -> str:
    """Search TONE3000 for NAM captures.

    The Core runs A2 Full and A2 Lite plus every A1 size, and A2 captures the
    amp's sag and compression better, so architecture defaults to A2.

    Requires TONE3000 credentials (TONE3000_CLIENT_ID plus a completed
    login). Some endpoints are tier-gated; if the API refuses, that is
    reported rather than worked around.

    Args:
        query: Free text, e.g. 'JCM800 hot'.
        make: Manufacturer filter, e.g. 'Marshall'.
        tags: Tag filters.
        architecture: '2' for A2, '1' for A1, 'any' for the API default.
        sizes: e.g. ['lite'] to keep CPU cost down.
        calibrated: Only captures carrying input/output dBu levels, which can
            be gain-staged properly instead of guessed at.
        limit: Maximum results.
    """
    from ..tone.tone3000 import Tone3000Client, Tone3000Error, load_tokens

    settings = get_settings()
    tokens = load_tokens(settings.credentials_path)
    if tokens is None:
        return (
            "Not authenticated with TONE3000.\n\n"
            "Set TONE3000_CLIENT_ID, then run 'rigsmith tone3000 login'.\n\n"
            "You can also browse TONE3000 directly on the Core's touchscreen - "
            "that path needs no API access and downloads straight to the device."
        )

    client = Tone3000Client(tokens=tokens, client_id=settings.tone3000_client_id)
    try:
        tones = await client.search(
            query,
            architecture=None if architecture == "any" else int(architecture),
            makes=[make] if make else None,
            tags=tags,
            sizes=sizes,
            calibrated=calibrated or None,
            page_size=min(limit, 25),
        )
    except Tone3000Error as exc:
        return f"TONE3000 search failed: {exc}"

    if not tones:
        return f"No captures matched '{query}'."

    lines = [f"{len(tones)} capture(s):"]
    for tone in tones[:limit]:
        lines.append(f"  {tone.describe()}")
    lines.append(
        "\nUse inspect_capture on a downloaded .nam file to confirm its "
        "architecture and calibration before committing to it."
    )
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def inspect_capture(path: str) -> str:
    """Inspect a local `.nam` capture file.

    Reports architecture, generation (A1/A2), size tier, receptive field,
    sample rate, gear metadata, calibration levels, and whether the Core can
    load it - along with the evidence for each inference, since the file
    format does not state the generation outright.
    """
    from ..tone.nam import parse_nam

    try:
        model = parse_nam(path)
    except (ValueError, OSError) as exc:
        return f"Could not read {path}: {exc}"

    lines = [model.describe(), ""]
    lines.append(f"generation: {model.generation.value}")
    for item in model.generation_evidence:
        lines.append(f"  - {item}")
    lines.append(f"size: {model.size or 'unknown'}")
    for item in model.size_evidence:
        lines.append(f"  - {item}")

    compatible = model.headrush_compatible
    verdict = {
        True: "yes",
        False: "NO - the Core does not accept this combination",
        None: "cannot tell - generation or size is unknown",
    }[compatible]
    lines.append(f"loadable on the Core: {verdict}")

    if model.is_calibrated:
        lines.append(
            f"calibrated: input {model.input_level_dbu} dBu, "
            f"output {model.output_level_dbu} dBu"
        )
    else:
        lines.append("calibrated: no - levels will need setting by ear")
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def scan_captures(directory: str) -> str:
    """Parse every `.nam` file in a directory and summarise them.

    Useful against the Core's NAM folder over USB, or a local capture library.
    """
    from ..tone.nam import scan_directory

    models = scan_directory(directory)
    if not models:
        return f"No readable .nam files under {directory}."

    lines = [f"{len(models)} capture(s) in {directory}:"]
    for model in models:
        lines.append(f"  {model.describe()}")

    a2 = sum(1 for m in models if m.generation.value == "A2")
    unknown = sum(1 for m in models if m.generation.value == "unknown")
    lines.append(f"\nA2: {a2}   A1: {len(models) - a2 - unknown}   undetermined: {unknown}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# device storage
# --------------------------------------------------------------------------


@mcp.tool()
@surface_errors
def device_storage_status(path: str | None = None) -> str:
    """Check whether the device's USB storage is mounted, and what is on it.

    Put the device into Menu > USB Transfer with a USB-B cable attached. It
    mounts as a drive named 'HeadRush' holding Rigs, Setlists, Blocks,
    Impulse Responses, Amp Clones, and NAM folders.
    """
    from ..device.usbmsc import DeviceStorageError, find_device_volume

    try:
        volume = find_device_volume(path)
    except DeviceStorageError as exc:
        return str(exc)
    return volume.describe()


@mcp.tool()
@surface_errors
def device_backup(path: str | None = None) -> str:
    """Copy everything off the device into a timestamped local backup.

    Reads only - the device is not modified. Run this before any write; the
    authoring pipeline does it automatically, but taking one by hand before a
    session of experimenting is cheap insurance.
    """
    from ..device.usbmsc import DeviceStorageError, find_device_volume, timestamped_backup

    require_writable("write a device backup")
    settings = get_settings()
    settings.ensure_workspace()

    try:
        volume = find_device_volume(path)
        result = timestamped_backup(settings.backups_dir, volume)
    except DeviceStorageError as exc:
        return str(exc)
    return result.describe()


@mcp.tool()
@surface_errors
def install_capture(nam_path: str, device_path: str | None = None, overwrite: bool = False) -> str:
    """Copy a NAM capture onto the device over USB.

    The capture is checked first: the Core loads A2 Full/Lite and A1
    Standard/Lite/Nano/Feather, and anything outside that is refused rather
    than copied and left to fail silently on the device.

    The on-screen TONE3000 browser is usually easier - it downloads straight
    to the device over Wi-Fi with no computer involved. Use this for captures
    you already have locally.
    """
    from ..device.usbmsc import DeviceStorageError, find_device_volume, install_nam

    require_writable("copy a capture to the device")
    try:
        volume = find_device_volume(device_path)
        return install_nam(nam_path, volume, overwrite=overwrite)
    except (DeviceStorageError, OSError) as exc:
        return str(exc)


# --------------------------------------------------------------------------
# tone matching
# --------------------------------------------------------------------------


@mcp.tool()
@surface_errors
def list_audio_devices() -> str:
    """List audio interfaces, flagging the HeadRush if it is connected.

    Hardware tone testing needs the Core connected over USB-B and exposing
    all six input channels - channel 5 is the dry DI, which is what makes
    reamping a single performance through many candidates possible.
    """
    from ..match.render_device import AudioUnavailable, find_headrush_device, list_devices

    try:
        devices = list_devices()
    except AudioUnavailable as exc:
        return str(exc)

    lines = [f"{len(devices)} audio device(s):"]
    lines.extend(f"  {d.describe()}" for d in devices)

    try:
        headrush = find_headrush_device()
        lines.append(f"\nHeadRush: {headrush.describe()}")
        if not headrush.has_full_channel_layout:
            lines.append(
                "  WARNING: fewer than 5 input channels visible, so the dry DI "
                "is not reachable. Check the Core's USB audio mode."
            )
    except AudioUnavailable as exc:
        lines.append(f"\n{exc}")
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def isolate_guitar(audio_path: str, output_dir: str | None = None) -> str:
    """Separate a guitar stem from a mixed recording, using Demucs htdemucs_6s.

    Slow (minutes per track on CPU) and offline.

    The result is a timbral reference, not a clean target: it still carries
    the record's amp, cab, mic, and mix processing, plus separation
    artefacts. Score against it in distribution mode only.
    """
    from ..match.separate import SeparationUnavailable, separate

    settings = get_settings()
    settings.ensure_workspace()
    destination = Path(output_dir) if output_dir else settings.audio_dir / "stems"

    try:
        result = separate(audio_path, destination)
    except (SeparationUnavailable, FileNotFoundError) as exc:
        return str(exc)
    return result.describe()


@mcp.tool()
@surface_errors
def capture_di(duration_s: float = 15.0, name: str = "take") -> str:
    """Record one performance, capturing the wet tone and the dry DI together.

    Play the part once. The dry DI on channel 5 can then be reamped through
    any number of candidate rigs, each compared against this same wet
    reference - so trying more candidates costs compute, not playing time.

    Needs the Core connected over USB-B.
    """
    from ..match.render_device import AudioUnavailable, capture_performance
    from ..match.render_offline import RenderUnavailable, save_audio

    require_writable("record audio to the workspace")
    settings = get_settings()
    settings.ensure_workspace()

    try:
        capture = capture_performance(duration_s)
    except AudioUnavailable as exc:
        return str(exc)

    lines = [capture.describe()]
    try:
        wet_path = save_audio(
            settings.audio_dir / f"{name}-wet.wav", capture.wet, capture.sample_rate
        )
        lines.append(f"wet: {wet_path}")
        if capture.dry is not None:
            dry_path = save_audio(
                settings.audio_dir / f"{name}-dry.wav", capture.dry, capture.sample_rate
            )
            lines.append(f"dry: {dry_path}")
    except RenderUnavailable as exc:
        lines.append(f"could not save: {exc}")
    return "\n".join(lines)


@mcp.tool()
@surface_errors
def reamp_di(di_path: str, name: str = "candidate") -> str:
    """Play a DI through the Core's currently loaded rig and record the result.

    Round-trip latency is measured and compensated, because a few
    milliseconds of offset is enough to make an aligned comparison report a
    perfect match as a catastrophic one.

    Load the rig you want to test on the device first.
    """
    from ..match.render_device import AudioUnavailable, reamp
    from ..match.render_offline import RenderUnavailable, load_audio, save_audio

    require_writable("record audio to the workspace")
    settings = get_settings()
    settings.ensure_workspace()

    try:
        di, sample_rate = load_audio(di_path)
        capture = reamp(di, sample_rate=sample_rate)
    except (AudioUnavailable, RenderUnavailable, OSError) as exc:
        return str(exc)

    path = save_audio(
        settings.audio_dir / f"{name}-reamped.wav", capture.wet, capture.sample_rate
    )
    return f"{capture.describe()}\nsaved: {path}"


@mcp.tool()
@surface_errors
def score_candidates(
    target_path: str,
    candidate_paths: dict[str, str],
    mode: Literal["aligned", "distribution"] = "distribution",
) -> str:
    """Score rendered candidates against a target and rank them.

    Args:
        target_path: The reference audio.
        candidate_paths: {label: path} of rendered candidates.
        mode: Use 'aligned' ONLY when the target and candidates are the same
            performance - your own DI reamped through each rig, against your
            own reference. Use 'distribution' against anything separated from
            a record, which is a different performance.

    In distribution mode the sample-wise metrics (ESR, STFT loss) are not
    computed at all. Against a different performance they are unstable to
    arbitrary timing offsets and report nonsense.
    """
    from ..match.metrics import ComparisonMode
    from ..match.render_offline import RenderUnavailable, load_audio
    from ..match.score import rank_candidates, render_ranking

    if not candidate_paths:
        raise ToolError("no candidates given")

    try:
        target, sample_rate = load_audio(target_path)
        loaded = {}
        for label, path in candidate_paths.items():
            audio, rate = load_audio(path)
            if rate != sample_rate:
                from ..match.render_offline import resample

                audio = resample(audio, rate, sample_rate)
            loaded[label] = audio
    except (RenderUnavailable, OSError) as exc:
        return str(exc)

    scores = rank_candidates(
        target, loaded, sample_rate, mode=ComparisonMode(mode)
    )
    detail = "\n\n".join(s.render() for s in scores[:3])
    return f"{render_ranking(scores)}\n\nTop candidates in detail:\n\n{detail}"


@mcp.tool()
@surface_errors
def start_ab_test(
    session_id: str,
    target_label: str,
    candidates: dict[str, str],
    pairs: int | None = None,
) -> str:
    """Start a blind A/B listening test over candidate renders.

    Metrics rank; ears decide. Candidate identities are hidden until results
    are requested, because knowing which one the agent recommended is enough
    to hear it as better.

    Args:
        candidates: {label: audio path}, at least two.
        pairs: Cap the number of trials. Defaults to every unique pairing,
            which is n*(n-1)/2 - worth capping above four candidates.
    """
    from ..match.ab import create_session

    require_writable("create an A/B session")
    settings = get_settings()
    settings.ensure_workspace()

    session = create_session(session_id, target_label, candidates, pairs=pairs)
    session.save(settings.audio_dir / f"ab-{session_id}.json")
    return (
        f"Created blind A/B session '{session_id}' with {len(session.trials)} "
        f"trial(s) over {len(candidates)} candidates.\n\n{session.render_next()}"
    )


@mcp.tool()
@surface_errors
def submit_ab_verdict(
    session_id: str,
    trial_id: int,
    verdict: Literal["left", "right", "tie"],
    confidence: int | None = None,
    comment: str | None = None,
) -> str:
    """Record a verdict for one blind A/B trial and present the next one."""
    from ..match.ab import ABSession

    require_writable("record an A/B verdict")
    path = get_settings().audio_dir / f"ab-{session_id}.json"
    if not path.exists():
        raise ToolError(f"no A/B session '{session_id}'")

    session = ABSession.load(path)
    session.answer(trial_id, verdict, confidence=confidence, comment=comment)
    session.save(path)

    if session.complete:
        return f"Trial {trial_id} recorded. All trials done.\n\n{session.render_standings()}"
    return f"Trial {trial_id} recorded.\n\n{session.render_next()}"


@mcp.tool()
@surface_errors
def ab_results(session_id: str) -> str:
    """Unblind a listening session and show the standings."""
    from ..match.ab import ABSession

    path = get_settings().audio_dir / f"ab-{session_id}.json"
    if not path.exists():
        raise ToolError(f"no A/B session '{session_id}'")
    return ABSession.load(path).render_standings()


def main() -> None:
    """Run the server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

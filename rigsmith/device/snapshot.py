"""A read-only record of a device's state, over the network.

This is not the USB backup and does not replace it. A full backup needs the
device in USB Transfer mode with a cable, and copies the rig files themselves.
Over the network the device will only show you the rig it currently has
*loaded* - reading the other hundred would mean loading each in turn, which
changes the thing being backed up.

So what this captures is: every rig's name and id, the chain and every block
of whatever is loaded, and the module table needed to read any of it back.
That is enough to know exactly what was on the device before a write, to spot
a rig that went missing, and to rebuild by hand the one that was open.

Where a USB backup is available, take that instead. This is what you have when
it is not, and saying so plainly is better than calling it a backup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

#: Blocks worth serialising even when the chain does not reference them - the
#: fixed ends of the signal path carry real settings.
ALWAYS = ("Input", "Output", "Mix", "Rig")


@dataclass
class Snapshot:
    """What the device said about itself, at one moment."""

    host: str
    taken_at: str = ""
    rig_names: list[str] = field(default_factory=list)
    rig_ids: list[str] = field(default_factory=list)
    loaded_name: str | None = None
    loaded_id: str | None = None
    chain: dict[str, Any] = field(default_factory=dict)
    blocks: dict[str, Any] = field(default_factory=dict)
    module_table: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def rig_count(self) -> int:
        return len(self.rig_names)

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "taken_at": self.taken_at,
            "rig_count": self.rig_count,
            "rig_names": self.rig_names,
            "rig_ids": self.rig_ids,
            "loaded_name": self.loaded_name,
            "loaded_id": self.loaded_id,
            "chain": self.chain,
            "blocks": self.blocks,
            "module_table": self.module_table,
            "warnings": self.warnings,
            "scope": (
                "Network snapshot, not a USB backup. Holds every rig's name "
                "and id plus the full state of the loaded rig only - the "
                "device will not serve a rig it has not loaded."
            ),
        }

    def render(self) -> str:
        lines = [
            f"Snapshot of {self.host} at {self.taken_at}",
            f"  {self.rig_count} rig(s) on the device",
            f"  loaded: {self.loaded_name or 'nothing'}",
            f"  {len(self.blocks)} block(s) serialised from the loaded rig",
        ]
        lines.extend(f"  note: {w}" for w in self.warnings)
        lines.append(
            "  This is not a USB backup. It records what was here, not enough "
            "to restore it - for that, use USB Transfer and 'device backup'."
        )
        return "\n".join(lines)


async def take(client, *, serialise_blocks: bool = True) -> Snapshot:
    """Read everything the device will give up without changing anything."""
    snapshot = Snapshot(
        host=client.host,
        taken_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    rigs = await client.call("list_rigs")
    if isinstance(rigs, dict):
        snapshot.rig_names = list(rigs.get("RigNames") or [])
        snapshot.rig_ids = list(rigs.get("RigIds") or [])
        snapshot.loaded_name = rigs.get("loadedName")
        snapshot.loaded_id = rigs.get("loadedID")

    chain = await client.call("get_chain")
    if isinstance(chain, dict):
        snapshot.chain = chain

    table = await client.call("module_table")
    if isinstance(table, dict):
        snapshot.module_table = list(table.get("ModuleTypes") or [])

    if not serialise_blocks:
        return snapshot

    wanted = _blocks_in(snapshot)
    for name in wanted:
        try:
            raw = await client.call("serialize_block", block=name, instance=0)
        except Exception as exc:
            snapshot.warnings.append(f"could not serialise {name}: {exc}")
            continue
        try:
            snapshot.blocks[name] = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            snapshot.blocks[name] = raw
    return snapshot


def _blocks_in(snapshot: Snapshot) -> list[str]:
    """Which blocks the loaded chain actually uses, plus the fixed ends."""
    names: list[str] = []
    for slot in range(1, 15):
        index = snapshot.chain.get(f"ModuleType{slot}")
        if not isinstance(index, int) or index <= 0:
            continue
        if index < len(snapshot.module_table):
            name = snapshot.module_table[index]
            if name and name not in names:
                names.append(name)
    for name in ALWAYS:
        if name not in names:
            names.append(name)
    return names


def save(snapshot: Snapshot, directory: str | Path | None = None) -> Path:
    from ..config import get_settings, require_writable

    require_writable("save a device snapshot")
    settings = get_settings()
    settings.ensure_workspace()
    root = Path(directory) if directory else settings.workspace / "backups"
    root.mkdir(parents=True, exist_ok=True)
    stamp = snapshot.taken_at.replace(":", "").replace("-", "")
    path = root / f"snapshot-{snapshot.host.replace('.', '_')}-{stamp}.json"
    path.write_text(json.dumps(snapshot.to_dict(), indent=2), encoding="utf-8")
    return path

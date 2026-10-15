"""A tone sheet: the rig as you would dial it in by hand, and why.

The draft is the source of truth and the write is how it reaches the Core,
but somebody standing at the device wants something else: each block with
its controls as the screen shows them, the scenes, and the reasons - which
part of which record, what the research said, what still differs. Kept
beside the device it is also how a tone gets dialled in by hand, when the
network write is not an option.

Readings come from the catalog. A control whose taper has not been measured
is read as if it were linear, marked ``~``, and the sheet says so once.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

from .catalog import Catalog, ModelSpec, ParamSpec
from .model import Block, Rig, SceneBlockState

#: Controls nobody dials: the model picker, the block's own switch, its label.
HIDDEN = {"On", "PresetName", "Colour", "Type", "Type2", "Doubling", "DoubleMode", "Sync"}

#: Front-panel order; anything not named follows, alphabetically.
PANEL_ORDER = [
    "GainA", "Gain", "Drive", "Distortion", "Bass", "Mid", "MidFreq", "Treble", "Presence", "Resonance",
    "Master", "Tone", "DelayTime_Synced", "DelayTime", "Rate_Synced", "Rate", "DelayFdbk", "Feedback",
    "Depth", "Mode", "Mix", "Level",
]

LABELS = {
    "GainA": "Gain", "GainB": "Gain B", "DelayTime": "Time", "DelayTime_Synced": "Time", "DelayFdbk": "Feedback",
    "Rate_Synced": "Rate", "MidFreq": "Mid freq", "PreGain": "Input", "PostGain": "Output",
    "SltEQF": "Slot EQ freq", "SltEQG": "Slot EQ gain", "SltEQQ": "Slot EQ Q", "SltEQHP": "Slot high-pass",
    "SltEQLP": "Slot low-pass", "SltEQOn": "Slot EQ", "SltMix": "Slot mix",
}


def label(name: str) -> str:
    """``Gain1_6kHz`` -> ``1.6 kHz``, ``DelayFdbk`` -> ``Feedback``."""
    if name in LABELS:
        return LABELS[name]
    graphic = re.fullmatch(r"Gain(\d+)(?:_(\d+))?(k?)Hz", name)
    if graphic:
        whole, fraction, kilo = graphic.groups()
        return f"{whole}{'.' + fraction if fraction else ''} {kilo}Hz"
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ")


def _format(value: float, unit: str | None) -> str:
    if unit == "%":
        return f"{value:.0f}%"
    if unit == "dB":
        return f"{value:+.1f} dB"
    if unit == "Hz":
        if value >= 1000:
            return f"{value / 1000:.1f} kHz"
        return f"{value:.0f} Hz" if value >= 20 else f"{value:.2f} Hz"
    if unit:
        return f"{value:.0f} {unit}" if abs(value) >= 20 else f"{value:.1f} {unit}"
    return f"{value:.1f}"


def reading(param: ParamSpec | None, value) -> tuple[str, bool]:
    """What the device would show for a stored value, and whether it is assumed.

    The second item is True when the control's taper has not been measured
    and the reading takes it to be linear.
    """
    if isinstance(value, bool) or (param is not None and param.kind == "bool"):
        return ("on" if value else "off"), False
    if param is not None and param.choices and isinstance(value, (int, float)) and float(value).is_integer():
        index = int(value)
        if 0 <= index < len(param.choices):
            return param.choices[index], False
    if not isinstance(value, (int, float)):
        return str(value), False
    if param is None and isinstance(value, float) and 0.0 <= value <= 1.0:
        # Continuous controls are 0-1 on the wire, so with no range to read
        # it against, a value is still a knob position.
        return f"~{value * 100:.0f}% of travel", True
    if param is None or not param.normalised:
        return f"{value:g}", False
    if param.taper in ("linear", "log"):
        return _format(param.to_display(float(value)), param.unit), False
    if param.display_min is None or param.display_max is None:
        return f"~{float(value) * 100:.0f}% of travel", True
    shown = param.display_min + float(value) * (param.display_max - param.display_min)
    return "~" + _format(shown, param.unit), True


def _order(name: str) -> tuple[int, str]:
    return (PANEL_ORDER.index(name) if name in PANEL_ORDER else len(PANEL_ORDER), name)


def block_settings(block: Block, spec: ModelSpec | None, module: ModelSpec | None = None) -> tuple[list[tuple[str, str]], bool]:
    """``[(label, reading)]`` for the controls the draft sets, and whether any is assumed."""
    params = dict(block.params)
    synced = params.get("Sync") is True
    rows: list[tuple[str, str]] = []
    assumed = False
    for name in sorted(params, key=_order):
        if name in HIDDEN:
            continue
        if synced and name in ("DelayTime", "Rate"):
            continue
        if not synced and name.endswith("_Synced"):
            continue
        # A model's entry and its module's can both describe a control;
        # the one whose taper has been measured is the one to read by.
        found = [p for p in ((spec.param(name) if spec else None), (module.param(name) if module else None)) if p is not None]
        param = next((p for p in found if p.taper in ("linear", "log")), found[0] if found else None)
        text, guess = reading(param, params[name])
        if name.endswith("_Synced"):
            text += ", synced to the rig's tempo"
        rows.append((label(name), text))
        assumed = assumed or guess
    return rows, assumed


def readings(rig: Rig, catalog: Catalog) -> dict[int, list[tuple[str, str]]]:
    """Every block's controls as the screen shows them, by slot - for the board."""
    found = {}
    for block in rig.blocks:
        spec = catalog.get(block.model_id)
        module = catalog.get(spec.selector.split(".", 1)[0]) if spec and spec.selector else None
        found[block.slot] = block_settings(block, spec, module)[0]
    return found


@dataclass
class Sheet:
    title: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    chain: list[dict] = field(default_factory=list)
    """``{slot, name, kind, bypassed, doubled, settings: [(label, reading)]}``."""
    scenes: list[str] = field(default_factory=list)
    origin: list[tuple[str, str]] = field(default_factory=list)
    research: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    footnotes: list[str] = field(default_factory=list)

    def markdown(self) -> str:
        out = [f"# {self.title}", ""]
        out.extend(f"- **{key}:** {value}" for key, value in self.facts)
        out += ["", "## The chain", ""]
        for item in self.chain:
            head = f"{item['slot']}. **{item['name']}** ({item['kind']})"
            if item.get("doubled"):
                head += f" - 2X with {item['doubled']}"
            if item.get("bypassed"):
                head += " - bypassed"
            out.append(head)
            for key, value in item["settings"]:
                out.append(f"   - {key}: {value}")
        for title, rows in (("Scenes", self.scenes), ("Where it came from", [f"**{k}:** {v}" for k, v in self.origin]),
                            ("What the research said", self.research), ("Still different from the record", self.remaining),
                            ("Notes", self.notes)):
            if rows:
                out += ["", f"## {title}", ""]
                out.extend(f"- {row}" for row in rows)
        if self.footnotes:
            out += [""]
            out.extend(f"_{text}_" for text in self.footnotes)
        return "\n".join(out) + "\n"

    def html(self) -> str:
        e = html.escape
        facts = "".join(f"<div><dt>{e(k)}</dt><dd>{e(v)}</dd></div>" for k, v in self.facts)
        blocks = []
        for item in self.chain:
            extra = (f" <span class=tag>2X with {e(item['doubled'])}</span>" if item.get("doubled") else "") + (
                " <span class=tag>bypassed</span>" if item.get("bypassed") else "")
            rows = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in item["settings"])
            blocks.append(
                f"<li><div class=block><span class=slot>{item['slot']}</span><b>{e(item['name'])}</b>"
                f" <span class=kind>{e(item['kind'])}</span>{extra}</div>"
                + (f"<table>{rows}</table>" if rows else "<p class=plain>as it comes</p>") + "</li>"
            )
        sections = []
        for title, rows in (("Scenes", self.scenes), ("Where it came from", [f"{k}: {v}" for k, v in self.origin]),
                            ("What the research said", self.research), ("Still different from the record", self.remaining),
                            ("Notes", self.notes)):
            if rows:
                sections.append(f"<h2>{e(title)}</h2><ul>" + "".join(f"<li>{e(r)}</li>" for r in rows) + "</ul>")
        notes = "".join(f"<p class=foot>{e(t)}</p>" for t in self.footnotes)
        return (
            "<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{e(self.title)} - tone sheet</title><style>{_CSS}</style></head><body><main>"
            f"<h1>{e(self.title)}</h1><dl class=facts>{facts}</dl><h2>The chain</h2><ol class=chain>{''.join(blocks)}</ol>"
            f"{''.join(sections)}{notes}<p class=foot>Made by rigsmith. Print it, or keep it open beside the Core.</p>"
            "</main></body></html>"
        )


_CSS = """
:root { color-scheme: light dark; --ink: #1d2126; --soft: #5d6773; --line: #d9dee4; --bg: #fbfbfa; --accent: #b86e00; }
@media (prefers-color-scheme: dark) { :root { --ink: #e6e9ec; --soft: #9aa4af; --line: #2c333b; --bg: #111418; --accent: #f0a020; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 820px; margin: 0 auto; padding: 28px 16px 48px; }
h1 { font-size: 26px; margin: 0 0 10px; }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .06em; color: var(--soft); margin: 28px 0 8px; border-bottom: 1px solid var(--line); padding-bottom: 4px; }
.facts { display: flex; flex-wrap: wrap; gap: 6px 22px; margin: 0; }
.facts dt { font-size: 11px; color: var(--soft); text-transform: uppercase; letter-spacing: .05em; }
.facts dd { margin: 0; font-weight: 600; }
.chain { list-style: none; padding: 0; margin: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); gap: 10px; }
.chain > li { border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; break-inside: avoid; }
.block { margin-bottom: 6px; }
.slot { display: inline-block; min-width: 22px; color: var(--accent); font-weight: 700; }
.kind, .plain { color: var(--soft); font-size: 12px; }
.tag { font-size: 11px; border: 1px solid var(--line); border-radius: 4px; padding: 0 5px; color: var(--soft); }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th { text-align: left; font-weight: 500; color: var(--soft); padding: 1px 8px 1px 0; width: 45%; }
td { font-variant-numeric: tabular-nums; font-weight: 600; }
ul { padding-left: 20px; }
.foot { color: var(--soft); font-size: 12px; }
@media print { body { background: #fff; color: #000; } main { padding: 0; } .chain > li { border-color: #bbb; } }
"""


def build(rig: Rig, catalog: Catalog, *, origin: dict | None = None) -> Sheet:
    """The sheet for a rig; ``origin`` adds where it came from (see :mod:`rigsmith.core.sheets`)."""
    sheet = Sheet(title=rig.name)
    sheet.facts.append(("Tempo", f"{rig.tempo:g} BPM, fixed" if rig.tempo else "the device's"))
    sheet.facts.append(("Path", rig.signal_path.value.replace("_", " ")))
    sheet.facts.append(("Blocks", f"{len(rig.blocks)}"))
    if not rig.is_stereo:
        sheet.facts.append(("Output", "mono"))
    assumed = False
    for block in sorted(rig.blocks, key=lambda b: b.slot):
        spec = catalog.get(block.model_id)
        module = catalog.get(spec.selector.split(".", 1)[0]) if spec and spec.selector else None
        rows, guess = block_settings(block, spec, module)
        assumed = assumed or guess
        sheet.chain.append({
            "slot": block.slot, "name": block.model_name or block.model_id, "kind": block.category.value,
            "bypassed": not block.enabled, "doubled": block.doubled_model_id, "settings": rows,
        })
    names = {block.slot: block.model_name or block.model_id for block in rig.blocks}
    for scene in sorted(rig.scenes, key=lambda s: (s.footswitch or 99, s.name)):
        on = [names.get(s, str(s)) for s, st in sorted(scene.states.items()) if st is SceneBlockState.ON]
        off = [names.get(s, str(s)) for s, st in sorted(scene.states.items()) if st is SceneBlockState.OFF]
        where = f"FS{scene.footswitch}" if scene.footswitch else "no footswitch"
        sheet.scenes.append(f"{where} '{scene.name}': on {', '.join(on) or '-'}; off {', '.join(off) or '-'}")
    if rig.scenes:
        sheet.footnotes.append("Scenes are set on the device by hand for now - the network write does not carry them yet.")
    if assumed:
        sheet.footnotes.append(
            "~ marks a reading worked out without a measured taper - as if the control were linear, or as "
            "a share of the knob's travel where the catalog has no range for it ('rigsmith catalog "
            "calibrate' against a paired Core measures them)."
        )
    origin = origin or {}
    sheet.origin = list(origin.get("origin", []))
    sheet.research = list(origin.get("research", []))
    sheet.remaining = list(origin.get("remaining", []))
    sheet.notes = list(origin.get("notes", []))
    if rig.notes and not sheet.origin:
        sheet.notes.insert(0, rig.notes)
    return sheet

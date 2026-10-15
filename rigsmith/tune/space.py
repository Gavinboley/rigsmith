"""Which knobs to turn, and how far they go.

A knob is one block parameter on the device: the node it lives on (``Amp``,
``G_EQ``, ``Amp_2``), its name there (``GainA``), and its range *on the wire*
- 0 to 1 for every continuous control on the Core, whatever the display
says. The optimiser works in that normalised space; the display value is
only for showing a person what was chosen.

Not every parameter is worth tuning. A tone stack, the gain and the master
shape the sound; a colour index, a preset name or a bus routing do not. The
default spaces here name the parameters that matter, per kind of block.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..core.errors import Invalid

#: Parameters worth tuning, by catalog category, in priority order.
TONE_PARAMS = {
    "amp": ("GainA", "Bass", "Mid", "Treble", "Presence", "Master", "Resonance"),
    "nam": ("Input", "Bass", "Middle", "Treble"),
    "effect": ("Drive", "Gain", "Tone", "Level", "Mix"),
    "eq": ("Gain60Hz", "Gain100Hz", "Gain200Hz", "Gain400Hz", "Gain800Hz", "Gain1_6kHz", "Gain3_2kHz", "Gain6_4kHz"),
}


@dataclass
class Knob:
    node: str
    """The block's node on the device - ``Amp``, ``G_EQ``, ``Amp_2``."""
    param: str
    low: float = 0.0
    high: float = 1.0
    start: float | None = None
    kind: str = "float"
    """float, int or choice."""
    choices: list = field(default_factory=list)
    display_min: float | None = None
    display_max: float | None = None
    unit: str | None = None

    @property
    def key(self) -> str:
        return f"{self.node}.{self.param}"

    def clip(self, value: float):
        if self.kind == "choice":
            return self.choices[int(round(value)) % len(self.choices)] if self.choices else value
        value = min(max(float(value), self.low), self.high)
        return int(round(value)) if self.kind == "int" else value

    def display(self, value) -> str:
        if self.kind != "float" or self.display_min is None or self.display_max is None:
            return str(value)
        shown = self.display_min + (float(value) - self.low) / ((self.high - self.low) or 1) * (self.display_max - self.display_min)
        unit = f" {self.unit}" if self.unit and self.unit != "%" else ("%" if self.unit == "%" else "")
        return f"{shown:.1f}{unit}"

    def to_dict(self) -> dict:
        data = asdict(self)
        data["key"] = self.key
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Knob":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Space:
    knobs: list[Knob] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.knobs)

    def start(self) -> dict[str, float]:
        return {k.key: (k.start if k.start is not None else (k.low + k.high) / 2) for k in self.knobs}

    def clip(self, values: dict[str, float]) -> dict[str, float]:
        return {k.key: k.clip(values.get(k.key, k.start if k.start is not None else (k.low + k.high) / 2)) for k in self.knobs}

    def by_node(self, values: dict[str, float]) -> dict[str, dict[str, float]]:
        grouped: dict[str, dict[str, float]] = {}
        for knob in self.knobs:
            if knob.key in values:
                grouped.setdefault(knob.node, {})[knob.param] = values[knob.key]
        return grouped

    def describe(self, values: dict[str, float]) -> str:
        return ", ".join(f"{k.key}={k.display(values[k.key])}" for k in self.knobs if k.key in values)

    def to_dict(self) -> dict:
        return {"knobs": [k.to_dict() for k in self.knobs]}

    @classmethod
    def from_dict(cls, data: dict) -> "Space":
        return cls(knobs=[Knob.from_dict(k) for k in data.get("knobs", [])])


def virtual_amp(names=("GainA", "Bass", "Mid", "Treble", "Presence")) -> Space:
    """The simulated amp's tone controls, under the Core's names."""
    return Space([Knob("Amp", name, 0.0, 1.0, 0.5, display_min=0, display_max=100, unit="%") for name in names])


def from_model(spec, node: str, *, include: tuple[str, ...] | None = None, current: dict | None = None) -> Space:
    """Knobs for one catalog model on one node.

    Args:
        spec: A :class:`~rigsmith.rig.catalog.ModelSpec` (or its dict).
        include: Parameter names to tune; defaults to the category's tone
            parameters that the model actually has.
        current: Wire values to start from - the rig as it stands.
    """
    data = spec if isinstance(spec, dict) else asdict(spec)
    params = {p["name"]: p for p in data.get("params", [])}
    category = data.get("category") or "effect"
    if include is None:
        family = "eq" if "EQ" in data.get("name", "") else category
        include = tuple(n for n in TONE_PARAMS.get(family, TONE_PARAMS["effect"]) if n in params)
    knobs = []
    for name in include:
        param = params.get(name)
        if param is None:
            raise Invalid(f"'{data.get('name')}' has no parameter '{name}'", hint="It has: " + ", ".join(sorted(params)))
        kind = param.get("kind", "float")
        if kind == "float":
            knobs.append(
                Knob(
                    node, name, 0.0, 1.0,
                    start=(current or {}).get(name, param.get("default")),
                    display_min=param.get("display_min"), display_max=param.get("display_max"),
                    unit=param.get("unit"),
                )
            )
        elif kind == "int" and param.get("choices"):
            knobs.append(Knob(node, name, 0, len(param["choices"]) - 1, start=(current or {}).get(name, 0), kind="int"))
        elif kind == "bool":
            knobs.append(Knob(node, name, 0, 1, start=(current or {}).get(name, 0), kind="int"))
    return Space(knobs)

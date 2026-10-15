"""Measuring how a wire value maps to what the device shows.

Every continuous control on the Core is 0-1 in the protocol. The metadata
publishes a range next to it - ``0..100`` for a tone knob, ``220..3000`` for a
mid frequency - but that is what the control *reads*, not what it accepts. The
curve between the two is not published at all, and it is not guessable:

    MidFreq   0.5 -> 1610 Hz  across 220..3000    linear
    SltEQF    0.5 ->  566 Hz  across 20..16000    logarithmic

Same unit, same shape of metadata, different curve. Anything that inferred the
taper from the unit would be wrong about one of those two, silently, in a way
that only shows up as a tone that will not sit right.

So it is measured instead. The device will tell you both numbers about itself
at once - ``object-properties`` gives the wire value and ``serializeBlock``
gives the reading - and one sample strictly inside the range is enough to tell
the two curves apart. Where it is not enough, the answer stays ``unknown`` and
conversions refuse rather than guess.

Nothing here writes to the device. Both calls are reads.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

#: How far a prediction may be off, as a fraction of the display span, and
#: still count as matching. Generous, because the device rounds what it prints
#: (``%.0f Hz`` throws away everything below a hertz) and the wire value comes
#: back as a float32 widened to double, so 0.5 arrives as 0.50008.
TOLERANCE = 0.02

#: A sample at the very ends predicts the same reading under either curve, so
#: it separates nothing. Only samples this far inside the range are used.
INFORMATIVE_MARGIN = 0.05


@dataclass
class Sample:
    """One parameter caught in both units at the same moment."""

    param: str
    wire: float
    display: float


@dataclass
class Calibration:
    """What was measured, and what could not be."""

    tapers: dict[str, str] = field(default_factory=dict)
    """Parameter name -> ``linear``, ``log``, or ``unknown``."""
    uninformative: list[str] = field(default_factory=list)
    """Parameters whose sample sat too near an end to tell the curves apart."""
    conflicting: list[str] = field(default_factory=list)
    """Parameters whose samples disagreed - neither curve fits."""

    @property
    def measured(self) -> int:
        return sum(1 for t in self.tapers.values() if t != "unknown")

    def render(self) -> str:
        lines = [f"measured {self.measured} of {len(self.tapers)} parameter(s)"]
        by_taper: dict[str, int] = {}
        for taper in self.tapers.values():
            by_taper[taper] = by_taper.get(taper, 0) + 1
        for taper, count in sorted(by_taper.items()):
            lines.append(f"  {taper}: {count}")
        if self.uninformative:
            lines.append(
                f"  {len(self.uninformative)} sat at an end of their range, "
                "where both curves predict the same reading. Move those "
                "controls on the device and calibrate again."
            )
        if self.conflicting:
            lines.append(
                f"  {len(self.conflicting)} fit neither curve: "
                + ", ".join(sorted(self.conflicting)[:8])
            )
        return "\n".join(lines)


def _linear(wire: float, low: float, high: float) -> float:
    return low + wire * (high - low)


def _log(wire: float, low: float, high: float) -> float:
    return low * (high / low) ** wire


def taper_of(
    samples: list[Sample],
    display_min: float | None,
    display_max: float | None,
    *,
    tolerance: float = TOLERANCE,
) -> str:
    """Classify one parameter's curve from however many samples there are.

    Returns ``linear``, ``log``, or ``unknown``. A sample too close to either
    end is ignored rather than counted, because both curves agree there.
    """
    if display_min is None or display_max is None or display_max == display_min:
        return "unknown"

    span = abs(display_max - display_min)
    usable = [
        s for s in samples if INFORMATIVE_MARGIN < s.wire < 1.0 - INFORMATIVE_MARGIN
    ]
    if not usable:
        return "unknown"

    fits_linear = all(
        abs(_linear(s.wire, display_min, display_max) - s.display) <= tolerance * span
        for s in usable
    )
    # A logarithmic curve is only defined for a range that stays positive.
    can_log = display_min > 0 and display_max > 0
    fits_log = can_log and all(
        abs(_log(s.wire, display_min, display_max) - s.display) <= tolerance * span
        for s in usable
    )

    if fits_linear and not fits_log:
        return "linear"
    if fits_log and not fits_linear:
        return "log"
    # Both fitting means the samples do not separate them; neither fitting
    # means it is some third curve. Either way we do not know it.
    return "unknown"


def calibrate_model(
    spec,
    samples_by_param: dict[str, list[Sample]],
    *,
    tolerance: float = TOLERANCE,
) -> Calibration:
    """Measure and apply tapers for every float parameter of one model."""
    calibration = Calibration()
    for param in spec.params:
        if param.kind != "float":
            continue
        samples = samples_by_param.get(param.name, [])
        taper = taper_of(
            samples, param.display_min, param.display_max, tolerance=tolerance
        )
        param.taper = taper
        calibration.tapers[param.name] = taper
        if taper != "unknown":
            continue
        if not any(
            INFORMATIVE_MARGIN < s.wire < 1.0 - INFORMATIVE_MARGIN for s in samples
        ):
            calibration.uninformative.append(param.name)
        else:
            calibration.conflicting.append(param.name)
    return calibration


def samples_from(wire: dict, serialized: dict) -> dict[str, list[Sample]]:
    """Pair up one ``object-properties`` read with one ``serializeBlock``.

    ``serializeBlock`` wraps every child as ``{"type": 0, "value": N}`` for a
    number, ``{"state": bool}`` for a switch and ``{"string": s}`` for a
    picker. Only the numbers are of any use here.
    """
    children = serialized
    for key in ("data",):
        if isinstance(children, dict) and key in children:
            children = children[key]
    if isinstance(children, dict) and len(children) == 1:
        # `{"data": {"Amp": {...}}}` - step through the block's own name.
        children = next(iter(children.values()))
    if isinstance(children, dict):
        children = children.get("children", children)
    if not isinstance(children, dict):
        return {}

    paired: dict[str, list[Sample]] = {}
    for name, entry in children.items():
        if not isinstance(entry, dict) or "value" not in entry:
            continue
        live = wire.get(name)
        if not isinstance(live, (int, float)) or isinstance(live, bool):
            continue
        display = entry["value"]
        if not isinstance(display, (int, float)) or isinstance(display, bool):
            continue
        paired.setdefault(name, []).append(
            Sample(param=name, wire=float(live), display=float(display))
        )
    return paired

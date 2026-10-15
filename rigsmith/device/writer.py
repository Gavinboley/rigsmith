"""Turning a rig into the calls that build it on a Core.

The device does not model a rig as a resource. There is no ``POST /rigs``
returning an id you then populate: there is *the rig that is currently
loaded*, and you build one by asking for a blank, changing the live patch, and
saving it under a name. Every step operates on device state rather than on
something you are holding.

Three things that are easy to get wrong, and are the reason this is its own
module rather than a loop inside the client:

**Slots are integers into a module table.** A chain is ``ModuleType1..14``,
each an index into the 278-entry table the device publishes. Putting an amp in
slot six is ``ModuleType6 = 1``. The block's *name* appears nowhere.

**A model is usually not a module.** There is one ``Amp`` module and 53 amp
models behind its ``Type`` parameter. So "put a Treadplate in slot six" is two
different numbers: ``1`` for the module, ``36`` for the model.

**A doubled block is one block, not two.** The Core's 2X runs two models in
one slot - ``Doubling``, ``DoubleMode`` and ``Type2`` - which is how a stereo
pair of amps fits in a single slot rather than consuming two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.errors import Invalid, NotFound
from ..rig.catalog import Catalog, ModelSpec
from ..rig.model import Block, Doubling, Rig

#: The device's node name for a block, and its module-table entry, both derive
#: from the display name. The second instance of any block appends this.
SECOND_INSTANCE = " 2"


@dataclass
class Placement:
    """One block, resolved into everything needed to put it on the device."""

    slot: int
    module: str
    """Display name of the module, e.g. ``Amp``."""
    module_index: int
    """Its index in the device's module table."""
    node: str
    """Path segment under ``/Evil/Engine/Patch/``, e.g. ``Amp`` or ``Amp_2``."""
    properties: dict[str, Any] = field(default_factory=dict)
    """What to PUT on that node."""
    notes: list[str] = field(default_factory=list)


def node_name(module: str) -> str:
    """``Green JRC-OD 2`` -> ``Green_JRC-OD_2``."""
    return module.replace(" ", "_")


def _module_for(spec: ModelSpec, catalog: Catalog) -> tuple[str, ModelSpec]:
    """The module a model lives on, and its spec.

    A module is its own model; anything with a selector rides on one.
    """
    if not spec.selector:
        return spec.name, spec
    module_name = spec.selector.split(".", 1)[0]
    module = catalog.get(module_name)
    if module is None:
        raise NotFound(
            f"'{spec.name}' is selected on the '{module_name}' module, which "
            "is not in the catalog",
            hint="Re-run 'rigsmith catalog extract --subtree'.",
        )
    return module_name, module


def resolve_block(
    block: Block,
    catalog: Catalog,
    *,
    instance: int = 0,
) -> Placement:
    """Work out how to realise one block on the device.

    ``instance`` is 0 for the first use of a module in this rig and 1 for the
    second - the device carries exactly two of each, and a rig wanting two
    overdrives uses ``Green JRC-OD`` and ``Green JRC-OD 2``.
    """
    spec = catalog.get(block.model_id)
    if spec is None:
        raise NotFound(
            f"'{block.model_id}' is not in the catalog, so there is no way to "
            "know what to send for it",
            hint="Check the name, or re-run 'rigsmith catalog extract --subtree'.",
        )

    module_name, module = _module_for(spec, catalog)

    if instance > 1:
        raise Invalid(
            f"the device has only two {module_name} blocks, and this rig asks "
            f"for {instance + 1}",
            hint="Drop one, or use a different model for it.",
        )

    wanted = module_name + (SECOND_INSTANCE if instance else "")
    entry = catalog.get(wanted)
    module_index = entry.device_index if entry else None
    if module_index is None and instance:
        # The table lists second instances one after the first.
        module_index = (
            module.device_index + 1 if module.device_index is not None else None
        )
    if module_index is None:
        module_index = module.device_index
    if module_index is None:
        raise NotFound(
            f"the catalog has no module-table index for '{wanted}'",
            hint="The chain is written as integers; without one there is "
            "nothing to put in the slot.",
        )

    placement = Placement(
        slot=block.slot,
        module=wanted,
        module_index=int(module_index),
        node=node_name(wanted),
    )

    # Select the model on its module, when it is not a module itself.
    if spec.selector:
        parameter = spec.selector.split(".", 1)[1]
        placement.properties[parameter] = spec.device_index

    placement.properties.update(_doubling_properties(block, catalog, spec))

    # The block's own parameters last, so an explicit setting always wins over
    # anything inferred above.
    placement.properties.update(block.params)

    if block.enabled is False:
        placement.properties["On"] = False
    elif "On" not in placement.properties:
        placement.properties["On"] = True

    _check_values(placement, module, spec)
    return placement


def _doubling_properties(
    block: Block, catalog: Catalog, spec: ModelSpec
) -> dict[str, Any]:
    """2X, expressed the way the device expresses it."""
    if block.doubling is Doubling.NONE:
        return {}

    properties: dict[str, Any] = {
        "Doubling": True,
        # The device stores the mode as a switch: false is Stereo, true is
        # Switch. Naming it after the boolean would have been kinder.
        "DoubleMode": block.doubling is Doubling.SWITCH,
    }

    if not block.doubled_model_id:
        return properties

    second = catalog.get(block.doubled_model_id)
    if second is None:
        raise NotFound(
            f"the second model '{block.doubled_model_id}' is not in the catalog"
        )
    if not second.selector:
        raise Invalid(
            f"'{second.name}' is a module, not a model that can sit in the "
            "second half of a doubled block"
        )
    if spec.selector and second.selector != spec.selector:
        raise Invalid(
            f"'{spec.name}' and '{second.name}' are selected on different "
            "modules, so they cannot be doubled together",
            hint=f"{spec.selector} vs {second.selector}",
        )
    parameter = second.selector.split(".", 1)[1]
    properties[f"{parameter}2"] = second.device_index
    return properties


def _check_values(placement: Placement, module: ModelSpec, spec: ModelSpec) -> None:
    """Catch a knob reading sent where a wire value belongs."""
    for name, value in placement.properties.items():
        param = module.param(name) or spec.param(name)
        if param is None or not param.normalised:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if 1.0 < value <= (param.display_max or 1.0):
            raise Invalid(
                f"{placement.module}.{name} = {value} looks like a reading, "
                f"not a value to send",
                hint=(
                    f"On the wire this control is 0-1; the device displays it "
                    f"as {param.display_min:g}-{param.display_max:g}"
                    f"{' ' + param.unit if param.unit else ''}. "
                    f"Send {value / (param.display_max or 100):.2f}."
                ),
            )


def plan_rig(rig: Rig, catalog: Catalog) -> list[Placement]:
    """Resolve every block, tracking which module instance each one uses."""
    used: dict[str, int] = {}
    placements = []
    for block in sorted(rig.blocks, key=lambda b: b.slot):
        spec = catalog.get(block.model_id)
        module_name = _module_for(spec, catalog)[0] if spec else block.model_id
        instance = used.get(module_name, 0)
        placements.append(resolve_block(block, catalog, instance=instance))
        used[module_name] = instance + 1
    return placements


def chain_properties(placements: list[Placement], *, slots: int = 14) -> dict[str, int]:
    """The whole chain as one property object - empty slots included.

    Written in one PUT rather than fourteen calls. A half-written chain is a
    rig with blocks in it that nobody asked for, and the device would happily
    save that.
    """
    chain = {f"ModuleType{n}": 0 for n in range(1, slots + 1)}
    for placement in placements:
        if not 1 <= placement.slot <= slots:
            raise Invalid(
                f"slot {placement.slot} is outside the device's 1-{slots}",
            )
        chain[f"ModuleType{placement.slot}"] = placement.module_index
    return chain

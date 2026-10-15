"""Turning a rig's knobs towards a target, by ear - a machine's ear.

The pieces exist elsewhere; this joins them into one run:

1. a **target** - a part of the record (its own layer, its own seconds);
2. a **DI** - ideally that part played on your guitar, on its clock;
3. a **space** - which knobs, on which blocks;
4. a **renderer** - where each setting is heard:

   * ``virtual`` - the simulated amp, offline. For trying the flow.
   * ``loop`` - written live to the device (or the simulated bench) and
     reamped through the hardware loop. Every evaluation is the Core's own
     output.

5. an **optimiser** (:mod:`rigsmith.tune.optimize`) and, at the end, a
   **match EQ** for what the knobs could not close.

Each run is kept in the workspace with its whole history, so a result can be
replayed, compared, applied to a draft, or thrown away.

Nothing here writes a *rig* to the device. Live parameter changes alter the
loaded patch the way turning a knob on the unit does; saving it is the
gated write in :func:`rigsmith.core.rigs.write`, as always.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from ..config import get_settings, require_writable
from .errors import Invalid, NotFound
from .jobs import Progress


def _dir() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    directory = settings.workspace / "tuning"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


# --------------------------------------------------------------------------
# where parameters go
# --------------------------------------------------------------------------


class VirtualSink:
    """Knob changes on the simulated bench's Core."""

    name = "simulated Core"

    def __init__(self, world=None) -> None:
        from ..loop import virtual

        self.world = world or virtual.world()

    def apply(self, by_node: dict[str, dict[str, float]]) -> None:
        for node, values in by_node.items():
            if node.startswith("Amp"):
                self.world.rig.amp.set(**values)


class DeviceSink:
    """Knob changes written live to the paired Core's loaded patch."""

    name = "the Core"

    def apply(self, by_node: dict[str, dict[str, float]]) -> None:
        import asyncio

        from ..device.client import connect

        async def send():
            async with await connect() as client:
                for node, values in by_node.items():
                    await client.call("set_properties", node=f"/Evil/Engine/Patch/{node}", properties=values)

        asyncio.run(send())


# --------------------------------------------------------------------------
# the bench rig on the device
# --------------------------------------------------------------------------

#: Model families autopilot switches, by the module that hosts them.
SWITCHED = {"amp": ("Amp", "ReValver Amp"), "cab": ("Cab", "ReValver Cab")}

#: What a bench rig is made of when there is none: one amp, one cab. Any amp
#: and cab will do - autopilot switches the models - these are just sensible.
BENCH_AMP, BENCH_CAB = "64 Black Lux Norm", "4x12 Green 25W"
BENCH_NAME = "Autopilot bench"


@dataclass
class BenchCheck:
    modules: list[str]
    """What the loaded rig's chain holds, slot by slot."""
    hosts: list[str]
    """The first-instance modules autopilot can switch models on."""

    @property
    def ok(self) -> bool:
        return any(h in self.hosts for h in SWITCHED["amp"]) and any(h in self.hosts for h in SWITCHED["cab"])

    def describe(self) -> str:
        chain = " > ".join(self.modules) or "nothing"
        if self.ok:
            return f"ready: the loaded rig has {', '.join(self.hosts)} to switch ({chain})"
        missing = [kind for kind, hosts in SWITCHED.items() if not any(h in self.hosts for h in hosts)]
        return (
            f"not ready: the loaded rig has no {' and no '.join(missing)} block to switch models on ({chain}). "
            "Load a rig with an Amp and a Cab, or make the bench rig draft and write it."
        )

    def to_dict(self) -> dict:
        return {"modules": self.modules, "hosts": self.hosts, "ok": self.ok, "describe": self.describe()}


def assess_chain(chain: dict, table: list[str]) -> BenchCheck:
    """Which switchable modules a chain holds. Only the first instance counts:
    autopilot sets ``Amp``, and an ``Amp 2`` in the chain would not hear it."""
    modules = []
    for slot in range(1, 15):
        index = chain.get(f"ModuleType{slot}")
        if isinstance(index, int) and 0 < index < len(table):
            modules.append(table[index])
    hosts = [m for m in modules if m in SWITCHED["amp"] + SWITCHED["cab"]]
    return BenchCheck(modules=modules, hosts=hosts)


def bench_check() -> BenchCheck:
    """Read the Core's loaded chain - read only - and say whether autopilot
    can switch amps and cabs on it."""
    import asyncio

    from ..device.client import connect

    async def read():
        async with await connect() as client:
            chain = await client.call("get_chain")
            table = (await client.call("module_table"))["ModuleTypes"]
            return chain, table

    chain, table = asyncio.run(read())
    return assess_chain(chain, table)


def bench_rig_draft() -> str:
    """A draft with one amp and one cab, to write as the bench rig.

    Nothing is written here; writing it is the usual gated step, so the
    rig that was loaded is backed up first.
    """
    from . import rigs

    require_writable("make the bench rig draft")
    for draft in rigs.list_drafts():
        if getattr(draft, "rig", None) is not None and draft.rig.name == BENCH_NAME:
            return draft.draft_id
    made = rigs.create(BENCH_NAME, notes="One amp and one cab for autopilot's device bench to switch models on.").draft
    rigs.add_block(made.draft_id, "amp", BENCH_AMP)
    rigs.add_block(made.draft_id, "cab", BENCH_CAB)
    return made.draft_id


# --------------------------------------------------------------------------
# runs
# --------------------------------------------------------------------------


@dataclass
class TuneRun:
    run_id: str
    target: str
    di: str
    renderer: str
    mode: str
    region: str | None = None
    route_id: str | None = None
    draft_id: str | None = None
    voicing: int | None = None
    """For the virtual renderer: which simulated amp voicing."""
    result: dict = field(default_factory=dict)
    best_audio: str | None = None
    eq: dict | None = None
    applied_to: str | None = None
    song: dict | None = None
    """``{"song_id", "part_id"}`` when the target is a song's part."""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        lines = [f"tuning run {self.run_id}: {self.renderer} renderer, {self.mode} mode"]
        lines.append(f"  target {self.target}" + (f" ({self.region})" if self.region else ""))
        lines.append(f"  DI {self.di}")
        if self.result:
            lines.extend("  " + l for l in self.result.get("describe", "").splitlines())
        if self.eq:
            lines.extend("  " + l for l in self.eq.get("describe", "").splitlines())
        if self.best_audio:
            lines.append(f"  best render: {self.best_audio}")
        if self.applied_to:
            lines.append(f"  applied to draft '{self.applied_to}'")
        lines.extend(f"  note: {n}" for n in self.notes)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        from dataclasses import asdict

        data = asdict(self)
        data["describe"] = self.describe()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "TuneRun":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> Path:
        path = _dir() / f"{self.run_id}.json"
        data = self.to_dict()
        data.pop("describe", None)
        from . import storage

        return storage.write_json(path, data, default=float)


def list_runs() -> list[TuneRun]:
    runs = []
    for path in sorted(_dir().glob("*.json"), reverse=True):
        try:
            runs.append(TuneRun.from_dict(json.loads(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError):
            continue
    runs.sort(key=lambda r: r.created_at, reverse=True)
    return runs


def get_run(run_id: str) -> TuneRun:
    path = _dir() / f"{run_id}.json"
    if not path.is_file():
        raise NotFound(f"no tuning run '{run_id}'", hint="Runs: " + ", ".join(r.run_id for r in list_runs()[:10]))
    return TuneRun.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _space(draft_id: str | None, knobs: list[str] | None):
    from ..tune.space import Space, from_model, virtual_amp

    if draft_id is None:
        space = virtual_amp()
    else:
        from ..device.writer import resolve_block
        from ..rig.catalog import get_catalog
        from . import rigs

        draft = rigs.get(draft_id)
        catalog = get_catalog()
        knobs_found = []
        for block in draft.rig.blocks:
            if block.category.value not in ("amp", "nam") and "EQ" not in (block.model_name or block.model_id):
                continue
            try:
                placement = resolve_block(block, catalog)
            except Exception:
                continue
            spec = catalog.get(placement.module) or catalog.get(block.model_id)
            if spec is None:
                continue
            knobs_found.extend(from_model(spec, placement.node, current=block.params).knobs)
        space = Space(knobs_found)
    if knobs:
        wanted = set(knobs)
        space = Space([k for k in space.knobs if k.key in wanted or k.param in wanted])
    if not len(space):
        raise Invalid(
            "nothing to tune",
            hint="Give knobs like Amp.GainA, or a draft whose amp, NAM or EQ blocks the catalog knows.",
        )
    return space


def tune(
    target,
    di=None,
    *,
    region=None,
    renderer: str = "virtual",
    route_id: str | None = None,
    draft_id: str | None = None,
    knobs: list[str] | None = None,
    budget: int = 30,
    method: str = "auto",
    mode: str = "distribution",
    voicing: int = 3,
    song: dict | None = None,
    progress: Progress | None = None,
) -> TuneRun:
    """Search for the knob settings that bring a rig closest to a target."""
    from ..match.metrics import ComparisonMode
    from ..match.render_offline import load_audio, resample, save_audio
    from ..match.score import score_candidate
    from ..tune import eq as match_eq
    from ..tune.optimize import optimise
    from . import matching

    require_writable("run a tuning search")
    if renderer not in ("virtual", "loop"):
        raise Invalid(f"unknown renderer '{renderer}'", hint="Use 'virtual' or 'loop'.")
    comparison = matching.parse_mode(mode)

    target_path = matching.audio_path(target)
    di_path = matching._di_or_default(di)
    target_audio, rate = load_audio(target_path)
    span = matching.resolve_region(region, target_path)
    if span:
        target_audio = span.slice(target_audio, rate, label="the target")
    di_audio, di_rate = load_audio(di_path)
    if di_rate != rate:
        di_audio = resample(di_audio, di_rate, rate)
    if comparison is ComparisonMode.PERFORMANCE:
        di_audio = di_audio[: len(target_audio) + rate // 2]

    space = _space(draft_id, knobs)
    run = TuneRun(
        run_id=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4],
        target=str(target_path),
        di=str(di_path),
        renderer=renderer,
        mode=comparison.value,
        region=span.describe() if span else None,
        route_id=route_id,
        draft_id=draft_id,
        voicing=voicing if renderer == "virtual" else None,
        song=song,
    )

    if renderer == "virtual":
        from ..loop.virtual import SimAmp

        def render(values: dict) -> np.ndarray:
            amp = SimAmp(Type=voicing)
            amp.set(**{key.split(".", 1)[1]: value for key, value in values.items() if key.startswith("Amp")})
            return amp.process(di_audio, rate)

        run.notes.append("rendered on the simulated amp - a rehearsal of the search, not the Core's tone")
    else:
        from ..loop.run import reamp as loop_reamp
        from . import loop as hardware_loop

        route = hardware_loop.get_route(route_id)
        run.route_id = route.route_id
        backend = hardware_loop.backend_for(route)
        sink = VirtualSink() if route.backend == "virtual" else DeviceSink()
        if route.backend == "virtual":
            run.notes.append("tuned on the simulated bench, not a real Core")

        def render(values: dict) -> np.ndarray:
            sink.apply(space.by_node(values))
            capture = loop_reamp(route, di_audio, backend=backend)
            audio = capture.wet
            if route.sample_rate != rate:
                audio = resample(audio, route.sample_rate, rate)
            return audio

    def evaluate(values: dict):
        audio = render(values)
        if comparison is ComparisonMode.PERFORMANCE:
            audio = audio[: len(target_audio)]
        scored = score_candidate(target_audio, audio, rate, mode=comparison)
        return scored.composite, {m.name: round(m.value, 5) for m in scored.metrics}

    result = optimise(space, evaluate, budget=budget, method=method, progress=progress)
    run.result = result.to_dict()
    best = result.best
    if best is not None:
        audio = render(best.values)
        directory = _dir() / run.run_id
        run.best_audio = str(save_audio(directory / "best.wav", audio, rate))
        plan = match_eq.match(target_audio, audio[: len(target_audio)] if comparison is ComparisonMode.PERFORMANCE else audio, rate)
        run.eq = plan.to_dict()
        if renderer == "loop":
            # Leave the device where the search ended up, not on its last try.
            pass
    run.save()
    if song and best is not None:
        from . import songs

        try:
            songs.record_result(
                song["song_id"], song["part_id"],
                label=f"tuned {run.run_id}", composite=best.loss, mode=run.mode, path=run.best_audio,
            )
        except Exception as exc:
            run.notes.append(f"could not record the result on the song: {exc}")
            run.save()
    return run


def apply(run_id: str, draft_id: str, *, with_eq: bool = False) -> str:
    """Write a run's best settings into a draft (not to the device)."""
    from ..device.writer import resolve_block
    from ..rig.catalog import get_catalog
    from . import rigs

    require_writable("apply tuned settings")
    run = get_run(run_id)
    best = (run.result or {}).get("best")
    if not best:
        raise Invalid("that run has no result to apply")
    draft = rigs.get(draft_id)
    catalog = get_catalog()
    nodes = {}
    for block in draft.rig.blocks:
        try:
            nodes[resolve_block(block, catalog).node] = block.slot
        except Exception:
            continue
    changed = []
    for key, value in best["values"].items():
        node, param = key.split(".", 1)
        slot = nodes.get(node)
        if slot is None:
            continue
        rigs.set_param(draft_id, slot, param, value)
        changed.append(f"slot {slot} {param}={value:.3f}" if isinstance(value, float) else f"slot {slot} {param}={value}")
    if with_eq and run.eq:
        amp_slot = next((nodes[n] for n in nodes if n.startswith("Amp") or n.startswith("Neural")), None)
        if amp_slot is not None:
            for name, value in (run.eq.get("slot") or {}).items():
                if name == "SltEQOn":
                    rigs.set_param(draft_id, amp_slot, name, bool(value))
                    continue
                # Slot EQ controls are 0-1 on the wire like every other.
                spans = {"SltEQF": (20.0, 16000.0, True), "SltEQG": (-18.0, 18.0, False), "SltEQQ": (0.2, 10.0, False), "SltEQHP": (25.0, 1000.0, False), "SltEQLP": (1000.0, 16000.0, False)}
                lo, hi, logarithmic = spans[name]
                if logarithmic:
                    import math

                    wire = (math.log(value) - math.log(lo)) / (math.log(hi) - math.log(lo))
                else:
                    wire = (value - lo) / (hi - lo)
                rigs.set_param(draft_id, amp_slot, name, round(min(max(wire, 0.0), 1.0), 4))
            changed.append("slot EQ on the amp")
    run.applied_to = draft_id
    run.save()
    if not changed:
        return f"Nothing in '{draft_id}' matches the tuned blocks."
    return f"Applied to '{draft_id}': " + ", ".join(changed) + ". Review and write it as usual."

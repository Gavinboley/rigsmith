"""Tools for the hardware loop: reamp through the real Core and measure it."""

from __future__ import annotations

from ...core import loop
from ...core.jobs import get_manager
from .._shared import tool


@tool
def loop_status() -> str:
    """The loop routes set up, which is default, and their calibration.

    A route is how a DI reaches the Core and comes back: an interface output
    into the Core's guitar input and its output back in (analog), the Core's
    own USB (usb-reamp), or a mix of both (hybrid). Check this before
    reamping; suggest_loop_routes lists what fits the connected hardware.
    """
    routes = loop.list_routes()
    lines = [loop.render_routes(routes, loop.store().default_id())]
    for route in routes:
        found = loop.calibration(route.route_id)
        if found:
            lines.append("\n" + found.describe())
    return "\n".join(lines)


@tool
def check_bench_rig() -> str:
    """Whether the Core's loaded rig can serve as autopilot's device bench.

    Reads the loaded chain (read only) and says whether there is an Amp and a
    Cab block for models to be switched on. Check before running autopilot or
    tuning on the device bench.
    """
    from ...core import tuning

    return tuning.bench_check().describe()


@tool
def make_bench_rig_draft() -> str:
    """Make a draft with one amp and one cab to use as the device bench rig.

    Writes nothing to the device; the user writes the draft through the usual
    gated write (write_rig), which backs up what is there first.
    """
    from ...core import tuning

    draft_id = tuning.bench_rig_draft()
    return f"draft '{draft_id}' holds one amp and one cab. Writing it is write_rig('{draft_id}'), which asks first."


@tool
def suggest_loop_routes(virtual: bool = False) -> str:
    """Routes that fit the audio devices connected right now.

    ``virtual`` lists the simulated bench instead - useful for demonstrating
    the flow with no hardware, and every capture made on it says so.
    """
    found = loop.suggest_routes("virtual" if virtual else None)
    if not found:
        return "No interface or HeadRush is visible. Use virtual=True for the simulated bench."
    return "\n\n".join(r.describe() + (f"\n  wiring: {r.note}" if r.note else "") for r in found)


@tool
def adopt_loop_route(route_id: str, virtual: bool = False) -> str:
    """Save a suggested route and make it the default."""
    return loop.adopt_suggestion(route_id, backend="virtual" if virtual else None).describe()


@tool
def calibrate_loop(
    route_id: str | None = None,
    di: str | None = None,
    profile_id: str | None = None,
    direct: str | None = None,
    direct_is_reference: bool = False,
) -> str:
    """Measure latency, noise and response, and set the send level.

    The send level is what makes reamps honest: an interface output is much
    hotter than a guitar, and an amp model's gain only means something at
    the level a guitar would give it. Pass the user's DI (``di`` or a
    ``profile_id``) and, ideally, ``direct`` - their guitar played straight
    into the Core and recorded. Runs in the background; returns a job id.
    """
    job = get_manager().submit(
        "calibrate the loop",
        lambda progress: loop.calibrate(
            route_id, di=di, profile_id=profile_id, direct=direct,
            direct_is_reference=direct_is_reference, progress=progress,
        ),
    )
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def loop_reamp(di: str, label: str = "loop", route_id: str | None = None, file_as_take: bool = True) -> str:
    """Send a DI round the hardware loop and keep what comes back, aligned to it.

    The return is on the DI's own timeline (latency, drift and polarity are
    measured from sync marks every pass), so it can be compared sample for
    sample with anything else rendered from the same DI. Load the rig to
    test on the Core first. Runs in the background; returns a job id.
    """
    job = get_manager().submit(
        f"reamp {label}",
        lambda progress: loop.reamp(di, route_id=route_id, label=label, file_as_take=file_as_take, progress=progress),
    )
    return f"Started job {job.id}. Check it with job_status('{job.id}')."


@tool
def assess_capture(target: str, capture: str) -> str:
    """Sample-aligned comparison of two renditions of the *same* DI.

    ESR, null depth and the bands that differ, with NAM's verdict language
    (under 0.01 indistinguishable). Only for the same performance - against a
    record use score_candidates in distribution or performance mode.
    """
    return loop.assess(target, capture).render()


@tool
def offline_hardware_agreement(di: str, nam_path: str | None = None, ir_path: str | None = None) -> str:
    """How well offline rendering predicts the Core for one capture and IR.

    Load the same capture and IR on the Core first. A close agreement means
    offline sweeps can be trusted to shortlist for this rig.
    """
    assessment, hardware = loop.model_agreement(di, nam_path=nam_path, ir_path=ir_path)
    return assessment.render() + f"\n\nhardware capture: {hardware.path}"

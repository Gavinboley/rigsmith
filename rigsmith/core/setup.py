"""What is set up and what is not - the first-run checklist.

Autopilot works with nothing set up (a synthetic guitar, the simulated
bench), and says so in every result; each step here replaces one of those
stand-ins with the real thing. The checklist exists so that a new user finds
that out from the page instead of from a footnote under a ranking.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class Step:
    step_id: str
    title: str
    done: bool
    detail: str
    panel: str
    """Where in the app it is done."""
    optional: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def _guitar() -> Step:
    from . import profiles

    real = [p for p in profiles.list_profiles() if p.samples and "demo" not in (p.guitar or "").lower()]
    if real:
        best = max(real, key=lambda p: len(p.samples))
        return Step("guitar", "Teach it your guitar", True, f"{best.label}: {len(best.samples)} notes learned", "guitar")
    return Step(
        "guitar", "Teach it your guitar", False,
        "One guided minute of playing, and every part is rendered on your guitar instead of a synthetic string.",
        "guitar",
    )


def _core() -> Step:
    from . import devices

    found = devices.sessions()
    if found:
        return Step("core", "Pair with the Core", True, f"paired with {found[0]['host']}", "device")
    return Step(
        "core", "Pair with the Core", False,
        "Needed to write a rig. The Core shows a PIN; you accept it on the device.",
        "device",
    )


def _midi() -> Step:
    """MIDI over the Core's USB cable: loading a rig to reamp through, and
    checking a write on a channel independent of the one that made it."""
    try:
        from ..device.midi import find_headrush_port, list_ports

        outputs, _ = list_ports()
    except Exception as exc:
        missing = "pip install" in str(exc)
        detail = (
            "Install MIDI support (pip install 'rigsmith[midi]') to load rigs and verify writes over the Core's USB cable."
            if missing else f"MIDI is installed but could not list ports: {str(exc).splitlines()[0]}"
        )
        return Step("midi", "MIDI over USB", False, detail, "device", optional=True)
    port = find_headrush_port(outputs)
    if port:
        return Step("midi", "MIDI over USB", True, f"the Core is on {port}", "device", optional=True)
    return Step("midi", "MIDI over USB", False, "MIDI is ready; plug the Core in over USB and it shows up here.", "device", optional=True)


def _isolation() -> Step:
    """Pulling the guitar out of a record, so a part is measured on its own."""
    from ..match.separate import demucs_available

    if demucs_available():
        return Step("isolation", "Guitar isolation", True, "Demucs is installed - a song's guitar is pulled out of the mix before it is mapped", "songs", optional=True)
    return Step(
        "isolation", "Guitar isolation", False,
        "Install Demucs (pip install 'rigsmith[separation]', a few GB) to pull the guitar out of a record. "
        "Without it parts are measured from the full mix: guitars panned left and right still come out "
        "mostly on their own, a centred one shares its layer with the vocals, bass and drums.",
        "songs", optional=True,
    )


def _loop() -> Step:
    from . import loop

    routes = loop.list_routes()
    real = [r for r in routes if r.backend != "virtual"]
    if real:
        calibrated = [r for r in real if loop.calibration(r.route_id)]
        detail = f"route {real[0].route_id}" + ("" if calibrated else " - not calibrated yet, so the send level is a guess")
        return Step("loop", "Wire the hardware loop", bool(calibrated), detail, "loop", optional=True)
    return Step(
        "loop", "Wire the hardware loop", False,
        "Interface out to the Core and back, so candidates are heard through the real thing rather than a simulation.",
        "loop", optional=True,
    )


def _song() -> Step:
    from . import songs

    found = songs.list_songs()
    if found:
        mapped = [s for s in found if s.parts]
        return Step("song", "Add a song you own", True, f"{len(found)} song(s), {len(mapped)} mapped", "songs")
    return Step("song", "Add a song you own", False, "Your copy of the record - autopilot maps it into parts.", "songs")


def _brains() -> Step:
    from . import appsettings

    settings = appsettings.load()
    try:
        from ..agent import providers

        state = providers.status()
    except Exception as exc:  # the checklist must not fail on a probe
        return Step("brains", "Choose who answers", False, f"could not check: {exc}", "settings", optional=True)
    if settings.mode == "local":
        ok = state["local"].get("reachable")
        detail = (
            f"local mode, {len(state['local'].get('models') or [])} model(s) loaded"
            if ok else "local mode, but no local model server is answering - start LM Studio, Ollama or llama.cpp"
        )
        return Step("brains", "Choose who answers", bool(ok), detail, "settings", optional=True)
    claude = state["claude_code"] and state.get("claude_code_signed_in") is not False
    api = state["anthropic_sdk"] and state["anthropic_key"]
    ok = claude or api or state["local"].get("reachable")
    detail = "accuracy mode: " + (
        "Claude Code" if claude else "the Anthropic API" if api else "a local model" if state["local"].get("reachable") else "nothing available - install Claude Code, or start a local model"
    )
    if state["claude_code"] and state.get("claude_code_signed_in") is False:
        detail += " - Claude Code is installed but not signed in: run `claude` in a terminal and use /login"
    return Step("brains", "Choose who answers", bool(ok), detail, "settings", optional=True)


def checklist() -> list[Step]:
    return [_guitar(), _song(), _isolation(), _core(), _midi(), _loop(), _brains()]


def render(steps: list[Step]) -> str:
    lines = []
    for step in steps:
        mark = "[x]" if step.done else "[ ]"
        extra = " (optional)" if step.optional and not step.done else ""
        lines.append(f"{mark} {step.title}{extra} - {step.detail}")
    missing = [s for s in steps if not s.done and not s.optional]
    lines.append("")
    lines.append("Everything essential is set up." if not missing else f"Next: {missing[0].title.lower()}.")
    return "\n".join(lines)

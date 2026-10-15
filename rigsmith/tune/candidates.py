"""Which of the Core's amps to try, given what a part sounds like.

When research has named the amp, that is the first candidate. When it has
not - or to check it - the measured gain of the part narrows a hundred amp
models to the handful worth reamping: a clean part is not going to be a
Treadplate, and a palm-muted modern riff is not a Tweed Deluxe. The tiers
below are drawn from the Core's own model list, by the amp each model is
named after; they are a starting field, not a verdict, and the loop is what
decides between them.

Each tier also suggests a cab and a few blocks around the amp, the way the
tone sketch describes the part.
"""

from __future__ import annotations

from dataclasses import dataclass

#: (tier, amp model names in the Core's catalog, a cab that suits them)
TIERS: dict[str, tuple[tuple[str, ...], str]] = {
    "clean": (
        ("64 Black Lux Norm", "65 Black SR", "59 Tweed Deluxe", "67 Black Duo",
         "89 SL-100 Clean", "99 PV51 II Clean", "11 EPB II Clean", "65 Black Prince"),
        "1x12 Black Panel Lux",
    ),
    "edge": (
        ("59 Deluxe Gain Mod", "66 AC Hi Boost", "68 Plexiglas 50W", "05 Tangerine 30 Ch1",
         "64 Black Lux Vib", "62 BluesMaker", "64 Fox AC30"),
        "2x12 AC Blue",
    ),
    "crunch": (
        ("69 Plexiglas 100W", "68 Plexi EL84 Mod", "82 Lead 800 50W", "89 SL-100 Crunch",
         "05 Tangerine 30 Ch2", "11 EPB II Crunch", "67 Plexiglas Vari"),
        "4x12 Green 25W",
    ),
    "high": (
        ("82 Lead 800 100W", "82 Lead 800 TS Mod", "82 Lead 800 Bass Mod", "85 M-2 Lead",
         "89 SL-100 Drive", "11 EPB II Lo-Lead", "82 Lead 800 Bright"),
        "4x12 Green 20W",
    ),
    "modern": (
        ("92 Treadplate Modern", "92 Treadplate Raw", "97 RB-01B Red", "99 PV51 II Lead",
         "11 EPB II Hi-Lead", "05 Peavey 6505+ Lead", "03 Herr Demon Ch3", "97 RB-01B Blue"),
        "4x12 Classic 30W",
    ),
}

ORDER = ("clean", "edge", "crunch", "high", "modern")

#: A simulated amp voicing per tier, for the virtual bench.
VIRTUAL_VOICING = {"clean": 0, "edge": 1, "crunch": 1, "high": 2, "modern": 3}

#: Cabs worth hearing per tier, the usual one first. After the amp, the cab
#: is the biggest single thing in a guitar tone - a Greenback 4x12 and a V30
#: 4x12 on the same head are different records - so the best amp gets heard
#: through each of these before anything is tuned.
CAB_CHOICES: dict[str, tuple[tuple[str, str], ...]] = {
    "clean": (
        ("1x12 Black Panel Lux", "the usual clean cab"),
        ("2x12 Black Panel Duo", "a 2x12 - wider, more low end"),
        ("1x12 Tweed Lux", "a tweed 1x12 - warmer, looser"),
        ("2x12 Silver Cone", "Silver Cones - chimier top"),
    ),
    "edge": (
        ("2x12 AC Blue", "the usual edge-of-breakup cab"),
        ("2x12 Silver Cone", "Silver Cones - softer top"),
        ("1x12 Tweed Lux", "a tweed 1x12 - looser"),
        ("4x12 Green 25W", "a Greenback 4x12 - thicker mids"),
    ),
    "crunch": (
        ("4x12 Green 25W", "the usual crunch cab"),
        ("4x12 Green 20W", "earlier Greenbacks - darker"),
        ("4x12 Classic 30W", "V30s - more upper-mid bark"),
        ("2x12 B30", "a 2x12 - tighter, less low end"),
    ),
    "high": (
        ("4x12 Green 20W", "the usual high-gain cab"),
        ("4x12 Green 25W", "later Greenbacks - a touch brighter"),
        ("4x12 Classic 30W", "V30s - more upper-mid bark"),
        ("4x12 65W", "65W speakers - flatter, more headroom"),
    ),
    "modern": (
        ("4x12 Classic 30W", "the usual modern cab"),
        ("2x12 B30", "a 2x12 - tighter"),
        ("4x12 65W", "65W speakers - flatter"),
        ("4x12 Green 25W", "Greenbacks - rounder, less fizz"),
    ),
}


def virtual_cab(name: str) -> int:
    """Which simulated cab stands in for a Core cab on the virtual bench."""
    lowered = (name or "").lower()
    if lowered.startswith("1x") or " 112" in lowered or "1x12" in lowered:
        return 0
    if "green" in lowered or "greenback" in lowered:
        return 3
    if any(word in lowered for word in ("classic 30", "b30", "v30", "65w", "412", "4x12")):
        return 2
    return 1


def tier_for(gain: float) -> str:
    """Where a measured gain (0-1) lands."""
    if gain < 0.2:
        return "clean"
    if gain < 0.38:
        return "edge"
    if gain < 0.58:
        return "crunch"
    if gain < 0.78:
        return "high"
    return "modern"


@dataclass
class Candidate:
    model: str
    tier: str
    cab: str
    reason: str
    device_index: int | None = None
    voicing: int | None = None
    """For the virtual bench."""
    selector: str | None = None
    """How the device picks this model: ``Amp.Type``, ``ReValver Amp.Type``.
    None means the plain Amp block's ``Type``."""
    cab_index: int | None = None
    cab_selector: str | None = None
    cab_voicing: int | None = None
    """The simulated cab, for the virtual bench; None keeps the amp's own."""

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "tier": self.tier,
            "cab": self.cab,
            "reason": self.reason,
            "device_index": self.device_index,
            "voicing": self.voicing,
            "selector": self.selector,
            "cab_index": self.cab_index,
            "cab_selector": self.cab_selector,
            "cab_voicing": self.cab_voicing,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Candidate":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def device_values(self) -> dict[str, dict]:
        """What to set on the device's live patch to hear this candidate."""
        values: dict[str, dict] = {}
        for selector, index in ((self.selector or "Amp.Type", self.device_index), (self.cab_selector or "Cab.CabType", self.cab_index)):
            if index is None:
                continue
            module, parameter = selector.split(".", 1)
            values.setdefault(module.replace(" ", "_"), {})[parameter] = index
        return values


def _lookup(catalog, name: str) -> tuple[bool, int | None, str | None]:
    """(allowed, device index, selector) for a model name."""
    if catalog is None:
        return True, None, None
    spec = catalog.get(name)
    verified = catalog.is_verified() if callable(getattr(catalog, "is_verified", None)) else bool(getattr(catalog, "is_verified", False))
    if spec is None:
        return (not verified), None, None
    return True, getattr(spec, "device_index", None), getattr(spec, "selector", None)


def cab_options(tier: str, *, researched: list[str] | None = None, catalog=None, count: int = 4) -> list[tuple[str, str, int | None, str | None]]:
    """Cabs to hear the best amp through: (name, why, device index, selector).

    Researched cabs first, then the tier's usual one and its alternatives.
    A name the device's catalog does not list is dropped.
    """
    found: list[tuple[str, str, int | None, str | None]] = []
    seen: set[str] = set()
    for name, why in [(n, "named by the research") for n in researched or []] + list(CAB_CHOICES.get(tier, ())):
        if name in seen:
            continue
        ok, index, selector = _lookup(catalog, name)
        if ok:
            found.append((name, why, index, selector))
            seen.add(name)
        if len(found) >= count:
            break
    return found


def with_cab(candidate: Candidate, cab: str, *, why: str, index: int | None = None, selector: str | None = None) -> Candidate:
    """The same amp through another cab."""
    from dataclasses import replace

    return replace(
        candidate, cab=cab, reason=f"{candidate.reason}; {why}", cab_index=index,
        cab_selector=selector, cab_voicing=virtual_cab(cab),
    )


def _tier_of(name: str, fallback: str) -> str:
    """The tier a model is listed under, or the measured one if it is not."""
    return next((tier for tier, (names, _) in TIERS.items() if name in names), fallback)


def candidates(
    gain: float, *, researched: list[str] | None = None, favoured: list[str] | None = None,
    count: int = 6, catalog=None,
) -> list[Candidate]:
    """Amps to try: researched ones first, then the measured tier and its neighbours.

    Models the catalog does not list are dropped, so a list built against
    one firmware never sends a stale name to another.
    """
    tier = tier_for(gain)
    index = ORDER.index(tier)
    neighbours = [ORDER[i] for i in (index - 1, index + 1) if 0 <= i < len(ORDER)]

    # Only a catalog read off a real device is allowed to veto a name (see
    # _lookup). The seed catalog knows a handful of models; filtering
    # against it would leave nothing to try.
    def known(name: str):
        ok, index, _ = _lookup(catalog, name)
        return ok, index

    def selector_of(name: str):
        return _lookup(catalog, name)[2]

    found: list[Candidate] = []
    seen: set[str] = set()
    # What the user's ears chose on an earlier run comes first, then what
    # the research names - each through its own tier's cab and voicing.
    for names, reason in ((favoured or [], "your ears chose it last time"), (researched or [], "named by the research")):
        for name in names:
            ok, device_index = known(name)
            if ok and name not in seen:
                own = _tier_of(name, tier)
                found.append(Candidate(name, own, TIERS[own][1], reason, device_index, VIRTUAL_VOICING[own], selector_of(name)))
                seen.add(name)
    primary, cab = TIERS[tier]
    for name in primary:
        if len(found) >= max(1, count - 2):
            break
        ok, device_index = known(name)
        if ok and name not in seen:
            found.append(Candidate(name, tier, cab, f"the part measures as {tier}", device_index, VIRTUAL_VOICING[tier], selector_of(name)))
            seen.add(name)
    # Neighbours take turns - one from each tier either side before a second
    # from either - so a measurement that is off by a step in *either*
    # direction still has its answer in the field.
    for depth in range(2):
        for other in neighbours:
            names, other_cab = TIERS[other]
            if depth >= len(names) or len(found) >= count:
                continue
            for name in names[depth:]:
                ok, device_index = known(name)
                if ok and name not in seen:
                    found.append(Candidate(name, other, other_cab, f"a {other} amp, in case the measurement is off by a step", device_index, VIRTUAL_VOICING[other], selector_of(name)))
                    seen.add(name)
                    break
    # Every amp is heard through its tier's cab, set explicitly - so the
    # comparison is amp against amp, not against whatever cab the loaded rig
    # happened to have, and the draft built from the winner sounds the same.
    for candidate in found:
        _, candidate.cab_index, candidate.cab_selector = _lookup(catalog, candidate.cab)
        candidate.cab_voicing = virtual_cab(candidate.cab)
    return found[:count]


#: The delay's tempo-synced divisions on the Core, in its own order, and the
#: sketch's note names for them.
DELAY_DIVISIONS = ("1/128", "1/64", "1/32T", "1/32", "1/16T", "1/16", "1/8T", "1/8", "1/4T", "3/16", "1/4", "1/2T", "3/8", "1/2", "5/8", "3/4", "Bar")
NOTE_TO_DIVISION = {
    "sixteenth": "1/16", "eighth": "1/8", "dotted eighth": "3/16", "triplet quarter": "1/4T",
    "quarter": "1/4", "dotted quarter": "3/8", "half": "1/2",
}
#: The modulation blocks' synced rates - one cycle per division - in the
#: Core's order, which is the delay's reversed.
RATE_DIVISIONS = tuple(reversed(DELAY_DIVISIONS))
#: Each division's length in beats, in 4/4.
DIVISION_BEATS = {
    "Bar": 4.0, "3/4": 3.0, "5/8": 2.5, "1/2": 2.0, "3/8": 1.5, "1/2T": 4 / 3, "1/4": 1.0, "3/16": 0.75,
    "1/4T": 2 / 3, "1/8": 0.5, "1/8T": 1 / 3, "1/16": 0.25, "1/16T": 1 / 6, "1/32": 0.125, "1/32T": 1 / 12,
    "1/64": 1 / 16, "1/128": 1 / 32,
}
#: How close a measured rate has to be to a division to be synced to it.
SYNC_TOLERANCE = 0.04


def synced_rate(hz: float | None, tempo: float | None) -> str | None:
    """The division a modulation rate is at the song's tempo, or None.

    A tremolo on a record is usually set to the beat, by the player's ear if
    not by a sync switch, and one that drifts against the drums is the
    first thing that sounds wrong. Within :data:`SYNC_TOLERANCE` of a
    division, the synced rate is as close to what was measured as the
    measurement is, and stays on the beat.
    """
    if not hz or not tempo or hz <= 0 or tempo <= 0:
        return None
    beats = tempo / (60.0 * hz)
    nearest = min(DIVISION_BEATS, key=lambda name: abs(beats - DIVISION_BEATS[name]) / DIVISION_BEATS[name])
    error = abs(beats - DIVISION_BEATS[nearest]) / DIVISION_BEATS[nearest]
    return nearest if error <= SYNC_TOLERANCE else None


#: AIR Reverb's modes, by what the sketch heard.
REVERB_MODES = {"a room": 0, "a plate or hall": 1, "a long wash": 7}


def _linear(value: float, low: float, high: float) -> float:
    """A display value as a wire value, assuming a linear control.

    The real taper is measured by catalog calibration; until it is, this is
    a starting point that the run says is one.
    """
    return round(min(max((value - low) / (high - low), 0.0), 1.0), 4)


def surrounding_blocks(sketch: dict, *, tempo: float | None = None) -> list[dict]:
    """Blocks the sketch suggests around the amp: ``{category, model, params, role, why}``.

    Where the measurement gives a number it is set: a delay synced to the
    division it measured (and its time in milliseconds as the unsynced
    fallback), a tremolo's or chorus's rate, a reverb's type, and mixes kept
    modest - a record's effects sit under the guitar, not on top of it. The
    millisecond and hertz settings assume a linear control until the catalog
    is calibrated; the sync division does not depend on that. Given the
    song's ``tempo``, a tremolo on the beat is synced too (see
    :func:`synced_rate`).
    """
    blocks: list[dict] = []
    if sketch.get("gated"):
        blocks.append({"category": "effect", "model": "Gate", "role": "gate", "params": {}, "why": "the gaps between notes are silent"})
    gain = float(sketch.get("gain") or 0)
    if gain >= 0.58 and sketch.get("low_end") != "heavy low end":
        blocks.append({"category": "effect", "model": "Green JRC-OD", "role": "boost", "params": {}, "why": "a tight high-gain part usually has a boost in front"})
    if sketch.get("delay_ms"):
        params = {"DelayTime": _linear(float(sketch["delay_ms"]), 20.0, 800.0), "Mix": round(0.2 + 0.25 * min(float(sketch.get("delay_strength") or 0), 1.0), 3)}
        division = NOTE_TO_DIVISION.get(sketch.get("delay_note") or "")
        if division:
            params.update({"Sync": True, "DelayTime_Synced": DELAY_DIVISIONS.index(division)})
        blocks.append({
            "category": "effect", "model": "M104 Analog Delay", "role": "delay", "params": params,
            "why": f"a {sketch['delay_ms']:.0f} ms echo" + (f" ({sketch['delay_note']}, synced to {division})" if division else ""),
            "delay_ms": sketch["delay_ms"], "division": division,
        })
    modulation = sketch.get("modulation") or ""
    hz = sketch.get("modulation_hz")
    rate = f" at {hz:.1f} Hz" if hz else ""
    if modulation == "tremolo":
        params = {"Rate": _linear(float(hz), 0.1, 12.0)} if hz else {}
        division = synced_rate(hz, tempo)
        if division:
            params.update({"Sync": True, "Rate_Synced": RATE_DIVISIONS.index(division)})
        blocks.append({
            "category": "effect", "model": "Tremolo", "role": "tremolo", "params": params,
            "why": f"a {hz or 0:.1f} Hz wobble" + (f" (a {division} cycle, synced)" if division else ""),
            "division": division,
        })
    elif modulation == "chorus":
        params = {"Rate": _linear(float(hz), 0.1, 10.0), "Mix": 0.5} if hz else {"Mix": 0.5}
        blocks.append({"category": "effect", "model": "Chorus", "role": "chorus", "params": params, "why": f"sustained notes wander about {sketch.get('wobble_cents') or 0:.0f} cents{rate}"})
    elif modulation == "vibrato":
        blocks.append({"category": "effect", "model": "Vibrato", "role": "vibrato", "params": {}, "why": f"every note wobbles in pitch{rate}"})
    space = sketch.get("space") or ""
    if space in ("a plate or hall", "a long wash"):
        blocks.append({"category": "effect", "model": "AIR Reverb", "role": "reverb", "params": {"Mode": REVERB_MODES[space], "Mix": 0.25}, "why": f"{space} on the part"})
    elif space == "a room":
        blocks.append({"category": "effect", "model": "Spring Reverb", "role": "reverb", "params": {"Mix": 0.2}, "why": "a short room"})
    return blocks

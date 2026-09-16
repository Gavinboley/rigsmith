"""ToneRecipe: the contract between research and hardware.

Recreating a recorded tone starts as research - interviews, rig rundowns,
photographs, someone's forum post from 2009 - and ends as a rig on a device.
Between those two is a gap where most of the reasoning gets lost: the agent
decides on a Big Muff into a JMP, builds it, and the *why* evaporates.

A recipe is that middle. It records what gear the tone is believed to have
used, **where each claim came from, and how confident it is**, and compiles
into a draft rig. Three things follow from that:

* You can review the reasoning before anything is built.
* You can tell a documented fact from a plausible guess, which is exactly the
  distinction that disappears when an agent presents both in the same voice.
* When the rig sounds wrong, you can see which assumption to attack.

Recipes are plain JSON. They are meant to be read, edited, shared, and argued
with.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from ..core.errors import Invalid
from ..rig.model import BlockCategory


class Confidence(str, Enum):
    """How well supported a claim is.

    The distinction that matters most is ``STATED`` versus everything else:
    somebody who was there said so, as against somebody reasoning backwards
    from a record. Both are useful; conflating them is not.
    """

    STATED = "stated"
    """Documented - the player, producer, or an equipment list says so."""

    REPORTED = "reported"
    """Second-hand but specific: a rig rundown, a photograph, a credible fan
    catalogue."""

    INFERRED = "inferred"
    """Reasoned from evidence - the era, the rest of the rig, what that sound
    usually comes from."""

    GUESSED = "guessed"
    """A plausible starting point and nothing more."""

    @property
    def weight(self) -> float:
        return {
            Confidence.STATED: 1.0,
            Confidence.REPORTED: 0.75,
            Confidence.INFERRED: 0.45,
            Confidence.GUESSED: 0.2,
        }[self]

    @property
    def symbol(self) -> str:
        return {
            Confidence.STATED: "***",
            Confidence.REPORTED: "** ",
            Confidence.INFERRED: "*  ",
            Confidence.GUESSED: "?  ",
        }[self]


@dataclass
class Source:
    """Where a claim came from."""

    title: str
    url: str | None = None
    kind: str = "web"
    """``interview``, ``rig-rundown``, ``liner-notes``, ``forum``,
    ``photo``, ``listening``, ``web``."""
    note: str | None = None
    """What this source actually says, in a sentence. Worth the keystrokes:
    a bare URL rots, and a quote survives the link going dead."""

    def render(self) -> str:
        bits = [self.title]
        if self.kind and self.kind != "web":
            bits.append(f"({self.kind})")
        if self.url:
            bits.append(self.url)
        line = " ".join(bits)
        if self.note:
            line += f"\n        \"{self.note}\""
        return line

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class GearChoice:
    """One element of the chain, and the case for it."""

    role: str
    """What it does here: ``drive``, ``amp``, ``cab``, ``delay``, ``modulation``."""
    name: str
    """What it is, as a human would say it: "Big Muff Pi"."""
    category: str = "effect"
    """Which kind of rigsmith block this becomes."""
    make: str | None = None
    model: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    confidence: Confidence = Confidence.GUESSED
    sources: list[Source] = field(default_factory=list)
    alternatives: list[str] = field(default_factory=list)
    """Other things it might have been. Keeping the runners-up makes the next
    iteration cheap when the first choice does not sit right."""
    note: str | None = None

    #: Filled in once a capture or catalog model has been chosen for it.
    resolved_model_id: str | None = None

    @property
    def block_category(self) -> BlockCategory:
        try:
            return BlockCategory(self.category.lower())
        except ValueError:
            return BlockCategory.EFFECT

    @property
    def supported(self) -> bool:
        """Whether anything backs this up beyond someone's hunch."""
        return bool(self.sources) or self.confidence in (
            Confidence.STATED,
            Confidence.REPORTED,
        )

    def render(self) -> str:
        gear = " ".join(p for p in (self.make, self.model) if p)
        header = f"  {self.confidence.symbol} {self.role:12} {self.name}"
        if gear and gear.lower() not in self.name.lower():
            header += f"  [{gear}]"
        lines = [header]
        if self.settings:
            rendered = ", ".join(f"{k} {v}" for k, v in sorted(self.settings.items()))
            lines.append(f"        {rendered}")
        if self.note:
            lines.append(f"        {self.note}")
        for source in self.sources:
            lines.append(f"        - {source.render()}")
        if self.alternatives:
            lines.append(f"        or: {', '.join(self.alternatives)}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = {
            "role": self.role,
            "name": self.name,
            "category": self.category,
            "make": self.make,
            "model": self.model,
            "settings": self.settings,
            "confidence": self.confidence.value,
            "sources": [s.to_dict() for s in self.sources],
            "alternatives": self.alternatives,
            "note": self.note,
            "resolved_model_id": self.resolved_model_id,
        }
        return {k: v for k, v in data.items() if v not in (None, [], {})}

    @classmethod
    def from_dict(cls, data: dict) -> "GearChoice":
        return cls(
            role=data["role"],
            name=data["name"],
            category=data.get("category", "effect"),
            make=data.get("make"),
            model=data.get("model"),
            settings=dict(data.get("settings") or {}),
            confidence=Confidence(data.get("confidence", "guessed")),
            sources=[Source(**s) for s in data.get("sources", [])],
            alternatives=list(data.get("alternatives") or []),
            note=data.get("note"),
            resolved_model_id=data.get("resolved_model_id"),
        )


@dataclass
class SceneSpec:
    """A part of the song, and what the rig does for it."""

    name: str
    """"Clean verse", "Big chorus", "Solo"."""
    roles_on: list[str] = field(default_factory=list)
    """Gear roles active in this part - roles, not slots, because slots do
    not exist until the recipe is compiled."""
    roles_off: list[str] = field(default_factory=list)
    footswitch: int | None = None
    note: str | None = None

    def render(self) -> str:
        bits = [f"  {self.name}"]
        if self.footswitch:
            bits.append(f"FS{self.footswitch}")
        if self.roles_on:
            bits.append(f"on: {', '.join(self.roles_on)}")
        if self.roles_off:
            bits.append(f"off: {', '.join(self.roles_off)}")
        line = "  ".join(bits)
        if self.note:
            line += f"\n      {self.note}"
        return line

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, [])}


@dataclass
class Measurement:
    """What actually happened when this recipe was tested against the record.

    A recipe without this records only what somebody believed. Confidence
    says how well-sourced a claim is; a measurement says whether acting on it
    worked - a different axis entirely, and the one that settles arguments.

    Kept deliberately thin. The full ranking lives with the audio; what
    belongs on the recipe is the answer and enough context to know what the
    answer was about.
    """

    label: str
    """Which candidate - usually the capture or rig that was tested."""

    target: str
    """What it was compared against, as a filename or a description."""

    kind: str = "score"
    """``score`` for a metric result, ``listen`` for a blind A/B verdict."""

    mode: str | None = None
    """``distribution`` or ``aligned``, for a score."""

    composite: float | None = None
    region: str | None = None
    """The passage that was compared, if it was not the whole file."""

    won: bool | None = None
    """Whether this came first. For a blind test, whether ears chose it."""

    trials: int | None = None
    note: str | None = None
    at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    def describe(self) -> str:
        bits = [f"{self.label}"]
        if self.kind == "listen":
            outcome = "chosen" if self.won else "not chosen"
            bits.append(f"blind test: {outcome}")
            if self.trials:
                bits.append(f"over {self.trials} trial(s)")
        else:
            if self.composite is not None:
                bits.append(f"composite {self.composite:.4f}")
            if self.mode:
                bits.append(f"({self.mode})")
            if self.won:
                bits.append("- ranked first")
        if self.region:
            bits.append(f"over {self.region}")
        bits.append(f"vs {self.target}")
        if self.note:
            bits.append(f"- {self.note}")
        return "  ".join(bits)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "target": self.target,
            "kind": self.kind,
            "mode": self.mode,
            "composite": self.composite,
            "region": self.region,
            "won": self.won,
            "trials": self.trials,
            "note": self.note,
            "at": self.at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Measurement":
        return cls(
            label=str(data.get("label", "")),
            target=str(data.get("target", "")),
            kind=data.get("kind", "score"),
            mode=data.get("mode"),
            composite=data.get("composite"),
            region=data.get("region"),
            won=data.get("won"),
            trials=data.get("trials"),
            note=data.get("note"),
            at=data.get("at")
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


@dataclass
class ToneRecipe:
    """A researched tone, ready to compile into a rig."""

    title: str
    artist: str | None = None
    song: str | None = None
    album: str | None = None
    year: int | None = None

    summary: str | None = None
    """What the tone actually sounds like, in a sentence or two. The thing a
    spectrum plot cannot tell you."""

    gear: list[GearChoice] = field(default_factory=list)
    scenes: list[SceneSpec] = field(default_factory=list)
    signal_path: str = "straight"

    tuning: str | None = None
    guitar: str | None = None
    pickup: str | None = None

    open_questions: list[str] = field(default_factory=list)
    """What is still unknown. Writing these down is the difference between a
    recipe that admits its gaps and one that quietly papers over them."""

    sources: list[Source] = field(default_factory=list)
    """Sources for the recipe as a whole, as opposed to one gear choice."""

    measurements: list[Measurement] = field(default_factory=list)
    """What testing it against the record actually produced. This is the half
    of the loop that research alone never closes: a recipe with measurements
    has been checked, and one without has only been reasoned about."""

    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    # -- inspection -------------------------------------------------------

    def role(self, name: str) -> GearChoice | None:
        lowered = name.lower()
        return next((g for g in self.gear if g.role.lower() == lowered), None)

    @property
    def tone_source(self) -> GearChoice | None:
        """The amp, clone, or capture that defines the voice."""
        return next(
            (g for g in self.gear if g.block_category.is_tone_source), None
        )

    @property
    def confidence(self) -> float:
        """Mean confidence across gear choices, 0-1."""
        if not self.gear:
            return 0.0
        return sum(g.confidence.weight for g in self.gear) / len(self.gear)

    @property
    def unsupported(self) -> list[GearChoice]:
        """Choices resting on nothing but a hunch."""
        return [g for g in self.gear if not g.supported]

    def confidence_report(self) -> str:
        """An honest summary of how much of this is actually known."""
        if not self.gear:
            return "No gear recorded yet."

        counts: dict[str, int] = {}
        for choice in self.gear:
            counts[choice.confidence.value] = counts.get(choice.confidence.value, 0) + 1

        lines = [
            f"Overall confidence: {self.confidence:.0%}",
            "  " + "  ".join(f"{name} {count}" for name, count in sorted(counts.items())),
        ]
        if self.unsupported:
            lines.append(
                "  Unsourced: "
                + ", ".join(f"{g.role} ({g.name})" for g in self.unsupported)
            )
        if self.open_questions:
            lines.append(f"  {len(self.open_questions)} open question(s)")
        return "\n".join(lines)

    def render(self) -> str:
        heading = self.title
        credit = " - ".join(p for p in (self.artist, self.album) if p)
        if credit:
            heading += f"  ({credit}{f', {self.year}' if self.year else ''})"

        lines = [heading]
        if self.summary:
            lines.append(f"\n{self.summary}")

        context = [
            f"guitar: {self.guitar}" if self.guitar else None,
            f"pickup: {self.pickup}" if self.pickup else None,
            f"tuning: {self.tuning}" if self.tuning else None,
        ]
        context = [c for c in context if c]
        if context:
            lines.append("\n" + "   ".join(context))

        if self.gear:
            lines.append("\nchain:")
            lines.extend(choice.render() for choice in self.gear)

        if self.scenes:
            lines.append("\nscenes:")
            lines.extend(scene.render() for scene in self.scenes)

        if self.open_questions:
            lines.append("\nstill unknown:")
            lines.extend(f"  - {question}" for question in self.open_questions)

        if self.sources:
            lines.append("\nsources:")
            lines.extend(f"  - {source.render()}" for source in self.sources)

        if self.measurements:
            lines.append("\nmeasured:")
            lines.extend(f"  - {m.describe()}" for m in self.measurements)
        else:
            lines.append(
                "\nmeasured: nothing yet - this recipe has been reasoned "
                "about, not checked."
            )

        lines.append("\n" + self.confidence_report())
        return "\n".join(lines)

    @property
    def verified(self) -> bool:
        """Has a person actually chosen this by ear?

        A metric result is a ranking, not a verdict - see docs/TONE_MATCHING.md.
        Only a blind listening test settles it, so only that counts here.
        """
        return any(m.kind == "listen" and m.won for m in self.measurements)

    # -- persistence ------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "song": self.song,
            "album": self.album,
            "year": self.year,
            "summary": self.summary,
            "guitar": self.guitar,
            "pickup": self.pickup,
            "tuning": self.tuning,
            "signal_path": self.signal_path,
            "gear": [g.to_dict() for g in self.gear],
            "scenes": [s.to_dict() for s in self.scenes],
            "open_questions": self.open_questions,
            "sources": [s.to_dict() for s in self.sources],
            "measurements": [m.to_dict() for m in self.measurements],
            "created_at": self.created_at,
        }

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        return path

    @classmethod
    def from_dict(cls, data: dict) -> "ToneRecipe":
        if not data.get("title"):
            raise Invalid("a recipe needs a title")
        recipe = cls(
            title=data["title"],
            artist=data.get("artist"),
            song=data.get("song"),
            album=data.get("album"),
            year=data.get("year"),
            summary=data.get("summary"),
            guitar=data.get("guitar"),
            pickup=data.get("pickup"),
            tuning=data.get("tuning"),
            signal_path=data.get("signal_path", "straight"),
            open_questions=list(data.get("open_questions") or []),
            created_at=data.get("created_at")
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        recipe.gear = [GearChoice.from_dict(g) for g in data.get("gear", [])]
        recipe.scenes = [SceneSpec(**s) for s in data.get("scenes", [])]
        recipe.sources = [Source(**s) for s in data.get("sources", [])]
        recipe.measurements = [
            Measurement.from_dict(m) for m in data.get("measurements", [])
        ]
        return recipe

    @classmethod
    def load(cls, path: str | Path) -> "ToneRecipe":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise Invalid(f"{Path(path).name} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)


# --------------------------------------------------------------------------
# compilation
# --------------------------------------------------------------------------

#: The order gear roles conventionally appear in a chain. Anything unlisted
#: keeps its declared order after the last role it is recognised behind.
ROLE_ORDER = [
    "wah", "filter",
    "compressor", "comp",
    "boost", "drive", "overdrive", "distortion", "fuzz",
    "eq", "preamp",
    "amp", "capture", "nam", "clone",
    "cab", "ir",
    "modulation", "chorus", "phaser", "flanger", "tremolo", "vibrato",
    "pitch", "octave",
    "delay", "echo",
    "reverb",
    "volume", "utility",
]


def _role_rank(role: str) -> int:
    lowered = role.lower()
    for index, known in enumerate(ROLE_ORDER):
        if known in lowered:
            return index
    return len(ROLE_ORDER)


@dataclass
class CompileResult:
    """A recipe turned into a draft rig, with anything unresolved called out."""

    draft_id: str
    recipe: ToneRecipe
    placed: list[tuple[int, GearChoice]] = field(default_factory=list)
    unplaced: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        lines = [f"Compiled '{self.recipe.title}' into draft '{self.draft_id}'"]
        for slot, choice in self.placed:
            marker = "" if choice.resolved_model_id else "  (placeholder)"
            lines.append(f"  slot {slot:2}  {choice.role:12} {choice.name}{marker}")
        if self.unplaced:
            lines.append("\nnot placed:")
            lines.extend(f"  {item}" for item in self.unplaced)
        if self.warnings:
            lines.append("")
            lines.extend(f"  warning: {w}" for w in self.warnings)
        lines.append("\n" + self.recipe.confidence_report())

        placeholders = [c for _, c in self.placed if not c.resolved_model_id]
        if placeholders:
            lines.append(
                f"\n{len(placeholders)} block(s) are placeholders named from the "
                "research rather than real device models. Search the catalog or "
                "TONE3000 and set the real ones before writing."
            )
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "draft_id": self.draft_id,
            "placed": [
                {"slot": slot, "role": choice.role, "name": choice.name,
                 "resolved": bool(choice.resolved_model_id)}
                for slot, choice in self.placed
            ],
            "unplaced": self.unplaced,
            "warnings": self.warnings,
            "confidence": self.recipe.confidence,
        }


def compile_recipe(recipe: ToneRecipe, *, name: str | None = None) -> CompileResult:
    """Turn a recipe into a draft rig.

    Gear is ordered by conventional signal-chain position rather than the
    order it was researched in - drives before the amp, cab after it, time
    effects last - because that is what the player means even when the
    research listed things in whatever order they were discovered.

    Scenes reference gear by *role*; this resolves those to the slots the
    gear actually landed in.
    """
    from ..core import rigs
    from ..rig.model import MAX_SLOTS

    ordered = sorted(
        enumerate(recipe.gear), key=lambda pair: (_role_rank(pair[1].role), pair[0])
    )

    created = rigs.create(
        name or recipe.title,
        signal_path=recipe.signal_path,
        notes=recipe.summary,
    )
    draft_id = created.draft.draft_id
    result = CompileResult(draft_id=draft_id, recipe=recipe)
    slot_of_role: dict[str, int] = {}

    for _, choice in ordered:
        if len(result.placed) >= MAX_SLOTS:
            result.unplaced.append(f"{choice.role} ({choice.name}) - rig is full")
            continue
        edit = rigs.add_block(
            draft_id,
            choice.category,
            choice.resolved_model_id or choice.name,
            params=dict(choice.settings),
            model_name=choice.name,
        )
        block = max(edit.rig.blocks, key=lambda b: b.slot)
        result.placed.append((block.slot, choice))
        slot_of_role[choice.role.lower()] = block.slot

    for scene in recipe.scenes:
        on, off, missing = [], [], []
        for role in scene.roles_on:
            slot = slot_of_role.get(role.lower())
            (on if slot else missing).append(slot or role)
        for role in scene.roles_off:
            slot = slot_of_role.get(role.lower())
            (off if slot else missing).append(slot or role)

        if missing:
            result.warnings.append(
                f"scene '{scene.name}' names role(s) with no gear: "
                + ", ".join(str(m) for m in missing)
            )
        rigs.configure_scene(
            draft_id,
            scene.name,
            on_slots=on,
            off_slots=off,
            footswitch=scene.footswitch,
        )

    if not recipe.tone_source:
        result.warnings.append(
            "no amp, capture, or clone in the recipe - the rig has no tone source"
        )
    for choice in recipe.unsupported:
        result.warnings.append(
            f"{choice.role} ({choice.name}) is unsourced - a guess, not a finding"
        )

    return result

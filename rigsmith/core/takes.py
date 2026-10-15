"""Your own playing, kept and labelled.

Every comparison in this tool is against a record made by somebody else on
somebody else's guitar. The one variable you can remove is the instrument:
play the part yourself, keep the recording, and render candidates through
that. Then a capture that scores well scores well *through your pickups*,
which is the only version of the question that has a useful answer.

A take is a file plus what you were holding when you made it. The metadata
is the point - "which of my guitars was this" is not a detail, it is the
difference between a library and a folder of wav files. Six months later,
`bridge-humbucker-drop-d.wav` is a guess and this is not.

Takes come from anywhere: a DI box, the Core's USB audio, a loop played into
the pedal and pulled off its storage, a phone in front of an amp. Where it
came from changes what it is good for, so that is recorded too.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings, require_writable
from .errors import Conflict, Invalid, NotFound

#: What a take can be used for. The distinction that matters is whether the
#: signal has been through an amp: a DI has not, so it can be rendered
#: through anything, and everything else can only be listened to or matched
#: against.
KINDS = {
    "di": "dry, straight from the guitar - the one you can reamp through anything",
    "wet": "already through an amp or the device - a reference to match, not a source",
    "loop": "recorded on the pedal's looper - usually wet, sometimes both",
    "reference": "someone else's recording you are chasing",
}

#: Where a take came from, which is mostly about how much to trust its level
#: and its top end.
ORIGINS = {
    "di-box": "a DI box or interface input",
    "usb": "the Core's USB audio, channel 5 dry or 1-2 wet",
    "looper": "played into the pedal and pulled off its storage",
    "file": "an existing file from somewhere else",
    "mic": "a microphone in front of something",
    "synth": "rendered from a learned profile - your guitar's notes, arranged by rigsmith",
    "loop-return": "played through the hardware loop and recorded on the way back",
}


def digest_of(path: Path) -> str | None:
    """A short content hash, or ``None`` if the file cannot be read.

    Bytes rather than decoded samples: two encodes of the same performance
    are different recordings and should both be keepable, while the same file
    imported twice is just the same file.
    """
    import hashlib

    try:
        hasher = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(block)
        return hasher.hexdigest()[:32]
    except OSError:
        return None


def find_duplicate(path: Path) -> "Take | None":
    """A take already holding this exact file, if there is one."""
    fingerprint = digest_of(path)
    if not fingerprint:
        return None
    return next(
        (take for take in _load_index().values() if take.digest == fingerprint), None
    )


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug or "take"


@dataclass
class Take:
    """One recording of yours, and the instrument that made it."""

    take_id: str
    path: str
    kind: str = "di"
    origin: str = "file"

    guitar: str | None = None
    """Which instrument. The reason this library exists."""
    pickup: str | None = None
    """bridge, neck, middle, both - the second-biggest variable after the guitar."""
    strings: str | None = None
    tuning: str | None = None
    capo: int | None = None

    note: str | None = None
    """What you played, in your own words. 'the Cherub Rock riff, twice.'"""
    tags: list[str] = field(default_factory=list)

    digest: str | None = None
    """Hash of the file's bytes. Only for spotting the same recording added
    twice - which happens constantly when loops are pulled off the pedal
    more than once."""

    duration: float = 0.0
    sample_rate: int = 0
    channels: int = 1
    lossy: bool = False
    concerns: list[str] = field(default_factory=list)

    lowest_note: str | None = None
    """The lowest note heard. Not proof of a tuning - a lower string may
    simply not have been played - but it is what the recording contains."""
    cents_offset: float | None = None
    """Deviation from concert pitch. A take recorded flat puts every harmonic
    slightly off where the record's sits, and a band comparison reads that as
    a tone difference."""

    added_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )

    @property
    def file(self) -> Path:
        return Path(self.path)

    @property
    def exists(self) -> bool:
        return self.file.is_file()

    @property
    def reamps(self) -> bool:
        """Can this be rendered through a candidate rig?

        Only a dry signal can. Rendering a wet take through an amp model
        stacks two amps, which sounds like neither and measures like nothing.
        """
        return self.kind == "di"

    def label_or_id(self) -> str:
        """The instrument if it is known, the take id otherwise."""
        bits = [p for p in (self.guitar, self.pickup) if p]
        return " ".join(bits) if bits else self.take_id

    @property
    def in_concert_pitch(self) -> bool | None:
        """``None`` when there was not enough pitched material to tell."""
        from ..match.pitch import CENTS_TOLERANCE

        if self.cents_offset is None:
            return None
        return abs(self.cents_offset) <= CENTS_TOLERANCE

    def describe(self) -> str:
        bits = [self.take_id, f"[{self.kind}]"]
        gear = " ".join(p for p in (self.guitar, self.pickup) if p)
        if gear:
            bits.append(gear)
        if self.tuning:
            bits.append(self.tuning)
        elif self.lowest_note:
            bits.append(f"low {self.lowest_note}")
        if self.in_concert_pitch is False:
            bits.append(f"{self.cents_offset:+.0f}c")
        if self.duration:
            bits.append(f"{self.duration:.1f}s")
        if self.lossy:
            bits.append("lossy")
        if not self.exists:
            bits.append("MISSING")
        return "  ".join(bits)

    def render(self) -> str:
        lines = [self.describe()]
        if self.note:
            lines.append(f"  {self.note}")
        lines.append(f"  {self.path}")
        lines.extend(f"  note: {c}" for c in self.concerns)
        return "\n".join(lines)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["exists"] = self.exists
        data["reamps"] = self.reamps
        data["in_concert_pitch"] = self.in_concert_pitch
        data["describe"] = self.describe()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Take":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------


def _dir() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    directory = settings.takes_dir
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _index_path() -> Path:
    return _dir() / "index.json"


def _load_index() -> dict[str, Take]:
    path = _index_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    takes = {}
    for entry in raw.get("takes", []):
        try:
            take = Take.from_dict(entry)
        except TypeError:
            continue
        takes[take.take_id] = take
    return takes


def _save_index(takes: dict[str, Take]) -> None:
    payload = {"takes": [take.to_dict() for take in takes.values()]}
    # to_dict adds derived keys; strip them so the file stays the record and
    # not a cache of things that can change.
    for entry in payload["takes"]:
        for derived in ("exists", "reamps", "in_concert_pitch", "describe"):
            entry.pop(derived, None)
    _index_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def list_takes(*, kind: str | None = None, guitar: str | None = None) -> list[Take]:
    """Every take, newest first, optionally filtered."""
    takes = sorted(_load_index().values(), key=lambda t: t.added_at, reverse=True)
    if kind:
        takes = [t for t in takes if t.kind == kind]
    if guitar:
        lowered = guitar.lower()
        takes = [t for t in takes if (t.guitar or "").lower() == lowered]
    return takes


def get(take_id: str) -> Take:
    takes = _load_index()
    if take_id not in takes:
        known = ", ".join(sorted(takes)) or "none yet"
        raise NotFound(f"no take '{take_id}'", hint=f"Known takes: {known}")
    return takes[take_id]


def guitars() -> list[str]:
    """Every instrument that appears in the library."""
    names = {t.guitar for t in _load_index().values() if t.guitar}
    return sorted(names)


# --------------------------------------------------------------------------
# adding
# --------------------------------------------------------------------------


def add(
    source_path: str | Path,
    *,
    take_id: str | None = None,
    kind: str = "di",
    origin: str = "file",
    guitar: str | None = None,
    pickup: str | None = None,
    strings: str | None = None,
    tuning: str | None = None,
    capo: int | None = None,
    note: str | None = None,
    tags: list[str] | None = None,
    copy: bool = True,
) -> Take:
    """Add a recording to the library, reading it to check it is usable.

    Args:
        copy: Bring the file into the workspace. Left on, because a library
            that points at files on a drive you unplug is not a library.
    """
    from ..match.pitch import analyse_file
    from ..match.source import inspect_audio

    require_writable("add a take")
    if kind not in KINDS:
        raise Invalid(
            f"unknown take kind '{kind}'",
            hint="Use one of: " + ", ".join(f"{k} ({v})" for k, v in KINDS.items()),
        )
    if origin not in ORIGINS:
        raise Invalid(
            f"unknown origin '{origin}'", hint="Use one of: " + ", ".join(ORIGINS)
        )

    source = Path(source_path)
    # Read it before filing it. A take that turns out to be unreadable is
    # worse than one that was never added, because it sits in the library
    # looking usable until the day you reach for it.
    described = inspect_audio(source)

    existing = find_duplicate(source)
    if existing is not None:
        raise Conflict(
            f"this recording is already in the library as '{existing.take_id}'",
            hint=(
                f"{existing.describe()}\n"
                "Same bytes, so nothing new to add. Rename or re-export it "
                "first if you meant to keep a second copy."
            ),
        )

    identifier = slugify(take_id or source.stem)
    takes = _load_index()
    if identifier in takes:
        raise Conflict(
            f"a take called '{identifier}' already exists",
            hint="Pass a different take_id, or delete the old one first.",
        )

    destination = source
    if copy:
        destination = _dir() / f"{identifier}{source.suffix.lower()}"
        if destination.resolve() != source.resolve():
            shutil.copy2(source, destination)

    # What the guitar was tuned to. Only for the user's own playing - the
    # tuning of somebody else's record is not something this library is
    # keeping, and measuring it would just be a wait.
    report = None
    if kind in ("di", "wet", "loop"):
        try:
            report = analyse_file(destination)
        except Exception:
            report = None

    take = Take(
        take_id=identifier,
        path=str(destination),
        kind=kind,
        origin=origin,
        guitar=guitar,
        pickup=pickup,
        strings=strings,
        tuning=tuning,
        capo=capo,
        note=note,
        tags=list(tags or []),
        duration=described.duration,
        sample_rate=described.sample_rate,
        channels=described.channels,
        lossy=described.lossy,
        digest=digest_of(destination),
        concerns=described.concerns(as_di=kind == "di"),
        lowest_note=report.lowest_note if report and report.measured else None,
        cents_offset=report.cents_offset if report and report.measured else None,
    )
    if report and report.measured:
        take.concerns.extend(report.concerns(stated_tuning=tuning))
    takes[identifier] = take
    _save_index(takes)
    return take


def update(take_id: str, **changes) -> Take:
    """Change a take's labels. The audio itself is never edited."""
    require_writable("edit a take")
    takes = _load_index()
    take = takes.get(take_id)
    if take is None:
        raise NotFound(f"no take '{take_id}'")

    editable = {
        "kind", "origin", "guitar", "pickup", "strings", "tuning", "capo",
        "note", "tags",
    }
    unknown = set(changes) - editable
    if unknown:
        raise Invalid(
            f"cannot change {', '.join(sorted(unknown))} on a take",
            hint="Editable: " + ", ".join(sorted(editable)),
        )
    if "kind" in changes and changes["kind"] not in KINDS:
        raise Invalid(f"unknown take kind '{changes['kind']}'")

    for name, value in changes.items():
        if value is not None:
            setattr(take, name, value)

    # Two of these labels change what is worth saying about the file. A take
    # added as a reference and relabelled a DI has just become something that
    # gets rendered through, which is where lossy encoding starts to matter;
    # a tuning added after the fact is a claim that can now be checked.
    if {"kind", "tuning"} & set(changes):
        _refresh_concerns(take)

    _save_index(takes)
    return take


def _refresh_concerns(take: Take) -> None:
    """Re-derive a take's notes after its labels changed."""
    from ..match.pitch import analyse_file
    from ..match.source import inspect_audio

    if not take.exists:
        return
    try:
        take.concerns = inspect_audio(take.file).concerns(as_di=take.kind == "di")
    except Exception:
        return

    if take.kind in ("di", "wet", "loop"):
        try:
            report = analyse_file(take.file)
        except Exception:
            return
        if report.measured:
            take.lowest_note = report.lowest_note
            take.cents_offset = report.cents_offset
            take.concerns.extend(report.concerns(stated_tuning=take.tuning))


def delete(take_id: str, *, remove_file: bool = False) -> str:
    """Forget a take. The audio stays unless you say otherwise."""
    require_writable("delete a take")
    takes = _load_index()
    take = takes.pop(take_id, None)
    if take is None:
        raise NotFound(f"no take '{take_id}'")
    _save_index(takes)

    if remove_file and take.file.is_file():
        # Only files this library brought in - never one it was pointed at.
        try:
            take.file.relative_to(_dir())
        except ValueError:
            return f"Forgot '{take_id}'. Its file was outside the library, so it stays."
        take.file.unlink()
        return f"Forgot '{take_id}' and deleted {take.file.name}."
    return f"Forgot '{take_id}'. The file stays at {take.path}."


# --------------------------------------------------------------------------
# using them
# --------------------------------------------------------------------------


def default_di(guitar: str | None = None) -> Take | None:
    """The take to reamp through, if the library has an obvious one.

    The most recent DI, optionally for one instrument. Being able to say
    "search these captures" without naming a file every time is most of what
    a library is for.
    """
    candidates = [t for t in list_takes(kind="di", guitar=guitar) if t.exists]
    return candidates[0] if candidates else None


def resolve(reference: str | Path) -> Path:
    """Accept a take id or a path, and return a path.

    Every surface that takes audio can then take either, which means a take
    id works anywhere a filename does.
    """
    text = str(reference)
    if not text:
        raise Invalid("no audio given")

    candidate = Path(text)
    if candidate.exists():
        return candidate

    index = _load_index()
    take = index.get(slugify(text))
    if take is None:
        raise NotFound(
            f"'{reference}' is neither a file nor a take",
            hint=_did_you_mean(text, index) or
            "List what the library holds with:  rigsmith take list",
        )
    if not take.exists:
        raise NotFound(
            f"take '{take.take_id}' points at {take.path}, which is gone",
            hint="Add it again, or delete the entry.",
        )
    return take.file


def _did_you_mean(text: str, index: dict[str, Take]) -> str | None:
    """Closest take ids, because a typo is likelier than a missing file."""
    import difflib

    if not index:
        return None
    close = difflib.get_close_matches(slugify(text), list(index), n=3, cutoff=0.5)
    if close:
        return "Did you mean:  " + ", ".join(close)
    return "The library holds:  " + ", ".join(sorted(index))


def record(
    duration_s: float = 15.0,
    *,
    take_id: str | None = None,
    guitar: str | None = None,
    pickup: str | None = None,
    tuning: str | None = None,
    note: str | None = None,
) -> tuple["Take | None", "Take | None", str]:
    """Record one performance over USB and file both halves of it.

    The Core sends the wet tone and the dry DI at once, on different
    channels, and they are two different things: the DI can be rendered
    through any candidate rig, the wet take is the reference to match against.
    Recording them as one action and filing them as two is the shape the
    hardware already has - asking somebody to record twice would be asking
    them to play it differently twice.

    Returns ``(di, wet, message)``. The DI is ``None`` when the device did not
    expose channel 5, which is a USB audio mode setting rather than a fault.
    """
    from . import matching

    require_writable("record a take")
    capture, paths = matching.capture_di(duration_s, name=take_id or "take")

    stem = slugify(take_id or "take")
    labels = {
        "guitar": guitar, "pickup": pickup, "tuning": tuning, "note": note,
    }

    wet = None
    if paths.get("wet"):
        wet = add(
            paths["wet"],
            take_id=f"{stem}-wet",
            kind="wet",
            origin="usb",
            **labels,
        )

    di = None
    if paths.get("dry"):
        di = add(
            paths["dry"],
            take_id=f"{stem}-di",
            kind="di",
            origin="usb",
            **labels,
        )

    lines = [capture.describe()]
    lines.extend(filter(None, [di.render() if di else None, wet.render() if wet else None]))
    if di is None:
        lines.append(
            "\nNo dry DI was captured, so this take cannot be rendered through "
            "other rigs - only listened to. The Core has to expose channel 5, "
            "which is a USB audio mode setting on the device."
        )
    return di, wet, "\n".join(lines)


def render_list(takes: list[Take]) -> str:
    if not takes:
        return (
            "No takes yet.\n\n"
            "This is where your own playing goes. Record the part once - a DI "
            "box, the Core's USB channel 5, or a loop played into the pedal - "
            "and every candidate afterwards gets rendered through your guitar "
            "rather than through a stranger's."
        )
    lines = [f"{len(takes)} take(s):"]
    lines.extend(f"  {take.describe()}" for take in takes)

    instruments = sorted({t.guitar for t in takes if t.guitar})
    if instruments:
        lines.append("\nguitars: " + ", ".join(instruments))
    unlabelled = [t.take_id for t in takes if not t.guitar]
    if unlabelled:
        lines.append(
            f"\n{len(unlabelled)} take(s) have no guitar recorded: "
            + ", ".join(unlabelled)
            + "\nWhich instrument it was is the whole point of keeping these."
        )
    return "\n".join(lines)

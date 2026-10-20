"""Songs being chased: the record, its stems, and the map of its parts.

A song lives in its own folder in the workspace - the mix you own, the guitar
stem separated from it, each guitar layer pulled out of the stem, and
``song.json`` with everything known about it. The useful unit is the
**part**: one section crossed with one layer, "the double-tracked verse
rhythm" or "the centred solo". Parts are what get picked, researched,
rendered on your guitar and matched, each on its own - because a song is not
one tone, and averaging it into one is how a capture that matches nothing
comes out on top.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..config import get_settings, require_writable
from .errors import Conflict, Invalid, NotFound
from .jobs import Progress


def _root() -> Path:
    settings = get_settings()
    settings.ensure_workspace()
    return settings.songs_dir


def slugify(text: str) -> str:
    from .takes import slugify as _slug

    return _slug(text)


@dataclass
class Part:
    part_id: str
    section: int
    """Index into the song's sections."""
    section_name: str
    layer: str
    """left, right, centre, wide, or stem (all of the guitar)."""
    start: float
    end: float
    presence: float = 1.0
    """How much of this layer is playing here, 0-1 of its loudest section."""
    sketch: dict = field(default_factory=dict)
    chosen: bool = False
    note: str | None = None
    recipe_id: str | None = None
    di_take: str | None = None
    """A DI of this part on your guitar, once rendered."""
    best: dict | None = None
    """The best candidate so far: label, composite, mode, when."""
    history: list[dict] = field(default_factory=list)
    reading: str = "auto"
    """How its stem is read into notes - auto, mono, poly or arpeggio. Set
    when the user corrects the automatic choice, and kept for every later
    render of the part, autopilot's included."""
    heard_as: str | None = None
    """How the notes of its rendered DI were found: monophonic, polyphonic
    or arpeggio when transcribed, else midi, text or basic-pitch - so a
    wrong automatic reading shows on the part, next to the fix."""

    @property
    def region(self) -> str:
        return f"{self.start:.2f}:{self.end:.2f}"

    @property
    def label(self) -> str:
        layer = {"stem": "all guitars", "centre": "centre", "left": "left", "right": "right", "wide": "wide"}.get(self.layer, self.layer)
        return f"{self.section_name} - {layer}"

    def describe(self) -> str:
        mark = "*" if self.chosen else " "
        summary = self.sketch.get("summary", "")
        minutes = f"{int(self.start // 60)}:{self.start % 60:04.1f}"
        line = f"{mark} {self.part_id:<22} {minutes} {self.end - self.start:5.1f}s  {summary}"
        if self.best:
            line += f"  [best: {self.best.get('label')} {self.best.get('composite', 0):.3f}]"
        return line

    def to_dict(self) -> dict:
        data = asdict(self)
        data["region"] = self.region
        data["label"] = self.label
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Part":
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})


#: Bumped when the analysis learns something a stored map does not have, so
#: a song mapped before can say so. 2: chorus and vibrato from pitch, tremolo
#: only inside notes, gain read by how monophonic the part is. 3: deep
#: tremolo heard at all, and every tremolo's depth. 4: the room measured
#: where playing stops, so reverb is read rather than guessed. 5: the same
#: music twice in a row is one section, and a last chorus is not an outro.
ANALYSIS_VERSION = 5


@dataclass
class Song:
    song_id: str
    title: str
    artist: str | None = None
    album: str | None = None
    year: int | None = None
    mix: str | None = None
    stem: str | None = None
    layer_files: dict[str, str] = field(default_factory=dict)
    structure: dict | None = None
    layers: dict | None = None
    parts: list[Part] = field(default_factory=list)
    lookup: dict | None = None
    """What the online lookup found - artwork, preview, store link."""
    credits: dict | None = None
    recipe_id: str | None = None
    notes: list[str] = field(default_factory=list)
    analysis_version: int = 0
    """:data:`ANALYSIS_VERSION` when the map was made; 0 if never, or before versions."""
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    updated_at: str = ""

    @property
    def directory(self) -> Path:
        return _root() / self.song_id

    @property
    def target(self) -> str | None:
        """The best audio to measure against: the stem, else the mix."""
        return self.stem or self.mix

    def covering(self, layer: str, start: float, end: float, *, at_least: float = 0.5) -> Part | None:
        """The part on ``layer`` that covers most of ``start``-``end`` (at least half), if any."""
        span = max(end - start, 1e-9)
        best, cover = None, 0.0
        for part in self.parts:
            if part.layer == layer:
                overlap = (min(part.end, end) - max(part.start, start)) / span
                if overlap > cover:
                    best, cover = part, overlap
        return best if cover >= at_least else None

    def part(self, part_id: str) -> Part:
        for part in self.parts:
            if part.part_id == part_id:
                return part
        raise NotFound(
            f"no part '{part_id}' in '{self.song_id}'",
            hint="Parts: " + ", ".join(p.part_id for p in self.parts) if self.parts else "Analyse the song first.",
        )

    def chosen(self) -> list[Part]:
        return [p for p in self.parts if p.chosen]

    def describe(self) -> str:
        head = self.title + (f" - {self.artist}" if self.artist else "")
        if self.year:
            head += f" ({self.year})"
        lines = [f"{self.song_id}: {head}"]
        lines.append(f"  mix:  {self.mix or '(none)'}")
        lines.append(f"  stem: {self.stem or '(none - isolate it, or add one)'}")
        if self.structure:
            lines.append(
                f"  {self.structure.get('tempo', 0):.0f} BPM"
                + (" (set)" if self.structure.get("tempo_source") == "hand" else "")
                + f", {self.structure.get('key')}, "
                f"{len(self.structure.get('sections', []))} sections"
            )
        if self.layers:
            lines.append(f"  layers: {self.layers.get('summary')}")
        if self.parts:
            lines.append(f"  {len(self.parts)} part(s), {len(self.chosen())} chosen:")
            lines.extend("  " + p.describe() for p in self.parts)
        lines.extend(f"  note: {n}" for n in self.notes)
        if self.stale:
            lines.append(f"  note: mapped by an older analyser - run 'rigsmith song analyse {self.song_id}' again for what the newer one hears - the room's reverb, deep tremolo and its depth, chorus, vibrato, a fairer gain reading, and sections that do not cut a chorus in two")
        return "\n".join(lines)

    @property
    def stale(self) -> bool:
        """Mapped by an older analyser - re-mapping would read it better."""
        return bool(self.structure) and self.analysis_version < ANALYSIS_VERSION

    def to_dict(self) -> dict:
        data = asdict(self)
        data["parts"] = [p.to_dict() for p in self.parts]
        data["describe"] = self.describe()
        data["stale"] = self.stale
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Song":
        known = set(cls.__dataclass_fields__)
        values = {k: v for k, v in data.items() if k in known and k != "parts"}
        song = cls(**values)
        song.parts = [Part.from_dict(p) for p in data.get("parts", [])]
        return song

    def save(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        path = self.directory / "song.json"
        data = self.to_dict()
        data.pop("describe", None)
        from . import storage

        return storage.write_json(path, data)


# --------------------------------------------------------------------------
# keeping them
# --------------------------------------------------------------------------


def list_songs() -> list[Song]:
    found = []
    for directory in sorted(p for p in _root().iterdir() if p.is_dir()):
        path = directory / "song.json"
        if path.is_file():
            try:
                found.append(Song.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, ValueError, KeyError, TypeError):
                continue
    found.sort(key=lambda s: s.updated_at or s.created_at, reverse=True)
    return found


def get(song_id: str) -> Song:
    path = _root() / slugify(song_id) / "song.json"
    if not path.is_file():
        known = ", ".join(s.song_id for s in list_songs()) or "none yet"
        raise NotFound(f"no song '{song_id}'", hint=f"Songs: {known}")
    return Song.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _bring_in(song: Song, source, name: str, *, copy: bool) -> str:
    from .takes import resolve

    path = resolve(source)
    if not copy:
        return str(path.resolve())
    destination = song.directory / f"{name}{path.suffix.lower()}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() != destination.resolve():
        shutil.copy2(path, destination)
    return str(destination)


def create(
    title: str,
    *,
    artist: str | None = None,
    album: str | None = None,
    year: int | None = None,
    mix=None,
    stem=None,
    copy: bool = True,
    song_id: str | None = None,
) -> Song:
    """Start a song: a title, and whatever audio of it you have."""
    require_writable("create a song")
    identifier = slugify(song_id or (f"{title} {artist}" if artist else title))
    if (_root() / identifier / "song.json").is_file():
        raise Conflict(f"a song called '{identifier}' already exists", hint="Open it, or give another id.")
    song = Song(song_id=identifier, title=title, artist=artist, album=album, year=year)
    song.directory.mkdir(parents=True, exist_ok=True)
    if mix is not None:
        song.mix = _bring_in(song, mix, "mix", copy=copy)
    if stem is not None:
        song.stem = _bring_in(song, stem, "stem", copy=copy)
    song.save()
    return song


def attach(song_id: str, *, mix=None, stem=None, copy: bool = True) -> Song:
    require_writable("add audio to a song")
    song = get(song_id)
    if mix is not None:
        song.mix = _bring_in(song, mix, "mix", copy=copy)
    if stem is not None:
        song.stem = _bring_in(song, stem, "stem", copy=copy)
        song.layers = None
        song.layer_files = {}
    song.save()
    return song


def update(song_id: str, **changes) -> Song:
    require_writable("edit a song")
    song = get(song_id)
    editable = {"title", "artist", "album", "year", "recipe_id"}
    unknown = set(changes) - editable
    if unknown:
        raise Invalid(f"cannot change {', '.join(sorted(unknown))} on a song")
    for key, value in changes.items():
        if value is not None:
            setattr(song, key, value)
    song.save()
    return song


def delete(song_id: str) -> str:
    require_writable("delete a song")
    song = get(song_id)
    shutil.rmtree(song.directory, ignore_errors=True)
    return f"Deleted '{song.song_id}' and everything in its folder."


# --------------------------------------------------------------------------
# finding and isolating
# --------------------------------------------------------------------------


def lookup(query: str, *, limit: int = 8):
    from ..song.acquire import search

    return search(query, limit=limit)


def enrich(song_id: str, *, with_credits: bool = True) -> Song:
    """Fill in metadata, artwork and credits from the online lookup."""
    from ..song.acquire import credits, search

    require_writable("update a song")
    song = get(song_id)
    query = f"{song.title} {song.artist or ''}".strip()
    matches = search(query, limit=5)
    if matches:
        best = matches[0]
        song.lookup = best.to_dict()
        song.album = song.album or best.album
        song.year = song.year or best.year
        song.artist = song.artist or best.artist
    if with_credits and song.artist:
        try:
            song.credits = credits(song.title, song.artist)
        except Exception as exc:  # credits are a bonus; a failure is a note
            song.notes.append(f"credits lookup failed: {exc}")
    song.save()
    return song


def preview(song_id: str) -> Song:
    """Fetch the store preview as the song's mix, when there is nothing else."""
    from ..song.acquire import SongMatch, fetch_preview

    require_writable("fetch a preview")
    song = get(song_id)
    if not song.lookup:
        song = enrich(song_id, with_credits=False)
    if not song.lookup or not song.lookup.get("preview_url"):
        raise NotFound("no preview available for this song")
    match = SongMatch(**{k: v for k, v in song.lookup.items() if k in SongMatch.__dataclass_fields__})
    path = fetch_preview(match, song.directory / "preview.m4a")
    song.notes.append(
        "the mix is a 30-second store preview (provided courtesy of iTunes) - "
        "fine to explore with, but use your own copy to match a whole part"
    )
    song.mix = path
    song.save()
    return song


def isolate(song_id: str, *, progress: Progress | None = None) -> Song:
    """Separate the guitar stem from the song's mix."""
    from . import matching

    require_writable("isolate a guitar stem")
    song = get(song_id)
    if not song.mix:
        raise Invalid("this song has no mix to separate", hint="Add one with:  rigsmith song add-audio <song> --mix <file>")
    result = matching.isolate_guitar(song.mix, out_dir=song.directory / "stems", progress=progress)
    guitar = next((p for name, p in result.stems.items() if name == "guitar"), None)
    if guitar is None:
        raise Invalid("the separation produced no guitar stem", hint=f"Stems: {', '.join(result.stems)}")
    song = get(song_id)
    song.stem = str(guitar)
    song.layers = None
    song.layer_files = {}
    song.save()
    return song


# --------------------------------------------------------------------------
# mapping
# --------------------------------------------------------------------------


def analyse(song_id: str, *, progress: Progress | None = None, min_presence: float = 0.25) -> Song:
    """Map a song: sections, guitar layers, and a tone sketch for every part."""
    from ..match.render_offline import load_audio
    from ..song import describe, layers, structure

    require_writable("analyse a song")
    song = get(song_id)
    if not song.mix and not song.stem:
        raise Invalid("this song has no audio yet", hint="Add a mix or a stem first.")

    def step(fraction, message):
        if progress:
            progress.check()
            progress.update(fraction, message)

    step(0.05, "reading the audio")
    # Structure from the mix when there is one - drums and vocals mark the
    # sections far better than the guitar alone.
    source = song.mix or song.stem
    mix_audio, rate = load_audio(source, mono=False)
    step(0.15, "finding the sections")
    # A tempo somebody corrected outlives the beat tracker's next guess.
    kept = (song.structure or {}).get("tempo") if (song.structure or {}).get("tempo_source") == "hand" else None
    named = [s for s in (song.structure or {}).get("sections", []) if s.get("name_source") == "hand"]
    shape = structure.analyse(mix_audio, rate)
    if kept:
        shape.tempo = float(kept)
    song.structure = shape.to_dict()
    if kept:
        song.structure["tempo_source"] = "hand"
    # A section somebody named outlives it too, where the new map has one in
    # the same place. Its parts keep ids from the map's own name, as a
    # renamed section's always had, so what was chosen is still found.
    hand_names: dict[int, str] = {}
    for index, section in enumerate(shape.sections):
        match = next((n for n in named if abs(n["start"] - section.start) < 2.0 and abs(n["end"] - section.end) < 2.0), None)
        if match:
            hand_names[index] = match["name"]
            song.structure["sections"][index].update(name=match["name"], name_source="hand")
    song.analysis_version = ANALYSIS_VERSION

    stem_audio = mix_audio
    if song.stem and song.stem != source:
        step(0.35, "reading the guitar stem")
        stem_audio, stem_rate = load_audio(song.stem, mono=False)
        if stem_rate != rate:
            from ..match.render_offline import resample

            stem_audio = resample(stem_audio, stem_rate, rate)
    elif not song.stem:
        song.notes = [n for n in song.notes if "no guitar stem" not in n]
        song.notes.append(
            "no guitar stem - parts are described from the full mix, which includes "
            "everything else in it. Isolate the guitar for a cleaner map."
        )

    step(0.45, "finding the guitar layers")
    layer_map = layers.analyse(stem_audio, rate, sections=shape.sections, extract_to=song.directory / "layers")
    song.layers = layer_map.to_dict()
    song.layer_files = {l.layer_id: l.path for l in layer_map.layers if l.path}

    step(0.6, "describing every part")

    sources = {"stem": stem_audio}
    for layer_id, path in song.layer_files.items():
        sources[layer_id], _ = load_audio(path, mono=False)

    previous = {p.part_id: p for p in song.parts}
    parts: list[Part] = []
    echoes: dict[int, dict] = {}
    total = len(shape.sections) * len(sources)
    done = 0
    for index, section in enumerate(shape.sections):
        name = section.name or section.label

        def present(layer_id: str) -> float:
            layer = next((l for l in layer_map.layers if l.layer_id == layer_id), None)
            return (layer.sections or {}).get(f"{section.start:.2f}", 0.0) if layer else 0.0

        # Where both sides carry a part, the centre is the two takes' common
        # ground summed - which beats like a chorus without being one.
        doubled = present("left") >= min_presence and present("right") >= min_presence
        for layer_id, audio in sources.items():
            done += 1
            presence = 1.0
            if layer_id != "stem":
                layer = next((l for l in layer_map.layers if l.layer_id == layer_id), None)
                presence = (layer.sections or {}).get(f"{section.start:.2f}", 0.0) if layer else 0.0
                if presence < min_presence:
                    continue
            a, b = int(section.start * rate), int(section.end * rate)
            piece = audio[a:b]
            if len(piece) < rate:
                continue
            sketch = describe.sketch(
                piece, rate, tempo=shape.tempo, from_stem=bool(song.stem),
                pitch=not (layer_id == "centre" and doubled),
            )
            # A layer split out of the stem keeps its notes but not its
            # echo; the stem has it, and where it sits says whose it is.
            if layer_id in ("centre", "left", "right") and not sketch.delay_ms:
                if index not in echoes:
                    echoes[index] = describe.echoes_by_place(stem_audio[a:b], rate, shape.tempo)
                echo = describe.layer_echo(echoes[index], layer_id)
                if echo:
                    sketch.delay_ms, sketch.delay_strength, sketch.delay_note = echo
                    sketch.suggestions = describe._suggestions(sketch)
            part_id = f"{slugify(name)}-{index + 1}-{layer_id}"
            part = Part(
                part_id=part_id,
                section=index,
                section_name=hand_names.get(index, name),
                layer=layer_id,
                start=section.start,
                end=section.end,
                presence=round(float(presence), 3),
                sketch=sketch.to_dict(),
            )
            # Keep what the user decided about a part across re-analysis.
            if part_id in previous:
                old = previous[part_id]
                part.chosen, part.note, part.recipe_id = old.chosen, old.note, old.recipe_id
                part.di_take, part.best, part.history = old.di_take, old.best, old.history
                part.reading, part.heard_as = old.reading, old.heard_as
            parts.append(part)
            step(0.6 + 0.35 * done / max(1, total), f"described {part.label}")
    carry_over(previous, parts)
    song.parts = parts
    song.save()
    step(1.0, f"{len(parts)} part(s) mapped")
    return song


def carry_over(previous: dict[str, Part], parts: list[Part]) -> None:
    """What was decided about parts a re-map moved or renamed.

    Parts keep their choices by id (in :func:`analyse`). One whose id is
    gone - its section merged with a neighbour, or named differently - hands
    its choice, note and recipe to the new part on the same layer that
    covers most of it. Its DI take and best result only follow when the new
    part spans the same stretch, since both were made for that stretch.
    """
    kept = {p.part_id for p in parts}
    mapped = Song(song_id="", title="", parts=parts)
    for old in previous.values():
        if old.part_id in kept or not (old.chosen or old.note or old.recipe_id or old.di_take or old.best or old.history or old.reading != "auto"):
            continue
        heir = mapped.covering(old.layer, old.start, old.end)
        if heir is None:
            continue
        # Two old parts can land on one new one (their sections merged):
        # keep both notes and histories, not only the first.
        heir.chosen = heir.chosen or old.chosen
        if old.note and old.note != heir.note:
            heir.note = f"{heir.note}; {old.note}" if heir.note else old.note
        if old.recipe_id and heir.recipe_id and old.recipe_id != heir.recipe_id:
            heir.note = f"{heir.note + '; ' if heir.note else ''}also researched as {old.recipe_id}"
        heir.recipe_id = heir.recipe_id or old.recipe_id
        if heir.reading == "auto":
            heir.reading = old.reading
        heir.history = (heir.history or []) + [h for h in (old.history or []) if h not in (heir.history or [])]
        if abs(heir.start - old.start) < 0.5 and abs(heir.end - old.end) < 0.5:
            if not heir.di_take:
                heir.di_take, heir.heard_as = old.di_take, old.heard_as
            heir.best = heir.best or old.best


def prominence(part: Part) -> float:
    """How much a part is worth chasing: loud, in a chorus, on a layer of its own."""
    value = part.presence
    if part.section_name == "chorus":
        value += 0.5
    elif part.section_name in ("verse", "solo"):
        value += 0.3
    if part.layer == "stem":
        value -= 0.2
    return value


#: Two parts are one sound when they are on one layer (either side of a
#: double-track counts as one) and their measured gains are within this.
SAME_SOUND_GAIN = 0.25


def sounds(song: Song, *, limit: int = 5) -> list[Part]:
    """One part for each sound the song's guitar has, the most prominent first.

    Songs change sound by section - a clean intro, a driven verse, a lead
    solo - so the most prominent part of each kind of section is taken, and
    two that are one sound are kept once. Measured gain alone would split one
    guitar: the demo song's rhythm read 0.56 to 0.83 across its sections.
    ``limit`` is a rig's footswitches.
    """
    playable = [p for p in song.parts if p.end - p.start >= 4] or list(song.parts)
    by_section: dict[str, list[Part]] = {}
    for part in playable:
        by_section.setdefault(part.section_name, []).append(part)
    picks = sorted((max(parts, key=prominence) for parts in by_section.values()), key=prominence, reverse=True)

    def layer(part: Part) -> str:
        return "sides" if part.layer in ("left", "right") else part.layer

    def gain(part: Part) -> float:
        return float((part.sketch or {}).get("gain") or 0.0)

    kept: list[Part] = []
    for part in picks:
        if not any(layer(k) == layer(part) and abs(gain(k) - gain(part)) < SAME_SOUND_GAIN for k in kept):
            kept.append(part)
    return kept[:limit]


def choose_sounds(song_id: str) -> Song:
    """Choose a part for each sound the song has (:func:`sounds`), and only those."""
    song = get(song_id)
    picked = sounds(song)
    if not picked:
        raise Invalid("the song has no parts to choose from yet", hint="Map it first.")
    return choose(song_id, [p.part_id for p in picked], only=True)


def choose(song_id: str, part_ids: list[str], *, chosen: bool = True, only: bool = False) -> Song:
    """Pick the parts to chase. ``only`` clears every other choice."""
    require_writable("choose parts")
    song = get(song_id)
    wanted = set(part_ids)
    for part in song.parts:
        if part.part_id in wanted:
            part.chosen = chosen
        elif only:
            part.chosen = False
    missing = wanted - {p.part_id for p in song.parts}
    if missing:
        raise NotFound(f"no part(s) {', '.join(sorted(missing))}")
    song.save()
    return song


def rename_section(song_id: str, index: int, name: str) -> Song:
    """Correct a section's guessed name - it carries through to its parts."""
    require_writable("rename a section")
    song = get(song_id)
    sections = (song.structure or {}).get("sections", [])
    if not 0 <= index < len(sections):
        raise Invalid(f"no section {index}", hint=f"Sections are numbered 0-{len(sections) - 1}.")
    sections[index]["name"] = name
    sections[index]["name_source"] = "hand"
    for part in song.parts:
        if part.section == index:
            part.section_name = name
    song.save()
    return song


#: Tempos a song can be set to, in BPM.
TEMPO_LIMITS = (40.0, 300.0)


def set_tempo(song_id: str, bpm: float | None = None, *, factor: float | None = None) -> Song:
    """Correct the song's tempo: an exact BPM, or the detected one halved or doubled.

    The beat tracker's known failure is an octave - 70 for 140. The tempo
    decides which note value each part's delay is and the tempo a draft
    rig is saved with, so a wrong one is worth a click to fix. Each part's
    sketch is re-timed (no re-analysis - the measurements do not depend on
    it), and a tempo set here survives re-mapping.
    """
    from ..song import describe

    require_writable("set a song's tempo")
    song = get(song_id)
    structure = dict(song.structure or {})
    current = float(structure.get("tempo") or 0)
    if bpm:
        tempo = float(bpm)
    elif factor:
        if not current:
            raise Invalid("there is no tempo to halve or double yet", hint="Map the song first, or give the BPM.")
        tempo = current * float(factor)
    else:
        raise Invalid("give a BPM, or a factor to halve (0.5) or double (2) the detected one")
    low, high = TEMPO_LIMITS
    if not low <= tempo <= high:
        raise Invalid(f"{tempo:.1f} BPM is outside {low:g}-{high:g}", hint="Halve or double it instead.")
    structure["tempo"] = round(tempo, 2)
    structure["tempo_source"] = "hand"
    song.structure = structure
    for part in song.parts:
        if part.sketch:
            part.sketch = describe.retime(part.sketch, tempo)
    song.save()
    return song


def part_audio(song_id: str, part_id: str, *, pad: float = 0.0) -> Path:
    """The part's own audio, cut out - the target to measure candidates against."""
    from ..match.render_offline import load_audio, save_audio

    song = get(song_id)
    part = song.part(part_id)
    source = song.layer_files.get(part.layer) if part.layer != "stem" else song.target
    source = source or song.target
    if not source:
        raise Invalid("this song has no audio")
    audio, rate = load_audio(source, mono=False)
    a = max(0, int((part.start - pad) * rate))
    b = int((part.end + pad) * rate)
    path = song.directory / "parts" / f"{part.part_id}.wav"
    save_audio(path, audio[a:b], rate)
    return path


def record_result(song_id: str, part_id: str, *, label: str, composite: float, mode: str, path: str | None = None) -> Part:
    """Remember how a candidate did against a part; keep the best."""
    require_writable("record a result")
    song = get(song_id)
    part = song.part(part_id)
    entry = {
        "label": label,
        "composite": float(composite),
        "mode": mode,
        "path": path,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    part.history.append(entry)
    same_mode = [h for h in part.history if h["mode"] == mode]
    part.best = min(same_mode, key=lambda h: h["composite"])
    song.save()
    return part


#: Audio a music library holds, in the order worth preferring for analysis.
LIBRARY_SUFFIXES = (".flac", ".wav", ".aiff", ".aif", ".m4a", ".mp3", ".ogg", ".opus", ".aac")

#: How many files a library scan looks at before giving up - a library on a
#: network drive should not hang the page.
LIBRARY_SCAN_LIMIT = 50_000


def _words(text: str) -> list[str]:
    import re

    text = re.sub(r"\(.*?\)|\[.*?\]", " ", (text or "").lower())  # (Remastered 2011), [Live]
    return [w for w in re.findall(r"[a-z0-9']+", text) if w not in ("the", "a", "an", "feat", "ft")]


def music_dirs() -> list[Path]:
    from . import appsettings

    configured = [Path(p).expanduser() for p in appsettings.load().music_dirs if p]
    roots = configured or [Path.home() / "Music"]
    return [root for root in roots if root.is_dir()]


def find_local(title: str, artist: str | None = None, *, roots: list[Path] | None = None, limit: int = 5) -> list[dict]:
    """Files in your music library that look like this song.

    Matched on names - a library is laid out Artist/Album/NN Title.ext, and
    that is what is read here, file and folders - so nothing has to be
    indexed and nothing leaves the machine. Every word of the title must be
    in the file's name; the artist's words in the path make it a better match;
    lossless beats lossy when both are there.
    """
    title_words = _words(title)
    if not title_words:
        return []
    artist_words = _words(artist or "")
    found = []
    seen = 0
    for root in roots if roots is not None else music_dirs():
        for path in root.rglob("*"):
            seen += 1
            if seen > LIBRARY_SCAN_LIMIT:
                break
            if path.suffix.lower() not in LIBRARY_SUFFIXES or not path.is_file():
                continue
            name = _words(path.stem)
            if not all(word in name for word in title_words):
                continue
            around = set(_words(" ".join(path.parts[-4:-1])) + name)
            artist_hits = sum(1 for word in artist_words if word in around)
            extra = max(0, len([w for w in name if not w.isdigit()]) - len(title_words) - artist_hits)
            score = 1.0 + (artist_hits / len(artist_words) if artist_words else 0.0) - 0.05 * extra
            score -= 0.01 * LIBRARY_SUFFIXES.index(path.suffix.lower())
            found.append({"path": str(path), "name": path.name, "score": round(score, 3), "artist_match": bool(artist_words) and artist_hits == len(artist_words)})
    found.sort(key=lambda f: -f["score"])
    return found[:limit]


#: How a part's stem can be read into notes (see :func:`..learn.transcribe.transcribe`).
READINGS = ("auto", "mono", "poly", "arpeggio")


def render_part_di(
    song_id: str, part_id: str, profile_id: str | None, *, midi=None, track=None, text=None, power_chords=False,
    like: str | None = None, amount: float = 1.0, reading: str | None = None, progress: Progress | None = None,
):
    """A DI of one part on your guitar, aligned to the part's own clock -
    moved toward the record's guitar with ``like`` when it was not yours.

    ``reading`` overrides how the stem is read into notes, and is kept on
    the part for its later renders; left out, the part's own is used.
    """
    from . import parts as part_rendering

    if reading is not None and reading not in READINGS:
        raise Invalid(f"no reading called '{reading}'", hint="One of: " + ", ".join(READINGS))
    require_writable("render a part")
    song = get(song_id)
    part = song.part(part_id)
    source = song.layer_files.get(part.layer) if part.layer != "stem" else song.target
    lowest = researched_lowest(song, part)
    result = part_rendering.part_di(
        profile_id,
        stem=source or song.target,
        midi=midi,
        track=track,
        text=text,
        region=part.region,
        mode=reading or part.reading,
        lowest=lowest,
        power_chords=power_chords,
        label=f"{song.song_id}-{part.part_id}",
        file_as_take=True,
        like=like,
        amount=amount,
        progress=progress,
    )
    song = get(song_id)
    song.part(part_id).di_take = result.take_id
    song.part(part_id).heard_as = _heard_as(result)
    if reading is not None:
        song.part(part_id).reading = reading
    song.save()
    return result


def part_notes(song_id: str, part_id: str) -> dict:
    """The notes the part's rendered DI plays, as saved beside it - times
    from the part's start, so they sit over its audio."""
    from . import takes

    song = get(song_id)
    part = song.part(part_id)
    if not part.di_take:
        raise NotFound(f"{part.label} has no DI yet", hint="Render one with 'on my guitar' on the part, or: rigsmith song di")
    try:
        take = takes.get(part.di_take)
    except NotFound:
        raise NotFound(f"{part.label}'s DI is gone from the takes library", hint="Render it again.") from None
    from .parts import notes_beside

    notes = notes_beside(take.path)
    if notes is None:
        raise NotFound(f"no notes were kept with {part.label}'s DI", hint="A DI you played yourself has no note list - only a rendered one does.")
    return {"part_id": part.part_id, "label": part.label, "duration": round(part.end - part.start, 3), "heard_as": part.heard_as, **notes}


def researched_lowest(song: Song, part: Part) -> str:
    """The record's lowest string, as a note, when research named its tuning.

    Inferred from the stem, the floor misses a drop tuning's low string when
    the part spends most of its time above it - a drop-C part of chords and
    chugs read as E standard, and its low C came back an octave up. A tuning
    the recipe states is better evidence than the audio's percentile.
    ``auto`` when there is none, or the tuning is not one rigsmith reads.
    """
    from ..learn.instrument import midi_to_note, tuning
    from . import recipes

    recipe_id = part.recipe_id or song.recipe_id
    if not recipe_id:
        return "auto"
    try:
        named = recipes.get(recipe_id).tuning
        return midi_to_note(tuning(named).lowest) if named else "auto"
    except Exception:  # a recipe gone, or a tuning written some other way
        return "auto"


def _heard_as(result) -> str | None:
    """``monophonic``, ``polyphonic`` or ``arpeggio`` for notes the built-in
    transcriber found; the notes' source otherwise."""
    source = getattr(result, "source", None)
    if source != "transcribed":
        return source
    method, *rest = (getattr(result, "method", None) or "").split(", ")
    return rest[0] if method == "builtin" and rest else method or None


def take_for_part(song_id: str, part_id: str, take: str) -> dict:
    """Your recording of a part, put on the part's clock and filed as its DI.

    The take is aligned to the part (an offset and a tempo ratio - see
    :func:`rigsmith.learn.align.align_take`); when it fits, it is stretched
    onto the part's timeline, filed in the takes library, and becomes the
    part's DI, so tuning can compare it frame by frame. When it does not fit,
    nothing is changed and the reason comes back.
    """
    from ..learn.align import align_take, apply_take
    from ..match.render_offline import load_audio, resample, save_audio
    from . import takes as take_library

    require_writable("align a take")
    song = get(song_id)
    part = song.part(part_id)
    source = take_library.resolve(take)
    audio, take_rate = load_audio(source)
    target, rate = load_audio(part_audio(song_id, part_id))
    if take_rate != rate:
        audio = resample(audio, take_rate, rate)
    alignment = align_take(audio, target, rate)
    if not alignment.usable:
        return {"aligned": False, "describe": alignment.describe(), "take_id": None, "path": None}
    placed = apply_take(audio, rate, alignment, len(target), record=target)
    path = save_audio(song.directory / "takes" / f"{part_id}-take.wav", placed, rate)
    take_id = slugify(f"{song_id}-{part_id}-take")
    if take_id in {t.take_id for t in take_library.list_takes()}:
        take_library.delete(take_id)
    filed = take_library.add(path, take_id=take_id, kind="di", origin="di-box", note=f"{part.label}: your take on the record's clock ({alignment.describe()})", tags=["part", "take"], copy=False)
    song = get(song_id)
    song.part(part_id).di_take = filed.take_id
    song.save()
    return {"aligned": True, "describe": alignment.describe(), "take_id": filed.take_id, "path": str(path)}


def brief(song_id: str, part_id: str | None = None) -> str:
    """Everything known about a song (or one part), for a researcher.

    What an agent is handed before it goes looking for the gear: the song,
    who played on it, the map, and - for a part - what it measures like, so
    the research can be checked against the sound rather than only against
    other sources.
    """
    song = get(song_id)
    lines = [f"Song: {song.title}" + (f" by {song.artist}" if song.artist else "")]
    if song.album or song.year:
        lines.append(f"Release: {song.album or '?'} ({song.year or '?'})")
    credits = song.credits or {}
    if credits.get("guitarists"):
        lines.append("Guitar credits: " + "; ".join(f"{g['name']} ({', '.join(g['instruments'])})" for g in credits["guitarists"]))
    if song.structure:
        lines.append(f"Tempo {song.structure.get('tempo', 0):.0f} BPM, key {song.structure.get('key')}")
    if song.layers:
        lines.append(f"Guitar layers: {song.layers.get('summary')}")
    targets = [song.part(part_id)] if part_id else (song.chosen() or song.parts)
    for part in targets:
        sketch = part.sketch or {}
        lines.append(f"\nPart {part.part_id} ({part.label}, {part.region}s):")
        lines.append(f"  sounds like: {sketch.get('summary', 'not analysed')}")
        for suggestion in sketch.get("suggestions", []):
            lines.append(f"  measurement suggests: {suggestion}")
    lines.append(
        "\nResearch the gear for these parts (amp, cab, pedals, guitar and pickups, "
        "tuning). Cite a source for every stated or reported claim; mark inferences "
        "as inferred. Note where the measurements and the sources disagree."
    )
    return "\n".join(lines)


def render_list(songs: list[Song]) -> str:
    if not songs:
        return (
            "No songs yet.\n\n"
            "A song holds the record you are chasing and a map of its parts.\n"
            "  rigsmith song new 'Going Under' --artist Evanescence --mix going-under.flac\n"
            "  rigsmith song analyse going-under-evanescence"
        )
    return "\n".join(
        f"  {s.song_id:<32} {s.title}" + (f" - {s.artist}" if s.artist else "") + f"  ({len(s.parts)} parts, {len(s.chosen())} chosen)"
        for s in songs
    )

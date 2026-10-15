"""Reading a Standard MIDI File, without a dependency for it.

Most songs anyone wants to chase have a tab somewhere, and every tab editor -
Guitar Pro, TuxGuitar, Songsterr's export - writes MIDI. A tab's notes are a
better starting point than any transcription of a distorted stem: they are
the right notes, on the right strings, which is exactly what a sampler needs.
What they do not have is the record's timing, which :mod:`.align` fixes.

Format 0 and 1, note on/off, tempo changes, and the channel each note was on
(tab exports put each guitar on its own channel or track, which is how a
rhythm part is told from a lead). Everything else in the file is skipped.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import Invalid
from .synth import NoteEvent


@dataclass
class MidiTrack:
    index: int
    name: str = ""
    channels: set[int] = field(default_factory=set)
    notes: list[tuple[float, float, int, int, int]] = field(default_factory=list)
    """(start_s, duration_s, pitch, velocity, channel)"""
    program: int | None = None

    @property
    def is_guitar(self) -> bool:
        """General MIDI programs 25-32 (24-31 zero-based) are guitars."""
        lowered = self.name.lower()
        if any(word in lowered for word in ("guitar", "gtr", "lead", "rhythm")):
            return True
        return self.program is not None and 24 <= self.program <= 31

    def describe(self) -> str:
        if not self.notes:
            return f"track {self.index}: {self.name or '(unnamed)'} - empty"
        low = min(n[2] for n in self.notes)
        high = max(n[2] for n in self.notes)
        from .instrument import midi_to_note

        return (
            f"track {self.index}: {self.name or '(unnamed)'} - {len(self.notes)} notes, "
            f"{midi_to_note(low)}-{midi_to_note(high)}"
            + (" (guitar)" if self.is_guitar else "")
        )


def _varlen(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    while True:
        if pos >= len(data):
            raise Invalid("MIDI file ends inside a variable-length number")
        byte = data[pos]
        pos += 1
        value = (value << 7) | (byte & 0x7F)
        if not byte & 0x80:
            return value, pos


def read(path: str | Path) -> list[MidiTrack]:
    """Every track of a MIDI file, with notes converted to seconds."""
    data = Path(path).read_bytes()
    if data[:4] != b"MThd":
        raise Invalid(f"{Path(path).name} is not a Standard MIDI File")
    header_length = struct.unpack(">I", data[4:8])[0]
    fmt, count, division = struct.unpack(">HHH", data[8:14])
    if division & 0x8000:
        raise Invalid("SMPTE-timed MIDI files are not supported", hint="Re-export with ticks per beat.")
    ticks_per_beat = division
    pos = 8 + header_length

    raw_tracks = []
    while pos < len(data) and len(raw_tracks) < count:
        if data[pos : pos + 4] != b"MTrk":
            break
        length = struct.unpack(">I", data[pos + 4 : pos + 8])[0]
        raw_tracks.append(data[pos + 8 : pos + 8 + length])
        pos += 8 + length

    # Tempo applies across every track, so gather it first.
    tempo_map: list[tuple[int, int]] = [(0, 500000)]
    parsed = []
    for index, chunk in enumerate(raw_tracks):
        events, name, program = _parse_track(chunk)
        for tick, kind, payload in events:
            if kind == "tempo":
                tempo_map.append((tick, payload))
        parsed.append((index, events, name, program))
    tempo_map.sort()

    def seconds(tick: int) -> float:
        total = 0.0
        last_tick, last_tempo = 0, 500000
        for change_tick, tempo in tempo_map:
            if change_tick > tick:
                break
            total += (change_tick - last_tick) * last_tempo / 1e6 / ticks_per_beat
            last_tick, last_tempo = change_tick, tempo
        return total + (tick - last_tick) * last_tempo / 1e6 / ticks_per_beat

    tracks = []
    for index, events, name, program in parsed:
        track = MidiTrack(index=index, name=name, program=program)
        open_notes: dict[tuple[int, int], tuple[int, int]] = {}
        for tick, kind, payload in events:
            if kind == "on":
                channel, pitch, velocity = payload
                key = (channel, pitch)
                if key in open_notes:  # retrigger: close the old one here
                    start, vel = open_notes.pop(key)
                    track.notes.append((seconds(start), seconds(tick) - seconds(start), pitch, vel, channel))
                open_notes[key] = (tick, velocity)
                track.channels.add(channel)
            elif kind == "off":
                channel, pitch = payload
                if (channel, pitch) in open_notes:
                    start, vel = open_notes.pop((channel, pitch))
                    track.notes.append((seconds(start), max(0.01, seconds(tick) - seconds(start)), pitch, vel, channel))
        for (channel, pitch), (start, vel) in open_notes.items():
            track.notes.append((seconds(start), 0.5, pitch, vel, channel))
        track.notes.sort()
        tracks.append(track)
    return tracks


def _parse_track(chunk: bytes):
    events = []
    pos = 0
    tick = 0
    status = 0
    name = ""
    program = None
    while pos < len(chunk):
        delta, pos = _varlen(chunk, pos)
        tick += delta
        if pos >= len(chunk):
            break
        byte = chunk[pos]
        if byte == 0xFF:
            kind = chunk[pos + 1]
            length, pos = _varlen(chunk, pos + 2)
            payload = chunk[pos : pos + length]
            pos += length
            if kind == 0x51 and length == 3:
                events.append((tick, "tempo", int.from_bytes(payload, "big")))
            elif kind in (0x03, 0x04) and not name:
                name = payload.decode("latin-1", "replace").strip()
            elif kind == 0x2F:
                break
            continue
        if byte in (0xF0, 0xF7):
            length, pos = _varlen(chunk, pos + 1)
            pos += length
            continue
        if byte & 0x80:
            status = byte
            pos += 1
        elif not status:
            raise Invalid("MIDI track uses running status before any status byte")
        kind = status & 0xF0
        channel = status & 0x0F
        if kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
            a, b = chunk[pos], chunk[pos + 1]
            pos += 2
            if kind == 0x90 and b > 0:
                events.append((tick, "on", (channel, a, b)))
            elif kind == 0x80 or (kind == 0x90 and b == 0):
                events.append((tick, "off", (channel, a)))
        elif kind in (0xC0, 0xD0):
            if kind == 0xC0 and program is None:
                program = chunk[pos]
            pos += 1
        else:
            pos += 1
    return events, name, program


def to_events(
    tracks: list[MidiTrack],
    *,
    track: int | None = None,
    transpose: int = 0,
    start: float = 0.0,
    end: float | None = None,
) -> list[NoteEvent]:
    """Note events from one track (or every guitar track) of a MIDI file."""
    if track is not None:
        chosen = [t for t in tracks if t.index == track]
        if not chosen:
            raise Invalid(f"no track {track}", hint="Tracks: " + ", ".join(str(t.index) for t in tracks))
    else:
        chosen = [t for t in tracks if t.is_guitar and t.notes] or [t for t in tracks if t.notes]
        # Without a choice, the busiest guitar track - that is usually the
        # part someone opened the file for.
        chosen = sorted(chosen, key=lambda t: len(t.notes), reverse=True)[:1]
    events = []
    for selected in chosen:
        for when, duration, pitch, velocity, channel in selected.notes:
            if when < start or (end is not None and when > end):
                continue
            if channel == 9:  # percussion
                continue
            events.append(
                NoteEvent(
                    start=when - start,
                    duration=duration,
                    midi=float(pitch + transpose),
                    velocity=max(0.05, min(1.0, velocity / 127)),
                )
            )
    return sorted(events, key=lambda e: e.start)


def write(path: str | Path, events: list[NoteEvent], *, tempo_bpm: float = 120.0) -> Path:
    """Write events as a one-track MIDI file - so a transcription can be
    opened and corrected in any tab editor and read back."""
    ticks_per_beat = 480
    seconds_per_tick = 60.0 / tempo_bpm / ticks_per_beat
    messages = []
    for event in events:
        on = int(round(event.start / seconds_per_tick))
        off = int(round((event.start + event.duration) / seconds_per_tick))
        pitch = int(round(event.midi))
        velocity = max(1, min(127, int(round(event.velocity * 127))))
        messages.append((on, 1, bytes([0x90, pitch, velocity])))
        messages.append((max(off, on + 1), 0, bytes([0x80, pitch, 0])))
    messages.sort(key=lambda m: (m[0], m[1]))

    def varlen(value: int) -> bytes:
        out = [value & 0x7F]
        value >>= 7
        while value:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        return bytes(reversed(out))

    body = bytearray()
    tempo = int(60_000_000 / tempo_bpm)
    body += varlen(0) + bytes([0xFF, 0x51, 0x03]) + tempo.to_bytes(3, "big")
    body += varlen(0) + bytes([0xC0, 29])  # overdriven guitar, for a player that cares
    last = 0
    for tick, _, message in messages:
        body += varlen(tick - last) + message
        last = tick
    body += varlen(0) + bytes([0xFF, 0x2F, 0x00])
    data = b"MThd" + struct.pack(">IHHH", 6, 0, 1, ticks_per_beat)
    data += b"MTrk" + struct.pack(">I", len(body)) + bytes(body)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path

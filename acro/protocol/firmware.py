"""Read-only static analysis of a HeadRush firmware image.

Why this exists: the Core ships roughly 97 amp models, each with its own knob
set, and no per-model parameter table is published anywhere. Hand-transcribing
that would be enormous and would rot on the next firmware release. The
firmware image contains the real thing - the Remote Editor's web bundle, the
model catalog, and the code that serialises rigs - so extracting it gives a
catalog that is correct by construction and can be regenerated per release.

**Boundaries.** Everything here reads. Nothing writes to, patches, or
reflashes a device, and there is no code path that could: this module only
ever opens local files. Firmware images and anything extracted from them stay
in the workspace and are never committed - the repository carries derived
documentation only, never vendor binaries or assets.

The packaging format is undocumented, so this works bottom-up: identify the
container, unpack what Python can handle natively, and for everything else
name the external tool needed rather than guessing. Embedded files are also
carved by magic bytes, which works even when the container itself is unknown.
"""

from __future__ import annotations

import gzip
import re
import tarfile
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: Magic signatures worth recognising in an embedded-filesystem image.
#: ``(name, magic, note)``.
MAGICS: tuple[tuple[str, bytes, str], ...] = (
    ("zip", b"PK\x03\x04", "unpacked natively"),
    ("gzip", b"\x1f\x8b\x08", "unpacked natively"),
    ("bzip2", b"BZh", "needs bz2"),
    ("xz", b"\xfd7zXZ", "needs lzma"),
    ("squashfs-le", b"hsqs", "needs unsquashfs (squashfs-tools)"),
    ("squashfs-be", b"sqsh", "needs unsquashfs (squashfs-tools)"),
    ("cramfs", b"E=\xcd\x28", "needs cramfsck"),
    ("ext2/3/4", b"\x53\xef", "needs debugfs or 7z; offset 0x438 in the superblock"),
    ("ubifs", b"UBI#", "needs ubireader"),
    ("jffs2", b"\x85\x19", "needs jefferson"),
    ("cpio", b"070701", "unpacked natively"),
    ("elf", b"\x7fELF", "an executable - read with Ghidra if needed"),
    ("u-boot", b"\x27\x05\x19\x56", "U-Boot image header"),
    ("android-sparse", b"\x3a\xff\x26\xed", "needs simg2img"),
    ("7z", b"7z\xbc\xaf\x27\x1c", "needs py7zr or 7z"),
    ("tar", b"ustar", "unpacked natively; magic sits at offset 257"),
)

#: File types worth pulling out of an unpacked tree.
INTERESTING_SUFFIXES = {
    ".html": "web",
    ".htm": "web",
    ".js": "web",
    ".mjs": "web",
    ".css": "web",
    ".json": "data",
    ".xml": "data",
    ".db": "data",
    ".sqlite": "data",
    ".sqlite3": "data",
    ".rig": "rig",
    ".block": "rig",
    ".nam": "capture",
    ".wav": "audio",
}

#: Strings that suggest a file is part of the Remote Editor or the catalog.
CATALOG_HINTS = (
    "revalver",
    "headrush",
    "amp_model",
    "ampmodel",
    "cabinet",
    "impulse",
    "signal_path",
    "footswitch",
    "scene",
    "rig",
)

_PRINTABLE = re.compile(rb"[\x20-\x7e]{6,}")


#: Magics shorter than this match by chance often enough to be meaningless on
#: their own - a 2-byte signature turns up roughly every 65 KB of random data,
#: so 2 MB of noise "contains" dozens of them. Short matches are still
#: reported, but as low confidence, and they do not count as structure.
STRONG_MAGIC_BYTES = 4


@dataclass
class Signature:
    """A magic signature found at an offset."""

    name: str
    offset: int
    note: str
    confidence: str = "strong"
    """``strong`` for magics long enough to be meaningful, else ``weak``."""

    @property
    def strong(self) -> bool:
        return self.confidence == "strong"

    def render(self) -> str:
        mark = "" if self.strong else "  (weak - may be coincidence)"
        return f"  0x{self.offset:08x}  {self.name:16} {self.note}{mark}"


@dataclass
class FirmwareReport:
    """What static analysis could determine about an image."""

    path: Path
    size: int
    container: str | None = None
    signatures: list[Signature] = field(default_factory=list)
    unpacked_to: Path | None = None
    files: list[Path] = field(default_factory=list)
    web_assets: list[Path] = field(default_factory=list)
    candidate_catalog_files: list[Path] = field(default_factory=list)
    entropy: float = 0.0
    notes: list[str] = field(default_factory=list)
    next_steps: list[str] = field(default_factory=list)

    @property
    def strong_signatures(self) -> list[Signature]:
        """Signatures long enough not to be coincidence."""
        return [s for s in self.signatures if s.strong]

    @property
    def likely_encrypted(self) -> bool:
        """High entropy with no recognisable structure suggests encryption.

        Not proof - a solidly compressed archive also looks like this - but
        worth flagging before someone spends an afternoon on it.
        """
        return self.entropy > 7.9 and not self.strong_signatures

    def render(self) -> str:
        lines = [
            f"{self.path.name}  ({self.size:,} bytes)",
            f"  entropy: {self.entropy:.3f} bits/byte",
        ]
        if self.container:
            lines.append(f"  container: {self.container}")
        if self.likely_encrypted:
            lines.append(
                "  HIGH ENTROPY with no recognisable signatures - the image may "
                "be encrypted or solidly compressed. The live-capture path "
                "(acro probe) does not depend on this and should be preferred."
            )
        if self.signatures:
            lines.append(f"\n  {len(self.signatures)} signature(s):")
            lines.extend(s.render() for s in self.signatures[:40])
            if len(self.signatures) > 40:
                lines.append(f"  ... {len(self.signatures) - 40} more")
        if self.unpacked_to:
            lines.append(f"\n  unpacked to: {self.unpacked_to}")
            lines.append(f"  {len(self.files)} file(s)")
        if self.web_assets:
            lines.append(f"\n  {len(self.web_assets)} web asset(s) - likely the Remote Editor:")
            lines.extend(f"    {p}" for p in self.web_assets[:20])
        if self.candidate_catalog_files:
            lines.append(f"\n  {len(self.candidate_catalog_files)} possible catalog file(s):")
            lines.extend(f"    {p}" for p in self.candidate_catalog_files[:20])
        lines.extend(f"\n  note: {n}" for n in self.notes)
        if self.next_steps:
            lines.append("\n  next steps:")
            lines.extend(f"    - {s}" for s in self.next_steps)
        return "\n".join(lines)


def shannon_entropy(data: bytes) -> float:
    """Entropy in bits per byte. 8.0 means indistinguishable from random."""
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    import math

    entropy = -sum(
        (count / total) * math.log2(count / total) for count in counts.values()
    )
    return abs(entropy)  # avoid a cosmetic -0.0 for uniform data


def find_signatures(data: bytes, *, limit: int = 500) -> list[Signature]:
    """Scan for known container and filesystem magics."""
    found: list[Signature] = []
    for name, magic, note in MAGICS:
        start = 0
        while len(found) < limit:
            offset = data.find(magic, start)
            if offset == -1:
                break
            # The tar magic lives at offset 257 within its 512-byte header.
            if name == "tar" and offset % 512 != 257:
                start = offset + 1
                continue
            found.append(
                Signature(
                    name=name,
                    offset=offset,
                    note=note,
                    confidence="strong" if len(magic) >= STRONG_MAGIC_BYTES else "weak",
                )
            )
            start = offset + len(magic)
    found.sort(key=lambda s: s.offset)
    return found


def extract_strings(data: bytes, *, min_length: int = 6) -> list[str]:
    """Printable ASCII runs, the way ``strings`` does it."""
    return [
        match.group().decode("ascii", errors="replace")
        for match in _PRINTABLE.finditer(data)
        if len(match.group()) >= min_length
    ]


def _unpack_native(path: Path, destination: Path) -> tuple[str | None, list[Path]]:
    """Unpack the archive formats Python handles without external tools."""
    destination.mkdir(parents=True, exist_ok=True)

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            # Refuse path traversal; a firmware image is untrusted input.
            safe = [
                member
                for member in archive.namelist()
                if not member.startswith(("/", "\\")) and ".." not in Path(member).parts
            ]
            archive.extractall(destination, members=safe)
        return "zip", sorted(destination.rglob("*"))

    try:
        if tarfile.is_tarfile(path):
            with tarfile.open(path) as archive:
                archive.extractall(destination, filter="data")
            return "tar", sorted(destination.rglob("*"))
    except (tarfile.TarError, OSError):
        pass

    with path.open("rb") as handle:
        head = handle.read(3)
    if head == b"\x1f\x8b\x08":
        target = destination / path.with_suffix("").name
        try:
            with gzip.open(path, "rb") as source, target.open("wb") as sink:
                while chunk := source.read(1 << 20):
                    sink.write(chunk)
            return "gzip", [target]
        except (OSError, EOFError):
            return None, []

    return None, []


def analyse_firmware(
    path: str | Path,
    *,
    unpack_to: str | Path | None = None,
    scan_bytes: int = 64 * 1024 * 1024,
) -> FirmwareReport:
    """Inspect a firmware image and report what is inside it.

    Args:
        path: The firmware file. Never modified.
        unpack_to: Where to extract, when the container is one Python can
            open. Extraction is skipped entirely when omitted.
        scan_bytes: Cap on how much is read for signature and entropy
            scanning, so a large image does not exhaust memory.

    Raises:
        FileNotFoundError: if the image does not exist.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"no firmware image at {path}")

    size = path.stat().st_size
    with path.open("rb") as handle:
        data = handle.read(scan_bytes)

    report = FirmwareReport(path=path, size=size)
    report.entropy = shannon_entropy(data[: 4 * 1024 * 1024])
    report.signatures = find_signatures(data)

    if size > scan_bytes:
        report.notes.append(
            f"only the first {scan_bytes:,} bytes were scanned; raise "
            "scan_bytes to cover the whole image"
        )

    if report.strong_signatures:
        report.container = report.strong_signatures[0].name

    if unpack_to is not None:
        destination = Path(unpack_to)
        container, files = _unpack_native(path, destination)
        if container:
            report.container = container
            report.unpacked_to = destination
            report.files = [f for f in files if f.is_file()]
        else:
            needed = {
                s.note for s in report.signatures if "needs" in s.note
            }
            if needed:
                report.notes.append(
                    "the container is not one Python can open directly; "
                    + "; ".join(sorted(needed))
                )
            else:
                report.notes.append(
                    "no recognised container - try binwalk for a deeper scan"
                )

    # Classify anything that was unpacked.
    for file in report.files:
        kind = INTERESTING_SUFFIXES.get(file.suffix.lower())
        if kind == "web":
            report.web_assets.append(file)
        elif kind in ("data", "rig"):
            report.candidate_catalog_files.append(file)

    if report.web_assets:
        report.next_steps.append(
            "run acro.protocol.capture.analyse_bundle over the extracted web "
            "assets to recover the Remote Editor API without needing the device"
        )
    if report.candidate_catalog_files:
        report.next_steps.append(
            "inspect the data files for the model catalog, then feed it to "
            "acro.protocol.catalog_extract"
        )
    if not report.files:
        report.next_steps.append(
            "if the container cannot be opened, the live-capture path "
            "(acro probe against a powered-on device) recovers the same API "
            "surface and does not depend on unpacking firmware at all"
        )

    report.notes.append(
        "read-only analysis: no device was contacted and nothing was flashed"
    )
    return report


def mine_catalog_strings(
    paths: list[Path] | Path, *, min_length: int = 6, limit: int = 2000
) -> dict[str, list[str]]:
    """Pull catalog-looking strings out of unpacked files.

    Groups by the hint that matched, so amp names, cabinet names, and
    parameter names can be triaged by hand before being trusted. This is a
    lead generator, not an extractor - a string that looks like a model name
    is not proof that it is one.
    """
    if isinstance(paths, Path):
        paths = [paths]

    grouped: dict[str, list[str]] = {hint: [] for hint in CATALOG_HINTS}
    seen: set[str] = set()

    for path in paths:
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        for value in extract_strings(data, min_length=min_length):
            lowered = value.lower()
            for hint in CATALOG_HINTS:
                if hint in lowered and value not in seen:
                    seen.add(value)
                    grouped[hint].append(value)
                    break
            if len(seen) >= limit:
                break

    return {hint: values for hint, values in grouped.items() if values}

"""Read and reason about Neural Amp Modeler (``.nam``) captures.

A ``.nam`` file is plain JSON:

``version``
    Spec version.
``architecture``
    Model class - usually ``WaveNet`` or ``LSTM``.
``config``
    Architecture-specific hyperparameters. For WaveNet this holds the layer
    array, from which the receptive field can be computed.
``weights``
    Flat array of floats.
``sample_rate``
    Optional; 48 kHz when absent.
``metadata``
    Optional; may carry ``gear_make``, ``gear_model``, ``gear_type``,
    ``tone_type``, and input/output levels in dBu.

**On A1 vs A2.** The file format does not carry a first-class "architecture
generation" field. A2 replaced A1's gated activation with LeakyReLU and grew
the receptive field from roughly 4,100 samples to roughly 6,350. So this
module reports a generation only when there is real evidence for it -
explicit metadata, a recognisable activation, or a receptive field in the
right range - and returns ``UNKNOWN`` rather than guessing. Anything
inferred is labelled as such, because choosing the wrong capture on a
confident-but-wrong label is exactly the failure this project has to avoid.

The HeadRush Core runs A2 Full, A2 Lite, and A1 Standard / Lite / Nano /
Feather.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

DEFAULT_SAMPLE_RATE = 48000

#: Receptive field, in samples, reported for the two generations at their
#: standard sizes. Used as corroborating evidence, never as proof on its own.
A1_STANDARD_RECEPTIVE_FIELD = 4100
A2_RECEPTIVE_FIELD = 6350

#: Architectures the HeadRush Core accepts, per HeadRush's own Cloud page.
HEADRUSH_SUPPORTED = {
    ("A2", "full"),
    ("A2", "lite"),
    ("A1", "standard"),
    ("A1", "lite"),
    ("A1", "nano"),
    ("A1", "feather"),
}

#: Tone descriptors used in NAM metadata.
TONE_TYPES = ("clean", "overdrive", "crunch", "hi_gain", "fuzz")


class Generation(str, Enum):
    """Which NAM architecture generation a capture uses."""

    A1 = "A1"
    A2 = "A2"
    CUSTOM = "custom"
    UNKNOWN = "unknown"


@dataclass
class NamModel:
    """A parsed ``.nam`` capture."""

    path: Path | None = None
    version: str | None = None
    architecture: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    sample_rate: int = DEFAULT_SAMPLE_RATE
    metadata: dict[str, Any] = field(default_factory=dict)
    weight_count: int = 0

    #: How the generation was determined, so callers can weigh it.
    generation: Generation = Generation.UNKNOWN
    generation_evidence: list[str] = field(default_factory=list)
    size: str | None = None
    size_evidence: list[str] = field(default_factory=list)

    # -- metadata conveniences -------------------------------------------

    @property
    def name(self) -> str | None:
        return self.metadata.get("name") or (self.path.stem if self.path else None)

    @property
    def gear_make(self) -> str | None:
        return self.metadata.get("gear_make")

    @property
    def gear_model(self) -> str | None:
        return self.metadata.get("gear_model")

    @property
    def gear_type(self) -> str | None:
        return self.metadata.get("gear_type")

    @property
    def tone_type(self) -> str | None:
        return self.metadata.get("tone_type")

    @property
    def modeled_by(self) -> str | None:
        return self.metadata.get("modeled_by")

    @property
    def input_level_dbu(self) -> float | None:
        value = self.metadata.get("input_level_dbu")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def output_level_dbu(self) -> float | None:
        value = self.metadata.get("output_level_dbu")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def is_calibrated(self) -> bool:
        """True when the capture carries input/output levels.

        Calibrated captures can be gain-staged correctly instead of guessed at,
        which matters when comparing candidates on equal footing.
        """
        return self.input_level_dbu is not None and self.output_level_dbu is not None

    @property
    def receptive_field(self) -> int | None:
        """Receptive field in samples, for WaveNet-style models.

        Computed from the dilation schedule: each layer contributes
        ``dilation * (kernel_size - 1)``, plus one for the initial sample.
        Returns ``None`` for architectures where this does not apply.
        """
        layers = self.config.get("layers_array") or self.config.get("layers")
        if not isinstance(layers, list) or not layers:
            return None

        total = 1
        for layer in layers:
            if not isinstance(layer, dict):
                return None
            dilations = layer.get("dilations")
            kernel = layer.get("kernel_size", 2)
            if not isinstance(dilations, list) or not isinstance(kernel, int):
                return None
            for dilation in dilations:
                if not isinstance(dilation, int):
                    return None
                total += dilation * (kernel - 1)
        return total

    @property
    def headrush_compatible(self) -> bool | None:
        """Whether the Core can load this capture.

        ``None`` when the generation or size could not be determined - an
        honest "cannot tell" rather than an optimistic yes.
        """
        if self.generation in (Generation.UNKNOWN, Generation.CUSTOM):
            return None
        if self.size is None:
            return None
        return (self.generation.value, self.size.lower()) in HEADRUSH_SUPPORTED

    def describe(self) -> str:
        bits = [self.name or "(unnamed)"]
        gear = " ".join(p for p in (self.gear_make, self.gear_model) if p)
        if gear:
            bits.append(f"[{gear}]")
        if self.tone_type:
            bits.append(f"tone={self.tone_type}")
        bits.append(f"arch={self.architecture or '?'}")
        bits.append(f"gen={self.generation.value}")
        if self.size:
            bits.append(f"size={self.size}")
        rf = self.receptive_field
        if rf:
            bits.append(f"rf={rf}")
        bits.append(f"{self.sample_rate} Hz")
        if self.is_calibrated:
            bits.append("calibrated")
        return " ".join(bits)

    def to_dict(self) -> dict:
        return {
            "path": str(self.path) if self.path else None,
            "name": self.name,
            "version": self.version,
            "architecture": self.architecture,
            "generation": self.generation.value,
            "generation_evidence": self.generation_evidence,
            "size": self.size,
            "size_evidence": self.size_evidence,
            "sample_rate": self.sample_rate,
            "receptive_field": self.receptive_field,
            "weight_count": self.weight_count,
            "gear_make": self.gear_make,
            "gear_model": self.gear_model,
            "gear_type": self.gear_type,
            "tone_type": self.tone_type,
            "modeled_by": self.modeled_by,
            "input_level_dbu": self.input_level_dbu,
            "output_level_dbu": self.output_level_dbu,
            "calibrated": self.is_calibrated,
            "headrush_compatible": self.headrush_compatible,
        }


def _detect_generation(model: NamModel) -> tuple[Generation, list[str]]:
    """Work out the architecture generation, recording the evidence.

    Explicit statements beat inference; inference from the activation beats
    inference from the receptive field. When nothing supports a conclusion,
    the answer is UNKNOWN.
    """
    evidence: list[str] = []

    # 1. Explicit, wherever it might have been written.
    for source, key in (
        (model.metadata, "architecture_version"),
        (model.metadata, "arch_version"),
        (model.config, "architecture_version"),
        (model.metadata, "generation"),
    ):
        raw = source.get(key)
        if raw is None:
            continue
        token = str(raw).strip().lower().lstrip("a")
        if token.startswith("2"):
            return Generation.A2, [f"explicit {key}={raw}"]
        if token.startswith("1"):
            return Generation.A1, [f"explicit {key}={raw}"]

    # 2. The activation function is the defining architectural difference.
    activation = str(
        model.config.get("activation")
        or model.config.get("activation_fn")
        or ""
    ).lower()
    if activation:
        if "leaky" in activation or "lrelu" in activation:
            evidence.append(f"activation={activation} (A2 uses LeakyReLU)")
            return Generation.A2, evidence
        if "gated" in activation or "tanh" in activation or "sigmoid" in activation:
            evidence.append(f"activation={activation} (A1 uses a gated activation)")
            return Generation.A1, evidence

    if model.config.get("gated") is True:
        return Generation.A1, ["config.gated=true"]
    if model.config.get("gated") is False:
        return Generation.A2, ["config.gated=false"]

    # 3. Receptive field, as weak corroboration only.
    rf = model.receptive_field
    if rf is not None:
        midpoint = (A1_STANDARD_RECEPTIVE_FIELD + A2_RECEPTIVE_FIELD) / 2
        if rf >= midpoint:
            return Generation.A2, [
                f"receptive field {rf} samples is near the A2 figure "
                f"(~{A2_RECEPTIVE_FIELD}) - inferred, not stated"
            ]
        return Generation.A1, [
            f"receptive field {rf} samples is near the A1-Standard figure "
            f"(~{A1_STANDARD_RECEPTIVE_FIELD}) - inferred, not stated"
        ]

    architecture = (model.architecture or "").lower()
    if architecture and architecture not in {"wavenet", "lstm", "convnet"}:
        return Generation.CUSTOM, [f"unrecognised architecture '{model.architecture}'"]

    return Generation.UNKNOWN, ["no architecture-generation evidence in the file"]


def _detect_size(model: NamModel) -> tuple[str | None, list[str]]:
    """Work out the size tier (standard/lite/feather/nano/full)."""
    for source, key in (
        (model.metadata, "size"),
        (model.config, "size"),
        (model.metadata, "model_size"),
    ):
        raw = source.get(key)
        if raw:
            return str(raw).strip().lower(), [f"explicit {key}={raw}"]

    name = (model.name or "").lower()
    for token in ("feather", "nano", "lite", "standard", "full"):
        if token in name:
            return token, [f"'{token}' appears in the model name - inferred"]

    return None, ["no size tier stated"]


def parse_nam(source: str | Path | dict) -> NamModel:
    """Parse a ``.nam`` file or an already-decoded dict.

    Raises:
        ValueError: if the content is not a NAM capture.
    """
    path: Path | None = None
    if isinstance(source, dict):
        data = source
    else:
        path = Path(source)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("NAM file must contain a JSON object")
    if "weights" not in data and "architecture" not in data:
        raise ValueError(
            "not a NAM capture: no 'weights' or 'architecture' key present"
        )

    weights = data.get("weights") or []
    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    model = NamModel(
        path=path,
        version=str(data["version"]) if data.get("version") is not None else None,
        architecture=data.get("architecture"),
        config=data.get("config") or {},
        sample_rate=int(data.get("sample_rate") or DEFAULT_SAMPLE_RATE),
        metadata=metadata,
        weight_count=len(weights) if isinstance(weights, list) else 0,
    )

    model.generation, model.generation_evidence = _detect_generation(model)
    model.size, model.size_evidence = _detect_size(model)
    return model


def scan_directory(directory: str | Path, *, recursive: bool = True) -> list[NamModel]:
    """Parse every ``.nam`` file under a directory, skipping unreadable ones."""
    directory = Path(directory)
    pattern = "**/*.nam" if recursive else "*.nam"
    models: list[NamModel] = []
    for path in sorted(directory.glob(pattern)):
        try:
            models.append(parse_nam(path))
        except (ValueError, OSError):
            continue
    return models


def rank_for_target(
    models: list[NamModel],
    *,
    gear_make: str | None = None,
    gear_model: str | None = None,
    tone_type: str | None = None,
    prefer_generation: Generation | None = Generation.A2,
    require_headrush_compatible: bool = True,
) -> list[tuple[NamModel, float, list[str]]]:
    """Score captures against a tone target.

    Returns ``(model, score, reasons)`` sorted best first. The score is a
    *ranking* aid built from metadata agreement, not a measure of how close
    the capture sounds - that only comes from actually rendering audio
    through it (see :mod:`acro.match`).
    """
    scored: list[tuple[NamModel, float, list[str]]] = []

    for model in models:
        score = 0.0
        reasons: list[str] = []

        if require_headrush_compatible and model.headrush_compatible is False:
            continue

        if gear_make and model.gear_make:
            if gear_make.lower() in model.gear_make.lower():
                score += 3.0
                reasons.append(f"make matches '{model.gear_make}'")
        if gear_model and model.gear_model:
            target = gear_model.lower()
            actual = model.gear_model.lower()
            if target == actual:
                score += 4.0
                reasons.append(f"model matches '{model.gear_model}'")
            elif target in actual or actual in target:
                score += 2.0
                reasons.append(f"model is close to '{model.gear_model}'")

        if tone_type and model.tone_type:
            if tone_type.lower() == model.tone_type.lower():
                score += 2.0
                reasons.append(f"tone type is {model.tone_type}")

        if prefer_generation and model.generation is prefer_generation:
            score += 1.5
            reasons.append(f"{prefer_generation.value} architecture")

        if model.is_calibrated:
            score += 1.0
            reasons.append("calibrated (input/output dBu known)")

        if model.headrush_compatible is None:
            reasons.append("compatibility unverified - generation or size unknown")

        scored.append((model, score, reasons))

    scored.sort(key=lambda item: -item[1])
    return scored

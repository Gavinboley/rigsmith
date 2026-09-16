"""How a capture's architecture generation is decided, and when it is not.

This figure decides whether a capture is reported as loadable on the Core, so
a guess presented as a finding is worse than an admitted gap. The rule is:
explicit statements beat inference, the activation function beats the
receptive field, and anything else is UNKNOWN.
"""

from __future__ import annotations

import pytest

from rigsmith.tone.nam import (
    A1_STANDARD_RECEPTIVE_FIELD,
    A2_RECEPTIVE_FIELD,
    Generation,
    parse_nam,
)

DILATIONS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]


def wavenet(
    *,
    activation: str | None = "Tanh",
    gated: bool = False,
    dilations: list[int] | None = None,
    kernel_size: int = 3,
    metadata: dict | None = None,
    config_extra: dict | None = None,
    architecture: str = "WaveNet",
) -> dict:
    """A .nam shaped the way the real exporter shapes one.

    The important detail: activation and gated live *inside* each layer, not
    at the top of config.
    """
    layer = {
        "input_size": 1,
        "condition_size": 1,
        "channels": 16,
        "head_size": 8,
        "kernel_size": kernel_size,
        "dilations": dilations if dilations is not None else DILATIONS,
        "gated": gated,
    }
    if activation is not None:
        layer["activation"] = activation

    config = {"layers": [dict(layer), dict(layer)], "head": None, "head_scale": 0.02}
    config.update(config_extra or {})
    return {
        "version": "0.5.2",
        "architecture": architecture,
        "config": config,
        "metadata": metadata if metadata is not None else {"name": "Amp"},
        "weights": [0.1] * 64,
        "sample_rate": 48000,
    }


# --------------------------------------------------------------------------
# the activation, which is where the answer actually lives
# --------------------------------------------------------------------------


def test_the_activation_is_read_from_inside_the_layers():
    """A real .nam has no top-level activation, so reading only there meant
    the defining difference between the generations was never consulted."""
    a1 = parse_nam(wavenet(activation="Tanh"))
    assert a1.generation is Generation.A1
    assert "activation" in a1.generation_evidence[0]

    a2 = parse_nam(wavenet(activation="LeakyReLU"))
    assert a2.generation is Generation.A2
    assert "LeakyReLU" in a2.generation_evidence[0]


def test_a_top_level_activation_still_works():
    """Some exports and every test fixture put it there."""
    document = wavenet(activation=None, config_extra={"activation": "LeakyReLU"})
    assert parse_nam(document).generation is Generation.A2


def test_layers_that_disagree_are_not_guessed_about():
    document = wavenet(activation="Tanh")
    document["config"]["layers"][1]["activation"] = "LeakyReLU"

    model = parse_nam(document)
    assert model.generation is Generation.UNKNOWN
    assert "disagree" in model.generation_evidence[0]
    assert model.headrush_compatible is None


def test_gated_false_does_not_mean_a2():
    """The standard A1 WaveNet config sets gated=false on every layer.

    Reading it as a generation signal reported ordinary A1 captures as A2 -
    and A2 sizes are named differently, so the size check then said the
    capture would not load.
    """
    document = wavenet(activation="Tanh", gated=False)
    document["config"]["gated"] = False  # as some exports write it
    assert parse_nam(document).generation is Generation.A1


# --------------------------------------------------------------------------
# explicit statements win
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key", ["architecture_version", "arch_version", "generation"]
)
def test_an_explicit_statement_beats_the_activation(key):
    document = wavenet(activation="Tanh", metadata={"name": "Amp", key: "2"})
    model = parse_nam(document)
    assert model.generation is Generation.A2
    assert model.generation_evidence == [f"explicit {key}={'2'}"]


def test_an_explicit_statement_accepts_the_a_prefix():
    document = wavenet(activation="LeakyReLU", metadata={"name": "A", "generation": "A1"})
    assert parse_nam(document).generation is Generation.A1


# --------------------------------------------------------------------------
# the receptive field, and the gap where it says nothing
# --------------------------------------------------------------------------


def test_the_receptive_field_matches_the_documented_a1_figure():
    """A cross-check on the dilation arithmetic itself."""
    model = parse_nam(wavenet(activation=None))
    assert model.receptive_field == pytest.approx(A1_STANDARD_RECEPTIVE_FIELD, abs=20)


def test_the_receptive_field_is_used_when_nothing_better_exists():
    model = parse_nam(wavenet(activation=None))
    assert model.generation is Generation.A1
    assert "receptive field" in model.generation_evidence[0]
    assert "inferred, not stated" in model.generation_evidence[0]


def test_a_receptive_field_between_the_references_decides_nothing():
    """The old rule split at the midpoint, so 5224 was A1 and 5226 was A2 -
    a coin flip presented as a finding, driving a loadability verdict."""
    midpoint = (A1_STANDARD_RECEPTIVE_FIELD + A2_RECEPTIVE_FIELD) // 2
    # kernel 3 means each dilation contributes 2 samples, across two layers.
    per_layer = [midpoint // 4]
    model = parse_nam(wavenet(activation=None, dilations=per_layer))

    assert model.receptive_field == pytest.approx(midpoint, rel=0.01)
    assert model.generation is Generation.UNKNOWN
    assert "not near either reference" in " ".join(model.generation_evidence)
    assert model.headrush_compatible is None


def test_a_receptive_field_near_the_a2_figure_reads_as_a2():
    per_layer = [A2_RECEPTIVE_FIELD // 4]
    model = parse_nam(wavenet(activation=None, dilations=per_layer))
    assert model.generation is Generation.A2


def test_an_enormous_receptive_field_is_not_evidence_of_anything():
    model = parse_nam(wavenet(activation=None, dilations=[20000]))
    assert model.generation is Generation.UNKNOWN


# --------------------------------------------------------------------------
# everything else
# --------------------------------------------------------------------------


def test_an_unrecognised_architecture_is_custom_not_a_guess():
    model = parse_nam(
        wavenet(activation=None, architecture="Transformer", dilations=[20000])
    )
    assert model.generation is Generation.CUSTOM
    assert model.headrush_compatible is None


def test_a_file_with_nothing_to_go_on_says_so():
    model = parse_nam(
        {
            "version": "0.5.2",
            "architecture": "LSTM",
            "config": {"hidden_size": 16},
            "metadata": {"name": "Amp"},
            "weights": [0.1] * 8,
        }
    )
    assert model.generation is Generation.UNKNOWN
    assert "no architecture-generation evidence" in model.generation_evidence[-1]


def test_an_unknown_generation_never_claims_the_core_will_load_it():
    model = parse_nam(wavenet(activation=None, dilations=[20000],
                              metadata={"name": "A", "size": "lite"}))
    assert model.generation is Generation.UNKNOWN
    assert model.headrush_compatible is None, "an honest cannot-tell, not a yes"


# --------------------------------------------------------------------------
# size, which is the other half of "will this load"
# --------------------------------------------------------------------------


def test_a_stated_size_is_taken_at_face_value():
    model = parse_nam(wavenet(activation="Tanh", metadata={"name": "A", "size": "lite"}))
    assert model.size == "lite"
    assert model.headrush_compatible is True


def test_a_stated_size_that_the_generation_does_not_have_is_flagged():
    """Reported, not corrected - the file says what it says."""
    model = parse_nam(wavenet(activation="Tanh", metadata={"name": "A", "size": "full"}))
    assert model.size == "full"
    assert "may be mislabelled" in " ".join(model.size_evidence)
    # A1 Full is not a thing the Core accepts, so this must not claim it loads.
    assert model.headrush_compatible is False


def test_a_size_word_in_a_name_must_be_a_whole_word():
    """"Fullerton" is a town, not a model size."""
    model = parse_nam(wavenet(activation="Tanh", metadata={"name": "Fullerton Clean"}))
    assert model.size is None


def test_a_cabinet_name_does_not_become_a_size():
    """The failure this guards: 'Full Stack' on an A1 capture used to read as
    size=full, which is not an A1 size, so a perfectly loadable capture was
    reported as one the Core would refuse."""
    model = parse_nam(wavenet(activation="Tanh", metadata={"name": "JCM800 Full Stack"}))
    assert model.size is None
    assert "A1 has no such size" in " ".join(model.size_evidence)
    assert model.headrush_compatible is None, "cannot tell, rather than 'will not load'"


def test_a_size_word_that_fits_the_generation_is_used():
    a1 = parse_nam(wavenet(activation="Tanh", metadata={"name": "JCM800 standard"}))
    assert a1.size == "standard" and a1.headrush_compatible is True

    a2 = parse_nam(wavenet(activation="LeakyReLU", metadata={"name": "JCM800 full"}))
    assert a2.size == "full" and a2.headrush_compatible is True


def test_an_a2_capture_never_infers_an_a1_only_size():
    model = parse_nam(wavenet(activation="LeakyReLU", metadata={"name": "JCM800 nano"}))
    assert model.size is None
    assert "A2 has no such size" in " ".join(model.size_evidence)


def test_size_inference_is_unconstrained_when_the_generation_is_unknown():
    """With no generation to check against, the name is all there is."""
    document = wavenet(activation=None, dilations=[20000], metadata={"name": "Amp lite"})
    model = parse_nam(document)
    assert model.generation is Generation.UNKNOWN
    assert model.size == "lite"
    # But an unknown generation still cannot claim the Core will load it.
    assert model.headrush_compatible is None

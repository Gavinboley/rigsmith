"""Downloading captures, and checking them against their own contents.

Search reports what TONE3000 believes about a capture. The file is the fact.
Those two disagree often enough that the download path exists partly to catch
it, so most of what is tested here is the disagreement.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from rigsmith import config
from rigsmith.core import captures
from rigsmith.core.errors import Invalid, NotFound, ReadOnly

DILATIONS = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]


def nam_document(*, activation: str, size: str, name: str = "JCM800") -> dict:
    layer = {
        "input_size": 1,
        "condition_size": 1,
        "channels": 16,
        "head_size": 8,
        "kernel_size": 3,
        "dilations": DILATIONS,
        "activation": activation,
    }
    return {
        "version": "0.5.2",
        "architecture": "WaveNet",
        "config": {"layers": [dict(layer), dict(layer)]},
        "metadata": {"name": name, "size": size},
        "weights": [0.1] * 32,
        "sample_rate": 48000,
    }


class FakeModel:
    """Stands in for tone3000.ToneModel."""

    def __init__(self, model_id, *, generation="A2", size="full", name=None):
        self.id = model_id
        self.name = name
        self.size = size
        self._generation = generation
        self.model_url = f"https://example.invalid/{model_id}.nam"

    @property
    def generation(self):
        return self._generation

    @property
    def headrush_compatible(self):
        from rigsmith.tone.nam import HEADRUSH_SUPPORTED

        if self._generation == "unknown" or not self.size:
            return None
        return (self._generation, self.size.lower()) in HEADRUSH_SUPPORTED

    def describe(self):
        return f"{self.name or self.id} {self._generation} {self.size}"


class FakeClient:
    """A TONE3000 client that writes whatever the test says it holds."""

    def __init__(self, models, *, contents=None, tone=None):
        self._models = models
        self._contents = contents or {}
        self._tone = tone
        self.downloaded: list[str] = []

    async def get_tone(self, tone_id, **kwargs):
        if self._tone is None:
            raise RuntimeError("tone endpoint is tier-gated")
        return self._tone

    async def list_models(self, tone_id, **kwargs):
        return list(self._models)

    async def download_model(self, model, destination):
        self.downloaded.append(model.id)
        document = self._contents.get(
            model.id, nam_document(activation="LeakyReLU", size="full")
        )
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(document), encoding="utf-8")
        return destination


class FakeTone:
    title = "Marshall JCM800"
    license = "CC BY-NC 4.0"


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def install(monkeypatch, client):
    monkeypatch.setattr(captures, "_client", lambda: client)
    return client


# --------------------------------------------------------------------------
# the happy path, which still verifies
# --------------------------------------------------------------------------


def test_a_download_lands_on_disk_and_reads_the_file_back(workspace, monkeypatch):
    client = install(
        monkeypatch,
        FakeClient(
            [FakeModel("11", generation="A2", size="full", name="JCM800 Hot")],
            tone=FakeTone(),
        ),
    )

    results = asyncio.run(captures.download("4821"))

    assert client.downloaded == ["11"]
    assert len(results) == 1
    one = results[0]
    assert one.path.is_file()
    assert one.path.parent == workspace / "nam", "downloads belong in the workspace"
    assert one.actual == "A2" and one.claimed == "A2"
    assert not one.disagrees
    assert one.loadable is True
    assert one.tone_title == "Marshall JCM800"
    assert one.licence == "CC BY-NC 4.0"


def test_the_summary_mentions_the_licence(workspace, monkeypatch):
    """Captures are somebody's work."""
    install(monkeypatch, FakeClient([FakeModel("11")], tone=FakeTone()))
    text = captures.render_download(asyncio.run(captures.download("4821")))
    assert "CC BY-NC 4.0" in text
    assert "redistributing" in text


def test_a_tier_gated_tone_endpoint_does_not_block_the_download(workspace, monkeypatch):
    """Metadata is a nicety; the models are the point."""
    install(monkeypatch, FakeClient([FakeModel("11")], tone=None))
    results = asyncio.run(captures.download("4821"))
    assert results[0].path.is_file()
    assert results[0].licence is None


# --------------------------------------------------------------------------
# what the download is actually for
# --------------------------------------------------------------------------


def test_a_file_that_contradicts_its_metadata_is_reported(workspace, monkeypatch):
    """The API said A2; the weights say A1. Trust the weights, report both."""
    install(
        monkeypatch,
        FakeClient(
            [FakeModel("11", generation="A2", size="full")],
            contents={"11": nam_document(activation="Tanh", size="standard")},
            tone=FakeTone(),
        ),
    )

    one = asyncio.run(captures.download("4821"))[0]
    assert one.claimed == "A2"
    assert one.actual == "A1"
    assert one.disagrees
    assert "the API called it A2" in one.describe()

    text = captures.render_download([one])
    assert "disagree" in text
    assert "the weights are what the model is" in text


def test_a_capture_the_core_cannot_load_is_called_out(workspace, monkeypatch):
    install(
        monkeypatch,
        FakeClient(
            [FakeModel("11", generation="A2", size="full")],
            contents={"11": nam_document(activation="Tanh", size="full")},
            tone=FakeTone(),
        ),
    )
    one = asyncio.run(captures.download("4821"))[0]
    # A1 has no "full" size, so the Core will refuse it.
    assert one.loadable is False
    assert "WILL NOT LOAD" in one.describe()
    assert "offline rendering" in captures.render_download([one])


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def test_sizes_the_core_cannot_run_are_skipped_by_default(workspace, monkeypatch):
    client = install(
        monkeypatch,
        FakeClient(
            [
                FakeModel("keep", generation="A2", size="lite"),
                FakeModel("skip", generation="A2", size="gigantic"),
            ],
            tone=FakeTone(),
        ),
    )
    asyncio.run(captures.download("4821"))
    assert client.downloaded == ["keep"]


def test_they_can_be_kept_for_offline_rendering(workspace, monkeypatch):
    """Offline rendering has no size limit, so "will not load" is a fact about
    the device rather than about the capture."""
    client = install(
        monkeypatch,
        FakeClient(
            [FakeModel("skip", generation="A2", size="gigantic")], tone=FakeTone()
        ),
    )
    asyncio.run(captures.download("4821", loadable_only=False))
    assert client.downloaded == ["skip"]


def test_a_tone_with_nothing_loadable_says_what_the_core_accepts(workspace, monkeypatch):
    install(
        monkeypatch,
        FakeClient(
            [FakeModel("skip", generation="A2", size="gigantic")], tone=FakeTone()
        ),
    )
    with pytest.raises(Invalid) as caught:
        asyncio.run(captures.download("4821"))
    assert "A2 Full/Lite" in caught.value.hint
    assert "loadable_only=False" in caught.value.hint


def test_a_tone_with_no_models_is_not_found(workspace, monkeypatch):
    install(monkeypatch, FakeClient([], tone=FakeTone()))
    with pytest.raises(NotFound, match="no models"):
        asyncio.run(captures.download("4821"))


def test_the_limit_caps_how_much_one_tone_costs(workspace, monkeypatch):
    client = install(
        monkeypatch,
        FakeClient([FakeModel(str(n)) for n in range(10)], tone=FakeTone()),
    )
    asyncio.run(captures.download("4821", limit=3))
    assert len(client.downloaded) == 3


# --------------------------------------------------------------------------
# filenames, which come out of other people's uploads
# --------------------------------------------------------------------------


HOSTILE_NAMES = [
    "JCM800 / Hot",
    "amp:model*1",
    "..",
    "   ",
    "a" * 300,
    "../../etc/passwd",
]


@pytest.mark.parametrize("name", HOSTILE_NAMES)
def test_a_hostile_model_name_still_produces_a_usable_filename(
    workspace, monkeypatch, name
):
    install(monkeypatch, FakeClient([FakeModel("11", name=name)], tone=FakeTone()))
    one = asyncio.run(captures.download("4821"))[0]

    assert one.path.is_file()
    assert one.path.suffix == ".nam"
    assert one.path.parent == workspace / "nam", "it must not escape the directory"
    assert len(one.path.name) <= 124


def test_downloading_is_refused_in_read_only_mode(workspace, monkeypatch):
    install(monkeypatch, FakeClient([FakeModel("11")], tone=FakeTone()))
    monkeypatch.setenv("RIGSMITH_READONLY", "1")
    config.set_settings(config.Settings())

    with pytest.raises(ReadOnly):
        asyncio.run(captures.download("4821"))

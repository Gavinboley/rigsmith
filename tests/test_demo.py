"""The demo song: made here, kept, and marked against its own answer."""

from __future__ import annotations

import pytest

pytest.importorskip("scipy", reason="needs pip install 'rigsmith[match]'")
pytest.importorskip("soundfile", reason="needs pip install 'rigsmith[match]'")

from rigsmith import config  # noqa: E402
from rigsmith.core import autopilot, demo, songs  # noqa: E402
from rigsmith.core.errors import Conflict  # noqa: E402
from rigsmith.core.songs import Part  # noqa: E402


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)
    monkeypatch.setenv("RIGSMITH_MODE", "local")
    # A shorter demo: the run is the same, in less time.
    monkeypatch.setattr(demo, "SECTION_S", 5.0)
    config.set_settings(config.Settings())
    yield tmp_path
    config.set_settings(None)


def test_the_demo_is_made_once_with_its_answer_beside_it(workspace):
    song = demo.make()
    assert song.song_id == demo.SONG_ID and song.title == demo.TITLE
    import soundfile

    mix, stem = soundfile.info(song.mix), soundfile.info(song.stem)
    # Stereo, so the double-tracked rhythm maps as a left and a right take.
    assert mix.channels == stem.channels == 2 and mix.samplerate == demo.RATE
    assert mix.frames == stem.frames and mix.frames / mix.samplerate == pytest.approx(5 * 5.0 + 1, abs=0.1)
    known = demo.answer(song.song_id)
    assert known["amps"]["rhythm"]["Type"] == 3 and [s["kind"] for s in known["sections"]][:2] == ["clean", "riff"]
    # Asked again, it is the same song - not a second one, not re-rendered.
    assert demo.make().mix == song.mix and len(songs.list_songs()) == 1


def test_a_song_of_its_own_under_the_demo_s_name_is_left_alone(workspace):
    songs.create("Something", song_id=demo.SONG_ID)
    with pytest.raises(Conflict):
        demo.make()
    assert demo.answer(demo.SONG_ID) is None


def test_a_part_is_whichever_guitar_plays_most_of_it():
    sections = [
        {"start": 0.0, "end": 8.0, "kind": "clean"},
        {"start": 8.0, "end": 16.0, "kind": "riff"},
        {"start": 16.0, "end": 24.0, "kind": "lead"},
    ]
    assert demo._guitar(Part("a", 0, "intro", "stem", 0.0, 8.5), sections) == "clean"
    assert demo._guitar(Part("b", 1, "verse", "left", 7.5, 16.0), sections) == "rhythm"
    assert demo._guitar(Part("c", 2, "solo", "centre", 16.0, 24.0), sections) == "lead"
    # The solo's sides are the rhythm guitar holding the roots.
    assert demo._guitar(Part("d", 2, "solo", "right", 16.0, 24.0), sections) == "rhythm"


def test_a_run_on_the_demo_says_how_close_it_came(workspace):
    song = demo.make()
    request = autopilot.Request(title=demo.TITLE, artist=demo.ARTIST, song_id=song.song_id, research=False, candidates=4, tune_budget=8)
    autopilot.Run(run_id="d1", request=request).save()
    run = autopilot.execute("d1")
    assert run.state == "done", run.describe()
    said = run.demo
    assert said and said["guitar"] in ("rhythm", "lead", "clean")
    # Every voicing of the simulated amp was heard, whatever the gain read.
    assert {r["voicing"] for r in run.results if " + " not in r["label"] and "(tuned" not in r["label"]} == {0, 1, 2, 3}
    assert said["truth"]["Type"] == demo.AMPS[said["guitar"]]["Type"]
    assert isinstance(said["voicing_right"], bool)
    assert "went through Sim" in said["summary"] and run.results[0]["label"] in said["summary"]
    assert said["summary"] in run.describe()


def test_the_demo_is_not_a_song_you_own(workspace):
    from rigsmith.core import setup

    demo.make()
    step = next(s for s in setup.checklist() if s.step_id == "song")
    assert not step.done and "demo" in step.detail
    songs.create("Mine")
    assert next(s for s in setup.checklist() if s.step_id == "song").done


def test_the_assistant_can_start_the_demo(workspace, monkeypatch):
    from rigsmith.agent.roles import ROLES
    from rigsmith.mcp.tools import autopilot as tools

    started = {}
    monkeypatch.setattr(demo, "start", lambda **kwargs: started.update(kwargs) or autopilot.Run(run_id="r9", request=autopilot.Request(title=demo.TITLE)))
    assert "r9" in tools.run_autopilot_demo(bench="virtual")
    assert started["bench"] == "virtual"
    assert "run_autopilot_demo" in ROLES["assistant"].tools and "run_autopilot_demo" in ROLES["assistant"].local_tools


def test_the_whole_demo_is_a_part_for_each_guitar_and_a_scene_each(workspace):
    song = demo.make()
    request = autopilot.Request(title=demo.TITLE, artist=demo.ARTIST, song_id=song.song_id, research=False, candidates=4, tune_budget=8, whole_song=True)
    autopilot.Run(run_id="w1", request=request).save()
    run = autopilot.execute("w1")
    assert run.state == "done", run.describe()
    assert len(run.part_ids) >= 2 and len(run.part_runs) == len(run.part_ids) - 1
    assert run.song_rig and len(run.song_rig["scenes"]) == len(run.part_ids)
    guitars = [run.demo["guitar"]] + [o["guitar"] for o in run.demo.get("others", [])]
    assert len(guitars) == len(run.part_ids) and len(set(guitars)) >= 2, guitars
    assert "...and:" in run.describe()


def test_a_sound_is_chased_once_however_many_sections_play_it():
    from types import SimpleNamespace

    def part(part_id, section, layer, gain, presence=1.0):
        return Part(part_id, 0, section, layer, 0.0, 8.0, presence=presence, sketch={"gain": gain})

    song = SimpleNamespace(parts=[
        part("intro-1-stem", "intro", "stem", 0.02),
        part("verse-2-left", "verse", "left", 0.68), part("verse-2-right", "verse", "right", 0.70),
        part("chorus-3-left", "chorus", "left", 0.56), part("chorus-3-right", "chorus", "right", 0.78),
        part("solo-4-centre", "solo", "centre", 0.80), part("solo-4-left", "solo", "left", 0.31, presence=0.5),
    ])
    picked = [p.part_id for p in songs.sounds(song)]
    # The verse and chorus are one rhythm sound, read 0.56-0.70; the lead and
    # the clean intro are sounds of their own.
    assert picked[0] == "chorus-3-left" and set(picked) == {"chorus-3-left", "solo-4-centre", "intro-1-stem"}, picked


def test_another_song_s_run_has_no_answer_to_give(workspace):
    run = autopilot.Run(run_id="x", request=autopilot.Request(title="Other"), song_id="other-song", results=[{"label": "x"}], part_ids=["p"])
    assert demo.verdict(run) is None


def test_a_voicing_missing_from_the_field_has_a_stand_in():
    from rigsmith.tune.candidates import VIRTUAL_VOICING, stand_in

    for voicing in range(4):
        candidate = stand_in(voicing)
        assert candidate.voicing == voicing and VIRTUAL_VOICING[candidate.tier] == voicing
        assert candidate.cab and candidate.cab_voicing is not None


def test_a_knob_well_off_the_song_s_is_explained(workspace):
    """On the demo the run's 50% gain scored closer than the song's own 70%
    through the cab it chose - making up for the difference, not a miss."""
    song = demo.make()
    song.parts = [Part("solo-4-left", 3, "solo", "left", 15.0, 20.0)]
    song.save()
    best = {"label": "92 Treadplate Modern (tuned)", "voicing": 3, "tier": "modern", "cab_voicing": 2,
            "knobs": {"Amp.GainA": 0.5, "Amp.Bass": 0.6, "Amp.Mid": 0.4, "Amp.Treble": 0.6}}
    run = autopilot.Run(run_id="v", request=autopilot.Request(title=demo.TITLE), song_id=song.song_id, part_ids=["solo-4-left"], results=[best])
    said = demo.verdict(run)
    assert said["guitar"] == "rhythm" and said["voicing_right"] is True
    assert "gain 50% (-20)" in said["summary"] and "making up for" in said["summary"] and "Sim 4x12 V30" in said["summary"]
    # Close knobs need no excuse.
    best["knobs"]["Amp.GainA"] = 0.68
    assert "making up for" not in demo.verdict(run)["summary"]

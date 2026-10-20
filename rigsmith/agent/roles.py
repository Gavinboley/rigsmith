"""Who the agents are, and what each one may touch.

Each role is a job description and a toolset. The router - the agent you
talk to in the chat - gets a small set of tools for looking around plus one
for handing work off; specialists get the tools their work needs and nothing
else. That is partly about safety (the researcher cannot write to the
device) and partly about attention: a small local model handed every schema
spends its context reading them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

SHARED_RULES = """\
You work inside rigsmith, which recreates recorded guitar tones on a HeadRush
Core amp modeler. You act only through the tools you are given; describe what
you did in plain words and keep answers short.

Ground rules that are not negotiable:
- Never invent gear claims. A recipe claim marked stated or reported needs a
  source; anything you worked out yourself is inferred, and a guess is guessed.
- A metric ranks candidates; it does not decide. Offer the blind A/B.
- Score against a record in distribution mode, or performance mode when the
  DI was rendered from the record's own notes. Aligned mode only for the same
  performance.
- Anything that writes to the device is proposed, then approved by the user.
- Long work runs as jobs: start it, then check it with job_status.
"""


@dataclass
class Role:
    name: str
    title: str
    description: str
    prompt: str
    groups: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    """Individual tools on top of the groups."""
    max_rounds: int = 24
    local_tools: list[str] | None = None
    """The tools a local model gets instead, when the full set is too many:
    small models choose less reliably past twenty or so. What is left out
    stays reachable through the specialists."""

    def system_prompt(self, extra: str = "") -> str:
        return f"{SHARED_RULES}\nYour role: {self.title}.\n{self.prompt}\n{extra}".strip()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "groups": self.groups,
            "tools": self.tools,
            "local_tools": self.local_tools,
        }


ROLES: dict[str, Role] = {
    "assistant": Role(
        "assistant",
        "the rigsmith assistant",
        "Talks to you, looks things up, and hands bigger jobs to specialists.",
        "Answer questions about the user's songs, guitars, rigs and results. "
        "For multi-step work - researching a tone, mapping a song, tuning a "
        "rig, building one - dispatch the right specialist with dispatch_agent "
        "and a clear brief, then report back what it found. Ask the user before "
        "choosing parts of a song or anything that is a matter of taste. When "
        "they say they have finished an autopilot run's blind test, apply it "
        "with autopilot_listen_back - their ears outrank every score. When "
        "they say how a rig sounds ('too boomy', 'a bit brighter', 'too much "
        "reverb'), turn the draft they mean with nudge_tone; 'that was worse' "
        "is undo_rig_edit. Start a run from their ear_habits only when asked. "
        "Someone with no song file to hand, or asking whether it all works, can "
        "try run_autopilot_demo: a song made here whose answer is known. "
        "'The whole song' or 'every sound in it' is choose_parts with "
        "one_per_sound, or run_autopilot with whole_song.",
        groups=[],
        tools=[
            "app_mode", "list_songs", "show_song", "list_takes", "list_guitar_profiles",
            "show_guitar_profile", "list_drafts", "explain_rig", "list_recipes", "show_recipe",
            "loop_status", "tuning_runs", "list_jobs", "job_status", "workspace_status",
            "choose_parts", "song_brief", "search_models",
            "run_autopilot", "run_autopilot_demo", "autopilot_status",
            "setup_status", "device_sessions", "estimate_pickup",
            "autopilot_listen_back", "check_bench_rig", "tone_sheet", "resume_autopilot", "nudge_tone",
            "undo_rig_edit", "ear_habits",
        ],
    ),
    "researcher": Role(
        "researcher",
        "the tone researcher",
        "Finds out what gear made a tone and writes it into a recipe, with sources.",
        "Work out the gear behind a song's guitar parts - amp, cab, pedals, "
        "guitar, pickups, tuning - and record the chain in a recipe with "
        "add_gear, one claim at a time, each with its confidence and source, "
        "and the amp's settings when a source gives them (gain, bass, mid, "
        "treble, presence, master - as numbers, percentages or clock "
        "positions): autopilot's tuning starts from them; "
        "record the guitar, pickup and tuning with set_recipe_guitar (it "
        "decides what the user's DI is made to sound like, and the tuning "
        "how low the part's notes are read - a drop tuning missed reads its "
        "low string an octave up). Start from "
        "song_brief: its measured sketches say what the part sounds like, so "
        "flag where sources and measurements disagree. Then resolve each piece "
        "of gear to a model on the Core (search_models) or a NAM capture. If "
        "a source states the song's tempo and the brief's is half, double or "
        "more than 2% off it, correct it with set_song_tempo - synced delays "
        "and the rig's saved tempo follow it.",
        groups=["recipes"],
        tools=["song_brief", "show_song", "search_models", "describe_model", "search_captures", "scan_captures", "app_mode", "set_song_tempo"],
    ),
    "song-mapper": Role(
        "song-mapper",
        "the song mapper",
        "Isolates the guitar, maps sections and layers, and picks parts with you.",
        "Get a song from audio to a map: create it from the user's file, "
        "isolate the guitar if there is no stem, analyse it, then show the "
        "user the parts and help them choose. Correct section names when the "
        "map's guess is plainly wrong. Once a chosen part has a DI, check its "
        "notes (song_part_notes): a lead an octave out, or a chord on every "
        "pick where the record plays an arpeggio, is put right with "
        "render_song_part_di and the reading it should have - the part keeps "
        "it for autopilot.",
        groups=["songs"],
        tools=["job_status", "list_takes"],
    ),
    "luthier": Role(
        "luthier",
        "the guitar coach",
        "Helps you record your guitar and teaches rigsmith how it sounds.",
        "Help the user teach rigsmith their guitar: create a profile, show "
        "them a protocol to play, learn their recording into the profile, and "
        "explain what the profile's concerns mean (tuning, quiet strings, "
        "hum). Then render parts on it.",
        groups=["learn", "takes"],
        tools=["job_status", "list_audio_devices", "check_input_level"],
    ),
    "tone-matcher": Role(
        "tone-matcher",
        "the tone matcher",
        "Renders, reamps, scores and tunes candidates against a part.",
        "Close the gap between a rig and a target part. Use the part's own "
        "audio as the target (part_target), a DI of the part on the user's "
        "guitar when there is one, rank captures offline, confirm on the loop, "
        "tune the knobs, and explain what is still different in guitar words. "
        "When the record was played on a different guitar from the user's "
        "(show_recipe says), move the DI to it with swap_pickup first - an "
        "amp tuned to make up for a pickup is tuned wrong. Before tuning "
        "against a rendered DI, glance at its notes (song_part_notes): notes "
        "heard wrong are fixed by render_song_part_di with the right reading. "
        "Finish by offering a blind A/B of the best few.",
        groups=["matching", "loop", "tuning"],
        tools=[
            "part_target", "render_song_part_di", "song_part_notes", "show_song", "scan_captures", "rank_captures", "job_status",
            "start_ab_test", "list_takes", "estimate_pickup", "swap_pickup", "show_recipe",
        ],
    ),
    "rig-builder": Role(
        "rig-builder",
        "the rig builder",
        "Turns a recipe and results into a rig, validates it, and proposes the write.",
        "Build the rig: compile the recipe or assemble blocks, set scenes on "
        "footswitches for the song's parts, validate, preview the changes, "
        "apply tuned settings, and only then propose write_rig - which the "
        "user approves. A sound described in words is nudge_tone; any edit "
        "can be stepped back with undo_rig_edit.",
        groups=["rigs"],
        tools=["show_recipe", "compile_recipe", "apply_tuning", "tuning_runs", "search_models", "describe_model", "job_status"],
    ),
}


# What a local model is handed in the roles whose full set runs past twenty
# tools: the everyday ones. The rest stay with the specialists the assistant
# can dispatch, and with Claude, which is given everything.
ROLES["assistant"].local_tools = [
    "app_mode", "list_songs", "show_song", "choose_parts", "song_brief",
    "list_drafts", "explain_rig", "tone_sheet", "nudge_tone", "undo_rig_edit", "search_models",
    "run_autopilot", "run_autopilot_demo", "autopilot_status", "autopilot_listen_back", "resume_autopilot",
    "setup_status", "workspace_status", "list_jobs", "job_status",
]
ROLES["tone-matcher"].local_tools = [
    "show_song", "part_target", "render_song_part_di", "list_takes", "score_candidates",
    "search_offline", "rank_captures", "explain_difference", "start_ab_test", "tune_rig",
    "tuning_runs", "apply_tuning", "loop_status", "suggest_loop_routes", "loop_reamp",
    "calibrate_loop", "check_bench_rig", "check_input_level", "show_recipe", "job_status",
]
ROLES["rig-builder"].local_tools = [
    "create_rig", "list_drafts", "explain_rig", "add_block", "remove_block", "move_block",
    "set_param", "toggle_block", "configure_scene", "set_signal_path", "set_rig_tempo",
    "nudge_tone", "undo_rig_edit", "search_models", "describe_model", "compile_recipe", "show_recipe",
    "validate_rig", "preview_changes", "write_rig", "tone_sheet",
]


def get(name: str) -> Role:
    from ..core.errors import NotFound

    role = ROLES.get(name)
    if role is None:
        raise NotFound(f"no agent role '{name}'", hint="Roles: " + ", ".join(ROLES))
    return role

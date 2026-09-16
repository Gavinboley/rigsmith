"""Tests for the command line interface.

The CLI had no tests at all, which is how three separate error messages ended
up telling users to run commands that were never built - `rigsmith tone3000
login`, `rigsmith catalog extract`, `rigsmith rig new`. The last test in this
file exists specifically to make that class of drift impossible: it scans the
codebase for anything that names a rigsmith command and asserts it resolves.
"""

from __future__ import annotations

import re
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from rigsmith.cli import main

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "rigsmith"
DOCS_ROOT = Path(__file__).resolve().parent.parent / "docs"


@pytest.fixture()
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)

    from rigsmith import config
    from rigsmith.core import drafts

    config.set_settings(config.Settings())
    drafts.set_store(drafts.DraftStore(tmp_path / "drafts"))
    yield CliRunner()
    config.set_settings(None)
    drafts.set_store(None)


def invoke(runner: CliRunner, *args: str):
    return runner.invoke(main, list(args), catch_exceptions=False)


# --------------------------------------------------------------------------
# structure
# --------------------------------------------------------------------------


def walk_commands(command, prefix=()):
    """Yield every (path, command) pair in the tree."""
    yield prefix, command
    if isinstance(command, click.Group):
        for name, child in command.commands.items():
            yield from walk_commands(child, prefix + (name,))


def all_command_paths() -> set[tuple[str, ...]]:
    return {path for path, _ in walk_commands(main) if path}


def test_expected_groups_exist():
    groups = {p[0] for p in all_command_paths() if len(p) == 1}
    assert {
        "rig", "device", "nam", "tone3000", "match",
        "firmware", "workspace", "jobs", "discover", "probe",
        "doctor", "serve", "ui",
    } <= groups


def test_rig_authoring_is_reachable_without_an_agent():
    """The whole point: a human can build a rig from the terminal."""
    paths = all_command_paths()
    for verb in ("new", "add", "rm", "move", "set", "scene", "validate",
                 "preview", "export", "write", "delete", "duplicate"):
        assert ("rig", verb) in paths, f"rig {verb} is missing"


def test_tone_matching_is_reachable_without_an_agent():
    paths = all_command_paths()
    for verb in ("isolate", "capture-di", "reamp", "render", "search", "score"):
        assert ("match", verb) in paths, f"match {verb} is missing"
    for verb in ("start", "next", "answer", "results", "list"):
        assert ("match", "ab", verb) in paths, f"match ab {verb} is missing"


def test_midi_is_reachable():
    """MIDI was fully implemented and wired to nothing."""
    paths = all_command_paths()
    for verb in ("ports", "select", "footswitch", "block", "verify"):
        assert ("device", "midi", verb) in paths


def test_every_command_has_help_text():
    for path, command in walk_commands(main):
        if not path:
            continue
        assert command.help or command.short_help, f"{' '.join(path)} has no help"


def test_help_renders_for_every_command(runner):
    for path, _ in walk_commands(main):
        result = invoke(runner, *path, "--help")
        assert result.exit_code == 0, f"{' '.join(path)} --help failed"


# --------------------------------------------------------------------------
# behaviour
# --------------------------------------------------------------------------


def test_build_a_rig_end_to_end(runner):
    assert invoke(runner, "rig", "new", "Cherub Rock").exit_code == 0

    for category, model in [
        ("effect", "Big Muff"),
        ("nam", "JMP-1959"),
        ("ir", "1960A"),
        ("effect", "Delay"),
    ]:
        result = invoke(runner, "rig", "add", "cherub-rock", category, model)
        assert result.exit_code == 0, result.output

    result = invoke(
        runner, "rig", "scene", "cherub-rock", "Verse", "--off", "1", "--fs", "1"
    )
    assert result.exit_code == 0

    shown = invoke(runner, "rig", "show", "cherub-rock")
    assert "Big Muff -> NAM(JMP-1959) -> IR(1960A) -> Delay" in shown.output
    assert "Verse" in shown.output


def test_scene_updates_merge_from_the_cli(runner):
    invoke(runner, "rig", "new", "S")
    for index in range(4):
        invoke(runner, "rig", "add", "s", "effect", f"p{index}")
    invoke(runner, "rig", "scene", "s", "Lead", "--on", "1", "--fs", "2")
    result = invoke(runner, "rig", "scene", "s", "Lead", "--on", "2")
    assert "on=[1, 2]" in result.output
    assert "footswitch=2" in result.output


def test_params_accept_json_and_bare_strings(runner):
    invoke(runner, "rig", "new", "P")
    result = invoke(
        runner, "rig", "add", "p", "nam", "Amp",
        "--param", "gain=0.72", "--param", 'size="A2 Lite"',
    )
    assert result.exit_code == 0

    from rigsmith.core import rigs

    block = rigs.get("p").rig.block_at(1)
    assert block.params["gain"] == 0.72
    assert block.params["size"] == "A2 Lite"


def test_duplicate_and_delete(runner):
    invoke(runner, "rig", "new", "Original")
    invoke(runner, "rig", "add", "original", "nam", "Amp")

    assert invoke(runner, "rig", "duplicate", "original").exit_code == 0
    assert len(invoke(runner, "rig", "list").output.strip().splitlines()) == 2

    assert invoke(runner, "rig", "delete", "original-copy", "--yes").exit_code == 0
    assert "original-copy" not in invoke(runner, "rig", "list").output


def test_unknown_draft_exits_nonzero_and_names_the_real_ones(runner):
    invoke(runner, "rig", "new", "Real")
    result = invoke(runner, "rig", "show", "ghost")
    assert result.exit_code == 1
    assert "no draft 'ghost'" in result.output
    assert "real" in result.output.lower()


def test_invalid_input_exits_two(runner):
    result = invoke(runner, "workspace", "prune", "not-an-area")
    assert result.exit_code == 2
    assert "unknown workspace area" in result.output


def test_validate_exits_nonzero_on_an_invalid_rig(runner):
    invoke(runner, "rig", "new", "Bad")
    invoke(runner, "rig", "add", "bad", "nam", "Amp")
    invoke(runner, "rig", "scene", "bad", "Nope", "--on", "13")

    result = invoke(runner, "rig", "validate", "bad")
    assert result.exit_code == 1
    assert "INVALID" in result.output


def test_readonly_blocks_a_write(runner, monkeypatch):
    invoke(runner, "rig", "new", "Locked")
    result = invoke(runner, "--readonly", "rig", "export", "locked")
    assert result.exit_code != 0
    assert "read-only" in result.output.lower()


def test_workspace_prune_is_a_dry_run_by_default(runner, tmp_path):
    (tmp_path / "captures" / "old").mkdir(parents=True)
    (tmp_path / "captures" / "old" / "f.bin").write_bytes(b"x" * 100)

    result = invoke(runner, "workspace", "prune")
    assert result.exit_code == 0
    assert "Dry run" in result.output
    assert (tmp_path / "captures" / "old").exists(), "dry run must not delete"


def test_workspace_prune_protects_backups(runner, tmp_path):
    (tmp_path / "backups" / "b1").mkdir(parents=True)
    (tmp_path / "backups" / "b1" / "f.bin").write_bytes(b"x" * 100)

    result = invoke(runner, "workspace", "prune")
    assert "backups (protected" in result.output
    assert (tmp_path / "backups" / "b1").exists()


def test_doctor_reports_without_crashing(runner):
    result = invoke(runner, "doctor")
    assert result.exit_code == 0
    assert "workspace:" in result.output
    assert "TONE3000" in result.output


# --------------------------------------------------------------------------
# the anti-drift test
# --------------------------------------------------------------------------


#: A command reference is one the reader is meant to type, so it always
#: appears quoted, backticked, or on its own line in a shell block. Matching
#: bare prose would flag sentences like "rigsmith learns the device".
#: The negative lookbehind skips keyword-argument values such as
#: `title="rigsmith recorder"`, which name a window, not a command.
QUOTED_REFERENCE = re.compile(
    r"""(?<![=(])['"`]\s*rigsmith ([a-z][a-z0-9 -]*?)\s*['"`]"""
)
SHELL_LINE_REFERENCE = re.compile(r"^\s*(?:\$ )?rigsmith ([a-z][a-z0-9 -]*)", re.M)

#: Option-like and argument-like trailing words are not subcommands.
def _clean(raw: str) -> tuple[str, ...]:
    words = []
    for word in raw.split():
        if word.startswith("-") or word.isupper() or "<" in word:
            break
        words.append(word)
    return tuple(words)


def referenced_commands() -> set[tuple[str, ...]]:
    """Every `rigsmith ...` invocation the project tells someone to run."""
    found: set[tuple[str, ...]] = set()
    sources = list(PACKAGE_ROOT.rglob("*.py"))
    if DOCS_ROOT.is_dir():
        sources += list(DOCS_ROOT.rglob("*.md"))

    for path in sources:
        text = path.read_text(encoding="utf-8", errors="replace")
        # In Python sources only quoted references count - a docstring line
        # like "rigsmith drives a HeadRush Core" is prose, not an invocation.
        patterns = [QUOTED_REFERENCE]
        if path.suffix == ".md":
            patterns.append(SHELL_LINE_REFERENCE)
        for pattern in patterns:
            for match in pattern.findall(text):
                words = _clean(match)
                if words:
                    found.add(words)
    return found


def test_every_referenced_command_actually_exists():
    """Error messages and docs must not name commands that do not exist.

    Three of them did: 'rigsmith tone3000 login', 'rigsmith catalog extract',
    and 'rigsmith rig new'. Each sent whoever followed it - human or agent -
    into a wall.
    """
    real = all_command_paths()
    missing = []

    for words in sorted(referenced_commands()):
        # A reference may name a prefix of a longer path; accept any prefix
        # that resolves, so "rigsmith rig" is fine even though it is a group.
        for length in range(len(words), 0, -1):
            if words[:length] in real:
                break
        else:
            missing.append(" ".join(words))

    assert not missing, (
        "these commands are named in code or docs but do not exist: "
        + ", ".join(f"'rigsmith {m}'" for m in missing)
    )

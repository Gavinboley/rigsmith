"""Tests that keep the documentation honest.

The README previously claimed a test count that was wrong, and pointed at
commands that did not exist. Both are the same failure: prose drifting away
from the thing it describes, with nothing to catch it. These assertions make
the drift fail the build instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
DOCS = ROOT / "docs"


def test_readme_test_count_is_accurate(request):
    """The README states a number; it has to be the real one.

    Only meaningful on a full run - ``testscollected`` counts what *this*
    invocation collected, so running a single file would otherwise report a
    mismatch that says nothing about the README.
    """
    targets = [str(arg) for arg in request.config.args]
    whole_suite = all(
        Path(target).resolve() in (ROOT, ROOT / "tests") for target in targets
    )
    if not whole_suite:
        pytest.skip("partial run - the collected count is not the suite total")

    # A missing optional dependency skips whole modules at import, which moves
    # the total without anything being wrong with the README. Only a complete
    # install can answer this question, so elsewhere it declines to guess
    # rather than failing someone who installed a subset on purpose.
    from importlib.util import find_spec

    missing = [
        name
        for name in ("mcp", "fastapi", "soundfile", "scipy", "pytest_asyncio", "playwright")
        if find_spec(name) is None
    ]
    if missing:
        pytest.skip(
            f"not a full install ({', '.join(missing)} absent) - the collected "
            "count would be short through no fault of the README"
        )

    claimed = re.search(r"covered by ([\d,]+) tests", README.read_text(encoding="utf-8"))
    assert claimed, "the README should state how many tests there are"

    stated = int(claimed.group(1).replace(",", ""))
    actual = request.session.testscollected
    assert stated == actual, (
        f"README claims {stated} tests but the suite collects {actual}. "
        "Update the README."
    )


def test_readme_tool_count_is_accurate():
    text = README.read_text(encoding="utf-8")
    claimed = re.search(r"\((\d+) tools\)", text)
    assert claimed, "the README should state how many MCP tools there are"

    pytest.importorskip("mcp")
    import asyncio

    from rigsmith.mcp import server

    actual = len(asyncio.run(server.mcp.list_tools()))
    assert int(claimed.group(1)) == actual, (
        f"README claims {claimed.group(1)} MCP tools but {actual} are registered."
    )


def test_internal_links_resolve():
    """Every relative link in the docs points at a file that exists."""
    broken: list[str] = []
    sources = [README] + sorted(DOCS.glob("*.md"))

    for source in sources:
        text = source.read_text(encoding="utf-8")
        for target in re.findall(r"\]\((?!https?://|#|mailto:)([^)#]+)", text):
            resolved = (source.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{source.name} -> {target}")

    assert not broken, "broken links: " + ", ".join(broken)


def test_readme_documents_every_extra():
    """A new extra that nobody is told about may as well not exist."""
    # tomllib is stdlib only from 3.11; the check still runs on newer jobs.
    tomllib = pytest.importorskip("tomllib", reason="needs Python 3.11+")

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = set(pyproject["project"]["optional-dependencies"]) - {"all"}
    text = README.read_text(encoding="utf-8")

    missing = [name for name in sorted(extras) if f"`{name}`" not in text]
    assert not missing, f"extras not mentioned in the README: {missing}"


def test_safety_claims_are_still_true():
    """The README promises things the code has to keep doing."""
    text = README.read_text(encoding="utf-8").lower()
    assert "does **not** modify, patch, or reflash device firmware" in text.replace(
        "does not modify", "does **not** modify"
    ) or "reflash" in text

    # The read-only guard the README names must exist and be wired in.
    from rigsmith.config import Settings

    assert hasattr(Settings(), "readonly")

    source = (ROOT / "rigsmith" / "config.py").read_text(encoding="utf-8")
    assert "RIGSMITH_READONLY" in source


def test_the_agent_facing_docstrings_carry_the_caveats_that_matter():
    """An agent's only interface to this tool is what the tools say about
    themselves. A caveat that lives only in docs/ does not reach it."""
    pytest.importorskip("mcp")
    import asyncio

    from rigsmith.mcp import server

    tools = {t.name: (t.description or "") for t in asyncio.run(server.mcp.list_tools())}

    required = {
        # The write is the one irreversible action, and the backup gate is
        # the thing an agent is most likely to try to work around.
        "write_rig": ["backup", "refused", "confirm"],
        # Mode choice is the single decision that makes tone numbers
        # meaningful or meaningless.
        "score_candidates": ["aligned", "distribution", "ranking"],
        # An inference presented as a fact is the failure mode here.
        "inspect_capture": ["evidence", "generation"],
        # Cooperative cancellation is a contract, not an implementation note.
        "cancel_job": ["checkpoint"],
        # A separated stem is not a DI, and agents will treat it as one.
        "isolate_guitar": ["distribution"],
    }

    missing: list[str] = []
    for name, phrases in required.items():
        assert name in tools, f"{name} is not registered"
        text = tools[name].lower()
        missing.extend(
            f"{name}: '{phrase}'" for phrase in phrases if phrase.lower() not in text
        )

    assert not missing, "agent-facing docstrings lost a caveat: " + "; ".join(missing)

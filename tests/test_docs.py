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
    import tomllib

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

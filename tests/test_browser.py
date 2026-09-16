"""The UI, in a real browser.

Everything else about the web interface is tested through HTTP, which proves
the server answers correctly and proves nothing about the page. A typo in a
panel, a handler bound to an element that was renamed, a vendored library
that does not attach its global - all of it loads without complaint and fails
only when a person clicks the thing.

These tests are deliberately few and broad. They are here to catch "the panel
is dead", not to pin down layout.

They need a browser. Playwright's own chromium if it is downloaded, otherwise
the Chrome or Edge already on the machine; with none of those they skip.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager

import pytest

pytest.importorskip("playwright", reason="browser tests need pip install 'rigsmith[browser]'")
pytest.importorskip("fastapi", reason="the UI is optional; pip install 'rigsmith[ui]'")

from playwright.sync_api import sync_playwright  # noqa: E402

CHANNELS = [None, "chrome", "msedge"]


@pytest.fixture()
def browser():
    """The first browser that will actually launch here.

    Per test, not per session, and that is deliberate: sync_playwright keeps
    an asyncio loop running for as long as it is open, and a loop left
    running in this thread makes every later asyncio.run() in the suite fail
    with "cannot be called from a running event loop". A browser launch is
    about a second; a suite that only fails when run in one particular order
    is worth a great deal more than that.
    """
    with sync_playwright() as play:
        failures = []
        for channel in CHANNELS:
            try:
                launched = play.chromium.launch(
                    channel=channel, args=["--autoplay-policy=no-user-gesture-required"]
                )
            except Exception as exc:  # not installed, or no sandbox
                failures.append(f"{channel or 'bundled chromium'}: {str(exc)[:80]}")
                continue
            try:
                yield launched
            finally:
                launched.close()
            return
        pytest.skip("no usable browser: " + "; ".join(failures))


@pytest.fixture()
def server(tmp_path, monkeypatch):
    """The real app, on a real port, with an isolated workspace."""
    import uvicorn

    monkeypatch.setenv("RIGSMITH_WORKSPACE", str(tmp_path))
    monkeypatch.delenv("RIGSMITH_READONLY", raising=False)

    from rigsmith import config
    from rigsmith.core import drafts
    from rigsmith.web.server import build_app

    config.set_settings(config.Settings())
    drafts.set_store(drafts.DraftStore(tmp_path / "drafts"))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    running = uvicorn.Server(
        uvicorn.Config(build_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not running.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not running.started:  # pragma: no cover - only on a wedged machine
        pytest.fail("the server never came up")

    yield f"http://127.0.0.1:{port}"

    running.should_exit = True
    thread.join(timeout=10)
    config.set_settings(None)
    drafts.set_store(None)


@contextmanager
def page_at(browser, url):
    """A page that fails the test if the console reports an error."""
    page = browser.new_page()
    problems: list[str] = []
    page.on(
        "console",
        lambda message: problems.append(f"console.{message.type}: {message.text}")
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: problems.append(f"uncaught: {error}"))

    page.goto(url, wait_until="networkidle")
    try:
        yield page
        # A failed fetch to a closed server during teardown is not the page's
        # fault, so only what happened while the test ran counts.
        assert not problems, "the page reported errors:\n  " + "\n  ".join(problems)
    finally:
        page.close()


def audio_file(directory, name="riff.wav", seconds=3.0):
    sf = pytest.importorskip("soundfile")
    import numpy as np

    rate = 48000
    t = np.arange(int(seconds * rate)) / rate
    tone = np.tanh(np.sin(2 * np.pi * 196 * t) * 4)
    path = directory / name
    sf.write(str(path), tone.astype("float32"), rate)
    return path


# --------------------------------------------------------------------------
# it loads at all
# --------------------------------------------------------------------------


def test_the_page_boots_without_a_console_error(browser, server):
    with page_at(browser, server) as page:
        assert page.title() == "rigsmith"
        page.wait_for_selector("#tabs .tab[aria-selected='true']")


def test_every_tab_opens_its_panel(browser, server):
    """Each tab is wired to a module; a broken one leaves the panel blank."""
    with page_at(browser, server) as page:
        for name in ("recipes", "rig", "captures", "match", "device", "workspace"):
            page.click(f"#tabs .tab[data-panel='{name}']")
            panel = page.locator(f"#panels .panel[data-panel='{name}']")
            panel.wait_for(state="visible")
            assert panel.is_visible(), f"the {name} panel did not open"


# --------------------------------------------------------------------------
# the panels do their job
# --------------------------------------------------------------------------


def test_a_rig_can_be_built_from_the_page(browser, server):
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='rig']")
        page.once("dialog", lambda dialog: dialog.accept())

        # The draft is created through the API the page itself uses, because
        # the modal is a dialog element and this test is about the board.
        page.evaluate(
            """async () => {
                const created = await fetch('/api/drafts', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'Browser Rig'}),
                }).then((r) => r.json());
                await fetch(`/api/drafts/${created.draft_id}/blocks`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({category: 'nam', model_id: 'JMP-1959'}),
                });
            }"""
        )
        page.reload(wait_until="networkidle")
        page.click("#tabs .tab[data-panel='rig']")

        page.wait_for_selector("#board .slot[data-category='nam']", timeout=10000)
        assert "JMP-1959" in page.inner_text("#board")
        assert page.inner_text("#cpu-value").strip() not in ("", "--")


def test_the_research_panel_shows_confidence_and_refuses_an_unsourced_claim(
    browser, server
):
    with page_at(browser, server) as page:
        page.evaluate(
            """async () => {
                await fetch('/api/recipes', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: 'Cherub Rock'}),
                });
                await fetch('/api/recipes/cherub-rock/gear', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({role: 'fuzz', name: 'Big Muff'}),
                });
            }"""
        )
        page.reload(wait_until="networkidle")
        page.click("#tabs .tab[data-panel='recipes']")

        page.wait_for_selector("#recipe-gear .item")
        gear = page.inner_text("#recipe-gear")
        assert "Big Muff" in gear
        assert "guessed" in gear, "the confidence marker is the point of the panel"
        assert "unsourced" in gear


# --------------------------------------------------------------------------
# the waveform, which is the newest and least proven part
# --------------------------------------------------------------------------


def test_opening_a_target_draws_a_waveform(browser, server, tmp_path):
    path = audio_file(tmp_path)
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(path))
        page.click("#open-target")

        page.wait_for_selector("#target-wave-block:not([hidden])", timeout=15000)
        page.wait_for_selector("#target-wave canvas", timeout=15000)
        assert "riff.wav" in page.inner_text("#target-wave-name")
        assert "3.0s" in page.inner_text("#target-wave-name")
        assert page.inner_text("#target-region-label") == "whole file"


def test_dragging_across_the_waveform_selects_a_region(browser, server, tmp_path):
    """The whole reason the waveform is there."""
    path = audio_file(tmp_path)
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(path))
        page.click("#open-target")
        page.wait_for_selector("#target-wave canvas", timeout=15000)

        box = page.locator("#target-wave").bounding_box()
        page.mouse.move(box["x"] + box["width"] * 0.25, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(
            box["x"] + box["width"] * 0.75,
            box["y"] + box["height"] / 2,
            steps=12,
        )
        page.mouse.up()

        label = page.locator("#target-region-label")
        label.wait_for()
        page.wait_for_function(
            "() => document.querySelector('#target-region-label').textContent "
            "!== 'whole file'",
            timeout=5000,
        )
        text = label.inner_text()
        assert "s-" in text, f"expected a span, got {text!r}"

        page.click("#target-clear")
        page.wait_for_function(
            "() => document.querySelector('#target-region-label').textContent "
            "=== 'whole file'",
            timeout=5000,
        )


def test_dragging_a_block_reorders_the_chain(browser, server):
    """Sortable reports a list index; the rig speaks in slot numbers.

    Those are the same number only when the board has no gaps, and reading
    the slot off the dropped element returns the dragged block's own slot.
    Both mistakes look right until you drag something.
    """
    with page_at(browser, server) as page:
        page.evaluate(
            """async () => {
                const created = await fetch('/api/drafts', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'Drag Rig'}),
                }).then((r) => r.json());
                for (const id of ['fuzz', 'amp', 'delay']) {
                    await fetch(`/api/drafts/${created.draft_id}/blocks`, {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({category: 'effect', model_id: id}),
                    });
                }
            }"""
        )
        page.reload(wait_until="networkidle")
        page.click("#tabs .tab[data-panel='rig']")
        page.wait_for_selector("#board .slot[data-slot='3']")

        order = lambda: page.eval_on_selector_all(
            "#board .slot[data-slot] .slot-name", "nodes => nodes.map(n => n.textContent)"
        )
        assert order() == ["fuzz", "amp", "delay"], order()

        source = page.locator("#board .slot[data-slot='1']")
        target = page.locator("#board .slot[data-slot='3']")
        start = source.bounding_box()
        end = target.bounding_box()

        page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
        page.mouse.down()
        # Sortable needs movement to begin the drag, then a settled position.
        page.mouse.move(
            end["x"] + end["width"] / 2, end["y"] + end["height"] / 2, steps=20
        )
        page.mouse.move(
            end["x"] + end["width"] / 2, end["y"] + end["height"] - 4, steps=8
        )
        page.mouse.up()

        page.wait_for_function(
            "() => Array.from("
            "document.querySelectorAll('#board .slot[data-slot] .slot-name')"
            ").map(n => n.textContent).join() !== 'fuzz,amp,delay'",
            timeout=10000,
        )

        # Where exactly it lands depends on Sortable's hit-testing against a
        # list that is reflowing under the cursor, so this pins the invariants
        # rather than the drop index: the chain reordered, and it is still a
        # whole rig afterwards. A slot number read off the wrong element
        # breaks one of these.
        moved = order()
        assert moved != ["fuzz", "amp", "delay"], "the drag did nothing"
        assert moved[0] == "amp", f"fuzz should no longer lead the chain: {moved}"
        assert sorted(moved) == ["amp", "delay", "fuzz"], f"a block was lost: {moved}"

        slots = page.eval_on_selector_all(
            "#board .slot:not(.empty)", "nodes => nodes.map(n => n.dataset.slot)"
        )
        assert slots == ["1", "2", "3"], f"the chain has a gap or a duplicate: {slots}"

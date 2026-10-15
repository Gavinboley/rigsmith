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
    # Backed off the rails. Saturated to exactly full scale it reads as
    # clipped, correctly - which would make every fixture here look like a
    # problem take.
    tone = tone / np.abs(tone).max() * 0.8
    path = directory / name
    sf.write(str(path), tone.astype("float32"), rate, subtype="FLOAT")
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
        # Sortable decides the drop index from the last position it saw, and
        # the list is reflowing underneath; one more move at rest lets it
        # settle before the button comes up.
        page.wait_for_timeout(100)
        page.mouse.move(
            end["x"] + end["width"] / 2, end["y"] + end["height"] - 4, steps=2
        )
        page.mouse.up()

        # Wait for the settled state, not the first change. Sortable moves the
        # element in the DOM immediately; the slot numbers only become
        # contiguous again once the server has answered and the board has
        # re-rendered, so that is the thing to wait on.
        page.wait_for_function(
            """() => {
                const slots = Array.from(
                    document.querySelectorAll('#board .slot:not(.empty)')
                );
                const numbers = slots.map((n) => n.dataset.slot).join();
                const names = slots
                    .map((n) => n.querySelector('.slot-name').textContent)
                    .join();
                return numbers === '1,2,3' && names !== 'fuzz,amp,delay';
            }""",
            timeout=15000,
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


def test_the_score_to_listen_loop_runs_in_the_page(browser, server, tmp_path):
    """Score, then blind-test the winners, without leaving the panel.

    Starting a listening session used to be a CLI-only step, which made the
    deciding stage of the whole method the one stage the interface could not
    do.
    """
    target = audio_file(tmp_path, "target.wav")
    first = audio_file(tmp_path, "bright-capture.wav")
    second = audio_file(tmp_path, "dark-capture.wav", seconds=3.5)

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(target))
        page.fill("#candidate-paths", f"{first},{second}")
        page.click("#score-btn")

        page.wait_for_selector("#ranking .item", timeout=20000)
        assert page.locator("#ranking .item").count() == 2

        page.click("#ranking button:has-text('Blind test the top 3')")
        page.wait_for_selector("#ab-trial .ab-pair", timeout=20000)

        # Nothing on screen may say which side is which.
        trial = page.inner_text("#ab-trial")
        assert "bright-capture" not in trial and "dark-capture" not in trial, trial

        # Nor may the audio URLs, which end up in the DOM and in history.
        urls = page.eval_on_selector_all(
            "#ab-trial *", "nodes => nodes.map(n => n.outerHTML).join()"
        )
        assert "bright-capture" not in urls and "dark-capture" not in urls

        page.click("#ab-trial button:has-text('A is closer')")
        page.wait_for_function(
            "() => document.querySelector('#ab-trial').textContent.includes('1/1')"
            " || document.querySelector('#ab-standings').children.length > 0",
            timeout=10000,
        )


def test_the_offline_sweep_reports_its_own_failure_in_the_panel(
    browser, server, tmp_path
):
    """A sweep that renders nothing must say so, not spin forever.

    This drives the scan/run/watch path the page actually uses, which is the
    part no HTTP test can reach: the captures folder is scanned in the
    browser, the job is started from the button, and its state arrives over
    the live socket.
    """
    target = audio_file(tmp_path, "target.wav")
    di = audio_file(tmp_path, "di.wav")

    captures = tmp_path / "captures"
    captures.mkdir()
    # A .nam that parses well enough to be listed but cannot be rendered
    # through: the scan finds it, the sweep refuses it.
    import json

    (captures / "not-really.nam").write_text(
        json.dumps(
            {
                "version": "0.5.2",
                "architecture": "WaveNet",
                "config": {"layers_array": [{"channels": 16, "activation": "Tanh"}]},
                "weights": [0.1] * 64,
                "metadata": {"name": "not-really"},
                "sample_rate": 48000,
            }
        ),
        encoding="utf-8",
    )

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(target))
        page.fill("#search-di", str(di))
        page.fill("#search-captures", str(captures))

        page.click("#search-scan")
        page.wait_for_function(
            "() => document.querySelector('#search-found').textContent"
            ".includes('capture')",
            timeout=10000,
        )
        assert "1 capture" in page.inner_text("#search-found")
        assert not page.locator("#search-run").is_disabled()

        page.click("#search-run")
        page.wait_for_function(
            "() => document.querySelector('#search-progress').textContent"
            ".includes('failed')",
            timeout=30000,
        )
        assert "failed" in page.inner_text("#search-progress")


def test_the_blind_test_can_be_decided_from_the_keyboard(browser, server, tmp_path):
    """Hands off the mouse, eyes off the buttons - one less thing that is not
    the sound."""
    first = audio_file(tmp_path, "one.wav")
    second = audio_file(tmp_path, "two.wav")

    with page_at(browser, server) as page:
        page.evaluate(
            """async ([a, b]) => {
                await fetch('/api/ab', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        session_id: 'keys',
                        target_label: 'the record',
                        candidates: {one: a, two: b},
                    }),
                });
            }""",
            [str(first), str(second)],
        )
        page.reload(wait_until="networkidle")
        page.click("#tabs .tab[data-panel='match']")
        page.select_option("#ab-session", "keys")
        page.wait_for_selector("#ab-trial .ab-pair")
        page.wait_for_selector(".ab-side.playing", timeout=15000)

        # A and B move the audible side without moving the playhead.
        page.keyboard.press("b")
        page.wait_for_function(
            "() => document.querySelectorAll('.ab-side')[1]"
            ".classList.contains('playing')",
            timeout=5000,
        )
        page.keyboard.press("a")
        page.wait_for_function(
            "() => document.querySelectorAll('.ab-side')[0]"
            ".classList.contains('playing')",
            timeout=5000,
        )

        # 1 records "A is closer" and moves on.
        page.keyboard.press("1")
        page.wait_for_function(
            "() => document.querySelector('#ab-trial').textContent.includes('1/1')"
            " || document.querySelector('#ab-standings').children.length > 0",
            timeout=10000,
        )


def test_a_path_can_be_picked_instead_of_typed(browser, server, tmp_path):
    """A browser will not hand the page an absolute path, so the server does."""
    library = tmp_path / "library"
    library.mkdir()
    audio_file(library, "picked.wav")
    audio_file(library, "other.wav")

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")

        # A path already in the box is where the picker opens, so this starts
        # in the folder rather than walking the whole drive to reach it.
        page.fill("#target-path", str(library))
        page.click("#target-path + button")
        page.wait_for_selector("dialog.picker", state="visible")

        rows = page.locator("dialog.picker .picker-row.file")
        assert rows.count() == 2, "both takes should be listed"

        page.click("dialog.picker .picker-row.file:has-text('picked.wav')")
        page.click("dialog.picker .picker-ok")

        page.wait_for_function(
            "() => document.querySelector('#target-path').value.endsWith('picked.wav')",
            timeout=10000,
        )
        # Picking a target also opens it, so the waveform is there to drag on.
        page.wait_for_selector("#target-wave canvas", timeout=15000)


def test_cancelling_the_picker_leaves_the_path_alone(browser, server, tmp_path):
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#search-di", str(tmp_path))
        page.click("#search-di + button")
        page.wait_for_selector("dialog.picker", state="visible")
        page.click("dialog.picker .picker-cancel")
        page.wait_for_selector("dialog.picker", state="detached")
        assert page.input_value("#search-di") == str(tmp_path)


def test_the_device_panel_sends_midi_rather_than_describing_it(browser, server):
    """This panel used to print the CLI command you should go and run."""
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='device']")

        # Five footswitches, because there is no scene-select CC - a scene is
        # reached by pressing the switch it is assigned to.
        page.wait_for_selector("#midi-footswitches button")
        assert page.locator("#midi-footswitches button").count() == 5

        page.click("#midi-ports")
        page.wait_for_function(
            "() => document.querySelector('#midi-out').textContent.includes('outputs')",
            timeout=10000,
        )
        out = page.inner_text("#midi-out")
        assert "rigsmith device midi" not in out, "the panel should act, not instruct"

        # A bad program number is caught before anything is sent.
        page.fill("#midi-program", "999")
        page.click("#midi-select")
        page.wait_for_selector(".toast", timeout=10000)
        assert "0-127" in page.inner_text(".toasts")


def test_the_panels_hand_work_to_each_other(browser, server):
    """Six tabs is a set of tools; a thread between them is one tool.

    Compiling a recipe produces a rig, and the rig it produced is the one
    that should open - not whatever was last selected.
    """
    with page_at(browser, server) as page:
        page.evaluate(
            """async () => {
                await fetch('/api/recipes', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: 'Cherub Rock'}),
                });
                for (const gear of [
                    {role: 'fuzz', name: 'Big Muff', category: 'effect'},
                    {role: 'amp', name: 'Marshall JMP', category: 'nam'},
                ]) {
                    await fetch('/api/recipes/cherub-rock/gear', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify(gear),
                    });
                }
                // A second rig, so "the one it compiled" is not the only one.
                await fetch('/api/drafts', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'Something Else'}),
                });
            }"""
        )
        page.reload(wait_until="networkidle")
        page.click("#tabs .tab[data-panel='recipes']")

        # Wait for the row that has the button, not just for any row - the
        # fuzz renders first and carries no capture link.
        find_a_capture = page.locator("#recipe-gear button", has_text="find a capture")
        find_a_capture.wait_for(state="visible", timeout=15000)
        find_a_capture.click()
        page.wait_for_selector("#panels .panel[data-panel='captures']", state="visible")
        assert page.input_value("#t3k-query") == "Marshall JMP"

        page.click("#tabs .tab[data-panel='recipes']")
        # Coming back re-renders the recipe; compiling before that lands would
        # read a recipe id that is briefly absent.
        page.locator("#recipe-gear .item").first.wait_for(state="visible")
        page.click("#compile-recipe")

        open_in_builder = page.locator(
            "#compile-out button", has_text="Open in the rig builder"
        )
        open_in_builder.wait_for(state="visible", timeout=15000)
        open_in_builder.click()

        page.wait_for_selector("#panels .panel[data-panel='rig']", state="visible")
        page.wait_for_function(
            "() => document.querySelector('#rig-name')?.textContent === 'Cherub Rock'",
            timeout=10000,
        )
        assert "Big Muff" in page.inner_text("#board")

        # And the rig hands off to the thing that measures it.
        page.click("#test-btn")
        page.wait_for_selector("#panels .panel[data-panel='match']", state="visible")
        assert "Cherub Rock" in page.inner_text("#mode-note")


def test_a_measured_result_goes_back_onto_the_recipe(browser, server, tmp_path):
    """Research that has been checked, rather than only reasoned about."""
    target = audio_file(tmp_path, "stem.wav")
    candidate = audio_file(tmp_path, "jcm800.wav")

    with page_at(browser, server) as page:
        page.evaluate(
            """async () => {
                await fetch('/api/recipes', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title: 'Cherub Rock'}),
                });
            }"""
        )
        page.reload(wait_until="networkidle")
        page.click("#tabs .tab[data-panel='recipes']")
        page.wait_for_selector("#recipe-measured")
        assert "reasoned about, not checked" in page.inner_text("#recipe-measured")
        assert page.inner_text("#recipe-verified") == "unverified"

        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(target))
        page.fill("#candidate-paths", str(candidate))
        page.click("#score-btn")
        page.wait_for_selector("#ranking .item", timeout=20000)

        page.click("#ranking button:has-text('Save the winner to a recipe')")
        page.wait_for_selector("#modal[open]", timeout=10000)
        page.click("#modal-ok")

        page.click("#tabs .tab[data-panel='recipes']")
        page.wait_for_selector("#recipe-measured .item", timeout=10000)
        measured = page.inner_text("#recipe-measured")
        assert "jcm800" in measured
        assert "metric" in measured
        # A metric result is a ranking; it must not claim the recipe is settled.
        assert page.inner_text("#recipe-verified") == "unverified"


def test_the_panel_says_what_is_wrong_not_just_how_wrong(browser, server, tmp_path):
    """A ranking is a number. The bands are something you can act on."""
    sf = pytest.importorskip("soundfile")
    pytest.importorskip("scipy")
    import numpy as np
    from scipy.signal import butter, lfilter

    rate = 48000
    t = np.arange(3 * rate) / rate
    dark = np.tanh(np.sin(2 * np.pi * 196 * t) * 3)
    bright = dark + 1.5 * lfilter(*butter(2, 4000 / (rate / 2), btype="high"), dark)

    target = tmp_path / "dark.wav"
    candidate = tmp_path / "bright.wav"
    # Float WAVs, so the test measures the signals it wrote rather than what
    # PCM_16 clipped them into.
    sf.write(str(target), dark.astype("float32"), rate, subtype="FLOAT")
    sf.write(str(candidate), bright.astype("float32"), rate, subtype="FLOAT")

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(target))
        page.fill("#candidate-paths", str(candidate))
        page.click("#score-btn")

        page.wait_for_selector("#bands .band", timeout=30000)
        rows = page.locator("#bands .band")
        assert rows.count() >= 6, "every band should be shown, quiet ones dimmed"

        # The treble lift must be the thing it points at.
        fizz = page.locator("#bands .band", has_text="fizz")
        assert "+" in fizz.inner_text(), fizz.inner_text()
        assert "hot" in fizz.locator(".band-fill").get_attribute("class")

        low = page.locator("#bands .band", has_text="low end")
        assert "quiet" in (low.get_attribute("class") or "")


def test_writing_asks_before_it_writes_and_shows_what(browser, server):
    """The one irreversible thing this tool does, so the dialog has to carry
    the diff, the cost, and the backup gate - not just a yes/no."""
    with page_at(browser, server) as page:
        page.evaluate(
            """async () => {
                const created = await fetch('/api/drafts', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({name: 'Cherub Rock'}),
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
        page.wait_for_selector("#board .slot[data-category='nam']")

        page.click("#write-btn")
        page.wait_for_selector("#modal[open]", timeout=15000)

        body = page.inner_text("#modal-body")
        assert "JMP-1959" in body, "the diff has to be in the dialog"
        assert "Estimated CPU" in body
        assert "USB Transfer" in body, "the backup gate has to be explained"

        # The waiver starts off. Somebody has to choose it.
        assert page.locator("#f-no_backup").is_checked() is False

        # Cancelling writes nothing and leaves the rig alone.
        page.click("#modal-form button[value='cancel']")
        page.wait_for_selector("#modal[open]", state="detached", timeout=5000)
        assert "JMP-1959" in page.inner_text("#board")


def test_the_takes_panel_keeps_the_instrument_with_the_recording(
    browser, server, tmp_path
):
    """A folder of wav files is not a library; the labels are what make it one."""
    riff = audio_file(tmp_path, "cherub-riff.wav")

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='takes']")
        page.wait_for_selector("#takes-list")
        assert "Nothing here yet" in page.inner_text("#takes-list")

        page.fill("#take-path", str(riff))
        page.click("#take-add")
        page.wait_for_selector("#modal[open]", timeout=10000)

        page.fill("#f-guitar", "Les Paul Standard")
        page.fill("#f-pickup", "bridge")
        page.fill("#f-tuning", "E standard")
        page.click("#modal-ok")

        page.wait_for_selector("#takes-list .item", timeout=10000)
        listed = page.inner_text("#takes-list")
        assert "cherub-riff" in listed
        assert "Les Paul Standard bridge" in listed
        assert "no guitar" not in listed

        # A DI is the one kind that can be rendered through a candidate rig,
        # so it is the one that offers to be one.
        page.click("#takes-list button:has-text('use as DI')")
        page.wait_for_selector("#panels .panel[data-panel='match']", state="visible")
        assert page.input_value("#search-di") == "cherub-riff"


def test_a_take_with_no_guitar_is_called_out(browser, server, tmp_path):
    riff = audio_file(tmp_path, "unlabelled.wav")

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='takes']")
        page.fill("#take-path", str(riff))
        page.click("#take-add")
        page.wait_for_selector("#modal[open]", timeout=10000)
        page.click("#modal-ok")

        page.wait_for_selector("#takes-list .item", timeout=10000)
        assert "no guitar" in page.inner_text("#takes-list")
        assert "the reason this library is worth keeping" in page.inner_text(
            "#takes-summary"
        )


def test_the_panel_can_find_the_part_for_you(browser, server, tmp_path):
    """Scrubbing a waveform and reading a clock is a thing people do once."""
    sf = pytest.importorskip("soundfile")
    import numpy as np

    rate = 48000
    t = np.arange(rate * 30) / rate
    signal = np.zeros_like(t)
    signal[14 * rate : 24 * rate] = np.sin(2 * np.pi * 196 * t[: 10 * rate]) * 0.8
    path = tmp_path / "song.wav"
    sf.write(str(path), signal.astype("float32"), rate, subtype="FLOAT")

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(path))
        page.click("#open-target")
        page.wait_for_selector("#target-wave canvas", timeout=20000)
        assert page.inner_text("#target-region-label") == "whole file"

        page.click("#target-auto")
        page.wait_for_function(
            "() => document.querySelector('#target-region-label').textContent "
            "!== 'whole file'",
            timeout=15000,
        )
        label = page.inner_text("#target-region-label")
        # The riff runs 14s to 24s, and that is where it should have landed.
        start = float(label.split("s-")[0])
        assert 12.0 <= start <= 17.0, label


def test_the_waveform_says_what_the_file_is(browser, server, tmp_path):
    """Lossy and clipped both look like tone to every metric here, so they
    belong next to the audio rather than in a log somewhere."""
    sf = pytest.importorskip("soundfile")
    import numpy as np
    from rigsmith.match.source import readable_formats

    if "MP3" not in readable_formats():
        pytest.skip("this libsndfile has no mp3 encoder")

    rate = 48000
    t = np.arange(rate * 3) / rate
    clipped = np.clip(np.sin(2 * np.pi * 196 * t) * 4, -1, 1)
    path = tmp_path / "hot.mp3"
    sf.write(str(path), clipped.astype("float32"), rate, format="MP3")

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(path))
        page.click("#open-target")
        page.wait_for_selector("#target-wave canvas", timeout=20000)

        tags = page.inner_text("#target-wave-tags")
        assert "lossy" in tags
        assert "clipped" in tags
        assert "hot.mp3" in page.inner_text("#target-wave-name")


def test_a_clean_file_says_nothing_extra(browser, server, tmp_path):
    """Saying something has to mean something."""
    path = audio_file(tmp_path, "clean.wav")

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='match']")
        page.fill("#target-path", str(path))
        page.click("#open-target")
        page.wait_for_selector("#target-wave canvas", timeout=20000)
        assert page.inner_text("#target-wave-tags").strip() == ""


def test_the_takes_panel_offers_to_record(browser, server):
    """Three steps - record, find the file, add it - collapsed into one."""
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='takes']")
        page.click("#take-record")
        page.wait_for_selector("#modal[open]", timeout=10000)

        body = page.inner_text("#modal-body")
        assert "Play the part once" in body
        assert "dry DI" in body, "the two halves have to be explained"

        # Cancelling records nothing. Asserted as "no job row" rather than
        # "no text": the strip is also where a job's own progress lands, and
        # the claim here is that nothing was started.
        page.click("#modal-form button[value='cancel']")
        page.wait_for_selector("#modal[open]", state="detached", timeout=5000)
        assert page.locator("#take-record-progress .item").count() == 0


# --------------------------------------------------------------------------
# getting around, and the loop
# --------------------------------------------------------------------------


def test_ctrl_k_goes_to_a_page_by_name_or_asks(browser, server):
    with page_at(browser, server) as page:
        page.keyboard.press("Control+k")
        page.wait_for_selector(".palette input")
        page.keyboard.type("loop")
        first = page.locator(".palette-item").first
        assert "Loop" in first.inner_text()
        assert "Ask" in page.locator(".palette-item").last.inner_text()
        page.keyboard.press("Enter")
        page.wait_for_selector("#tabs .tab[data-panel='loop'][aria-selected='true']")
        assert page.locator(".palette-overlay").count() == 0
        # Escape closes it without going anywhere.
        page.keyboard.press("Control+k")
        page.wait_for_selector(".palette input")
        page.keyboard.press("Escape")
        assert page.locator(".palette-overlay").count() == 0


def test_every_loop_route_draws_its_wiring(browser, server):
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='loop']")
        page.click("text=Use the simulated bench")
        page.wait_for_selector(".route-card details.wiring-box")
        page.evaluate("document.querySelectorAll('details.wiring-box').forEach(d => d.open = true)")
        labels = page.evaluate("[...document.querySelectorAll('svg.wiring')].map(s => s.textContent)")
        assert len(labels) >= 3
        usb = next(text for text in labels if "USB Mode: Reamp" in text)
        assert "skip the rig" in usb and "USB out 3" in usb
        assert any('1/4" out L' in text and "one clock" in text for text in labels)


def test_a_new_workspace_is_shown_what_to_set_up(browser, server):
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='autopilot']")
        page.wait_for_selector(".setup-step")
        steps = page.locator(".setup-step").all_inner_texts()
        assert any("Teach it your guitar" in s for s in steps) and any("Pair with the Core" in s for s in steps)
        page.locator(".setup-step", has_text="Teach it your guitar").locator("button").click()
        page.wait_for_selector("#tabs .tab[data-panel='guitar'][aria-selected='true']")


@contextmanager
def streaming_model(words, delay=0.12):
    """A tiny OpenAI-compatible server that streams one reply word by word."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps({"data": [{"id": "qwen3-8b"}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for i, word in enumerate(words):
                chunk = {"choices": [{"delta": {"content": (" " if i else "") + word}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
                time.sleep(delay)
            self.wfile.write(b'data: {"choices": [{"delta": {}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n')
            self.wfile.flush()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        httpd.shutdown()


def test_a_local_model_reply_streams_into_the_chat_without_its_thinking(browser, server):
    import urllib.request

    words = "<think>hmm</think>The verse is a Plexi pushed hard with the mids up.".split(" ")
    with streaming_model(words) as base_url:
        request = urllib.request.Request(
            f"{server}/api/settings", method="PATCH",
            data=f'{{"mode": "local", "local_llm": {{"base_url": "{base_url}"}}}}'.encode(),
            headers={"content-type": "application/json"},
        )
        urllib.request.urlopen(request).read()
        with page_at(browser, server) as page:
            page.keyboard.press("Control+j")
            page.wait_for_selector("#chat-text")
            page.locator("#chat-text").fill("what amp is the verse?")
            page.locator("#chat-text").press("Enter")
            page.wait_for_selector("#chat-drawer .msg.assistant.live", timeout=10000)
            partial = page.locator("#chat-drawer .msg.assistant").last.inner_text()
            page.wait_for_selector("#chat-drawer .msg.assistant:not(.live)", timeout=15000)
            final = page.locator("#chat-drawer .msg.assistant").last.inner_text()
    assert final == "The verse is a Plexi pushed hard with the mids up."
    assert final.startswith(partial.strip()) and len(partial.strip()) < len(final)
    assert "hmm" not in partial and "think" not in partial


def test_a_rigs_tempo_is_typed_beside_its_signal_path(browser, server):
    from rigsmith.core import rigs

    draft_id = rigs.create("Tempo").draft.draft_id
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='rig']")
        page.wait_for_selector("#rig-tempo")
        page.select_option("#draft-select", draft_id)
        page.fill("#rig-tempo", "137.5")
        page.press("#rig-tempo", "Tab")
        deadline = time.monotonic() + 5
        while rigs.get(draft_id).rig.tempo != 137.5 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert rigs.get(draft_id).rig.tempo == 137.5


def test_a_songs_tempo_can_be_doubled_and_tapped(browser, server):
    """The detector's known mistake is an octave; the fix is a click."""
    from rigsmith.core import songs
    from rigsmith.core.songs import Part

    made = songs.create("Tempo")
    made.structure = {
        "tempo": 70.0, "tempo_confidence": 0.2, "key": "E minor", "duration": 30.0,
        "sections": [{"start": 0.0, "end": 30.0, "label": "A", "name": "verse"}],
    }
    made.parts = [Part("verse-1-stem", 0, "verse", "stem", 0.0, 30.0)]
    made.save()

    def tempo_now(test):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            tempo = songs.get(made.song_id).structure["tempo"]
            if test(tempo):
                return tempo
            time.sleep(0.1)
        return songs.get(made.song_id).structure["tempo"]

    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='songs']")
        page.wait_for_selector("#song-tempo")
        assert page.inner_text("#song-tempo").startswith("70 BPM")
        assert page.locator(".tempo-line .tag.warn").is_visible(), "a low-confidence tempo says so"
        page.click(".tempo-line button[title^='Double']")
        assert tempo_now(lambda t: t == 140) == 140
        page.wait_for_selector(".tempo-line .tag:not(.warn)")

        for _ in range(5):
            page.click("#tempo-tap")
            page.wait_for_timeout(500)
        tapped = tempo_now(lambda t: t != 140)
        assert 95 <= tapped <= 130, f"five taps half a second apart read {tapped}"


def test_the_tone_sheet_opens_as_its_own_page(browser, server):
    from rigsmith.core import rigs

    draft_id = rigs.create("Sheet").draft.draft_id
    rigs.set_tempo(draft_id, 120.0)
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='rig']")
        page.wait_for_selector("#sheet-btn")
        page.select_option("#draft-select", draft_id)
        with page.expect_popup() as opened:
            page.click("#sheet-btn")
        sheet = opened.value
        sheet.wait_for_load_state()
        assert sheet.locator("h1").inner_text() == "Sheet"
        assert "120 BPM, fixed" in sheet.inner_text("body")
        sheet.close()


def test_a_mapped_song_hands_its_chosen_parts_to_autopilot(browser, server, monkeypatch):
    """Songs -> Autopilot carries the song itself, not just its title."""
    from rigsmith.core import autopilot, songs
    from rigsmith.core.songs import Part

    made = songs.create("Handover", artist="Synthetic")
    made.structure = {
        "tempo": 120.0, "key": "E minor", "duration": 30.0,
        "sections": [{"start": 0.0, "end": 15.0, "label": "A", "name": "verse"}, {"start": 15.0, "end": 30.0, "label": "B", "name": "chorus"}],
    }
    made.parts = [
        Part("verse-1-left", 0, "verse", "left", 0.0, 15.0, chosen=True),
        Part("chorus-2-left", 1, "chorus", "left", 15.0, 30.0, chosen=True),
    ]
    made.save()

    started = []

    def start(request):
        started.append(request)
        run = autopilot.Run(run_id="handover-run", request=request, state="stopped", song_id=request.song_id)
        run.save()
        return run

    monkeypatch.setattr(autopilot, "start", start)
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='songs']")
        page.wait_for_selector("#song-tempo")
        page.click("section[data-panel='songs'] button:has-text('Autopilot')")
        page.wait_for_selector("#tabs .tab[data-panel='autopilot'][aria-selected='true']")
        assert "2 chosen parts" in page.inner_text("#ap-song-hint")
        page.click("#ap-form button:has-text('Start autopilot')")
        deadline = time.monotonic() + 5
        while not started and time.monotonic() < deadline:
            time.sleep(0.1)
    assert started, "the form never started a run"
    assert started[0].song_id == made.song_id and started[0].title == "Handover"


def test_the_device_page_says_what_it_knows_about_the_core(browser, server):
    with page_at(browser, server) as page:
        page.click("#tabs .tab[data-panel='device']")
        page.wait_for_selector("#catalog-status .item")
        status = page.inner_text("#catalog-status")
        assert "models" in status and "knob curves measured" in status
        assert page.is_visible("#catalog-calibrate") and page.is_visible("#device-scenes")

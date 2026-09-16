"""The vendored front-end libraries, loaded the way the page loads them.

The regions plugin extends the ``WaveSurfer`` global rather than defining its
own, so it only works if it runs after the core bundle. That dependency is
invisible in the markup - two script tags that look interchangeable - and
swapping them produces a page that loads without error and then throws the
first time anyone drags across a waveform. This runs the real files in the
real order and checks the result.
"""

from __future__ import annotations

import re
import shutil
import subprocess

import pytest

from rigsmith.web.server import static_dir

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed; this check is advisory"
)


def script_order() -> list[str]:
    """The vendor scripts, in the order index.html loads them."""
    page = (static_dir() / "index.html").read_text(encoding="utf-8")
    return re.findall(r'<script src="/static/vendor/([^"]+)"', page)


def test_the_page_loads_the_regions_plugin_after_wavesurfer():
    order = script_order()
    assert "wavesurfer.min.js" in order, "wavesurfer is not loaded by the page"
    assert "wavesurfer.regions.min.js" in order, "the regions plugin is not loaded"
    assert order.index("wavesurfer.min.js") < order.index(
        "wavesurfer.regions.min.js"
    ), "the plugin extends the WaveSurfer global, so it has to run second"


def test_the_plugin_really_lands_on_the_wavesurfer_global():
    """Load the two bundles in page order and ask for what the panel calls.

    Scoped to the wavesurfer pair because that is the one ordering that
    matters here; Sortable and uPlot stand alone, and reach far enough into
    the DOM at load that faking one would test the fake.
    """
    vendor = static_dir() / "vendor"
    order = [name for name in script_order() if name.startswith("wavesurfer")]
    loads = "\n".join(
        f"vm.runInContext(fs.readFileSync({str(vendor / name)!r}, 'utf8'), ctx);"
        for name in order
    )
    wanted = "[typeof WaveSurfer.create, typeof WaveSurfer.Regions.create]"
    program = (
        "const fs = require('fs'), vm = require('vm');\n"
        # A browser-shaped context: no `module` and no `exports`, so each UMD
        # takes its global branch the way it does in the page.
        "const ctx = vm.createContext({window: {}, self: {}, document: {}});\n"
        f"{loads}\n"
        f"console.log(vm.runInContext({wanted!r}, ctx).join(' '));"
    )
    result = subprocess.run(["node", "-e", program], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["function", "function"], result.stdout

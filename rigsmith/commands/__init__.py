"""Command groups too large to keep in ``cli.py``.

Each module defines one click group over one area of :mod:`rigsmith.core` and
is attached to the root command by :func:`register`. The rule is the same as
for ``cli.py``: parse, call core, print. Nothing here orchestrates.
"""

from __future__ import annotations

from contextlib import contextmanager

import click


@contextmanager
def progress_bar(label: str):
    """A click progress bar that core's ``Progress`` protocol can drive.

    Core reports fractions and messages; this turns them into a bar and,
    where a fraction is unknown, a line of text - so a slow step never looks
    the same as a hung one.
    """
    with click.progressbar(length=100, label=label) as bar:
        state = {"last": 0, "message": ""}

        class Reporter:
            def update(self, fraction=None, message=""):
                if fraction is not None:
                    step = int(fraction * 100) - state["last"]
                    if step > 0:
                        bar.update(step)
                        state["last"] += step
                if message and message != state["message"]:
                    state["message"] = message
                    bar.label = f"{label}: {message}"[:60]

            def log(self, message):
                pass

            @property
            def cancelled(self):
                return False

            def check(self):
                pass

        yield Reporter()


#: Modules under this package that each define a click ``group``.
MODULES = ("learn", "loop", "song", "tune", "settings")


def register(root: click.Group) -> None:
    from importlib import import_module

    for name in MODULES:
        module = import_module(f"{__name__}.{name}")
        root.add_command(module.group)

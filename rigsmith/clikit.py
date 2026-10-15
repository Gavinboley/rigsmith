"""Helpers every command module shares.

Separate from ``cli.py`` so the command groups in :mod:`rigsmith.commands`
can use them without importing the root command - which, under
``python -m rigsmith.cli``, is a second copy of the module and a circular
import.
"""

from __future__ import annotations

import asyncio
import functools
import json
import sys
from pathlib import Path

import click

from .core.errors import RigsmithError


def _run(coro):
    return asyncio.run(coro)


def handled(func):
    """Turn a core error into a styled message and the right exit code."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except RigsmithError as exc:
            click.echo(click.style(exc.message, fg="red"), err=True)
            if exc.hint:
                click.echo(exc.hint, err=True)
            sys.exit(exc.exit_code)

    return wrapper


def _audio_label(reference) -> str:
    """A short name for something that may be a path or a take id."""
    text = str(reference)
    stem = Path(text).stem
    return stem or text


def _emit(text: str, *, as_json: bool = False, payload=None) -> None:
    if as_json and payload is not None:
        click.echo(json.dumps(payload, indent=2, default=str))
    else:
        click.echo(text)

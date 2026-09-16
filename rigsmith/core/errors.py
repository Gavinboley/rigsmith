"""The exception hierarchy every surface translates from.

One base with a handful of meanings, so each adapter can map failures
mechanically instead of guessing:

==================  =====================  ==============  ==========
Core                MCP                    CLI             HTTP
==================  =====================  ==============  ==========
``NotFound``        ToolError              exit 1          404
``Invalid``         ToolError              exit 2          400
``Conflict``        ToolError              exit 1          409
``Unavailable``     ToolError              exit 1          503
``ReadOnly``        ToolError              exit 1          403
==================  =====================  ==============  ==========

Before this existed, the MCP layer caught a hand-written tuple of exception
types, and every domain error (``MidiUnavailable``, ``DeviceStorageError``,
``Tone3000Error``, …) was outside it. It worked only because each call site
remembered to catch its own errors inline - a convention, not a guarantee.
Now the domain exceptions inherit from these, so the mapping is structural.

Every error carries a ``hint``: the next thing the user could actually do.
"An error occurred" strands whoever reads it, human or agent.
"""

from __future__ import annotations


class RigsmithError(Exception):
    """Base for every expected failure.

    Args:
        message: What went wrong.
        hint: What to do about it. Shown to the user, so write it as advice.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n{self.hint}"
        return self.message

    @property
    def exit_code(self) -> int:
        return 1

    @property
    def http_status(self) -> int:
        return 500


class NotFound(RigsmithError):
    """A named thing does not exist - a draft, scene, block, session, file."""

    @property
    def http_status(self) -> int:
        return 404


class Invalid(RigsmithError):
    """The request is malformed or violates a rule of the domain."""

    @property
    def exit_code(self) -> int:
        return 2

    @property
    def http_status(self) -> int:
        return 400


class Conflict(RigsmithError):
    """The request collides with existing state - a taken slot, a used name."""

    @property
    def http_status(self) -> int:
        return 409


class Unavailable(RigsmithError):
    """Something external is missing: hardware, a device, an optional package.

    Distinct from ``Invalid`` because the request was reasonable - the
    environment could not satisfy it. These are the errors whose hint matters
    most, since it is usually a specific ``pip install`` or a cable.
    """

    @property
    def http_status(self) -> int:
        return 503


class ReadOnly(RigsmithError):
    """A write was attempted while rigsmith is in read-only mode."""

    @property
    def http_status(self) -> int:
        return 403


def missing_dependency(what: str, extra: str, *, why: str = "") -> Unavailable:
    """Build the standard 'optional package not installed' error.

    Args:
        what: Human name of the capability, e.g. "MIDI control".
        extra: The extras group that provides it, e.g. "midi".
        why: Optional extra context about what it unlocks.
    """
    hint = f"Install it with:  pip install 'rigsmith[{extra}]'"
    if why:
        hint += f"\n{why}"
    return Unavailable(f"{what} is not available", hint=hint)

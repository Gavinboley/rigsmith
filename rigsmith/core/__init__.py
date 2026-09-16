"""The service layer: one implementation, three surfaces.

Every operation rigsmith can perform lives here. The CLI, the MCP server, and
the web UI are thin adapters over these functions - they parse input, call
core, and format the result. None of them contain orchestration of their own.

That rule exists because the first version broke it: orchestration lived
inside the MCP tools, so anything not reachable from an MCP tool was
reachable from nowhere, and whole subsystems (MIDI, offline rendering, the
TONE3000 login flow) ended up built but unwired.

Core functions raise :mod:`rigsmith.core.errors` exceptions and return domain
objects. Adapters decide how to present both.
"""

from .errors import (
    Conflict,
    Invalid,
    NotFound,
    ReadOnly,
    RigsmithError,
    Unavailable,
)

__all__ = [
    "Conflict",
    "Invalid",
    "NotFound",
    "ReadOnly",
    "RigsmithError",
    "Unavailable",
]

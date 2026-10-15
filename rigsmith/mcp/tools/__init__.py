"""Tool modules, grouped by domain.

Importing this package registers every tool against the shared server. Each
module is a thin adapter over :mod:`rigsmith.core` - if a tool body grows
past formatting a result, the logic belongs in core so the CLI and web UI
get it too.
"""

from . import (  # noqa: F401
    autopilot,
    captures,
    device,
    learn,
    loop,
    matching,
    recipes,
    rigs,
    songs,
    takes,
    tuning,
    workspace,
)

__all__ = [
    "autopilot",
    "captures",
    "device",
    "learn",
    "loop",
    "matching",
    "recipes",
    "rigs",
    "songs",
    "takes",
    "tuning",
    "workspace",
]

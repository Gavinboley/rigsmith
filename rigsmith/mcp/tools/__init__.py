"""Tool modules, grouped by domain.

Importing this package registers every tool against the shared server. Each
module is a thin adapter over :mod:`rigsmith.core` - if a tool body grows
past formatting a result, the logic belongs in core so the CLI and web UI
get it too.
"""

from . import captures, device, matching, recipes, rigs, workspace  # noqa: F401

__all__ = [
    "captures",
    "device",
    "matching",
    "recipes",
    "rigs",
    "workspace",
]

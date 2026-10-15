"""HTTP routes for the subsystems added after ``api.py`` grew past a file.

Same rules as ``api.py``: translate JSON to a core call and the result back,
nothing more. Core errors carry their own status through the shared handler.
"""

from __future__ import annotations


def routers():
    from . import agents, autopilot, files, learn, loop, settings, songs, tuning

    return [module.router for module in (files, learn, loop, songs, tuning, agents, autopilot, settings)]

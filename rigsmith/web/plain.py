"""Plain JSON out of every endpoint, whatever the core hands back.

The analysis code works in numpy, and a ``numpy.bool`` or ``float32`` that
reaches a response is a 500 - FastAPI's serialiser does not know them. Rather
than trust every endpoint (and every job result) to convert, the routers use
:class:`PlainRoute`, which converts on the way out.
"""

from __future__ import annotations

import functools
import inspect
import math
from pathlib import PurePath
from typing import Any

from fastapi.routing import APIRoute


def plain(value: Any) -> Any:
    """``value`` with numpy scalars and arrays, paths and sets made JSON-native."""
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(item) for item in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, PurePath):
        return str(value)
    module = type(value).__module__
    if module == "numpy":
        if hasattr(value, "tolist"):
            return plain(value.tolist())
        if hasattr(value, "item"):
            return plain(value.item())
    return value


def _wrap(endpoint):
    if inspect.iscoroutinefunction(endpoint):

        @functools.wraps(endpoint)
        async def wrapped(*args, **kwargs):
            return plain(await endpoint(*args, **kwargs))

    else:

        @functools.wraps(endpoint)
        def wrapped(*args, **kwargs):
            return plain(endpoint(*args, **kwargs))

    return wrapped


class PlainRoute(APIRoute):
    """An :class:`APIRoute` whose endpoint's result goes through :func:`plain`.

    Responses an endpoint builds itself (files, streams) pass untouched.
    """

    def __init__(self, path: str, endpoint, **kwargs) -> None:
        super().__init__(path, _wrap(endpoint), **kwargs)

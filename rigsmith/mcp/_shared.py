"""The MCP server object and the error surface every tool module shares.

Kept separate from ``server.py`` so the tool modules can import the server to
register against it without importing the assembly point, which would be
circular.
"""

from __future__ import annotations

import functools
import inspect

from .. import __version__
from ..core.errors import RigsmithError

INSTRUCTIONS = """\
rigsmith drives a HeadRush Core amp modeler over the local network so you can
recreate specific recorded guitar tones.

Typical flow for "make me the tone from <song>":

 1. Research the gear yourself (web search, interviews, rig rundowns). rigsmith
    does not browse for you - it gives you somewhere to put the conclusion.
 2. search_captures  - find a NAM capture of the amp. The Core runs A2 Full
    and A2 Lite plus all A1 sizes; A2 is preferred.
 3. create_rig       - start a draft.
 4. add_block        - build the chain: drives, amp or NAM, cab or IR, time FX.
 5. configure_scene  - map verse/chorus/lead to footswitches 1-5.
 6. validate_rig     - catch slot, CPU, and scene problems before writing.
 7. preview_changes  - see exactly what will change on the hardware.
 8. write_rig        - takes a backup, then writes.

To test a tone against the source recording:

 - isolate_guitar     pull a guitar stem out of the record
 - search_offline     render a DI through many captures and rank them, no
                      hardware needed
 - capture_di         record one performance; its dry DI reamps through
                      unlimited candidates afterwards
 - start_ab_test      blind listening, which is what actually decides

Three things to keep in mind:

* The model catalog may be unverified until it has been extracted from a real
  device. When it is, model ids are suggestions, not guarantees - say so.
* Score against a record with mode='distribution'. Sample-wise metrics like
  ESR are only meaningful when both signals are the same performance; against
  a record they are artefacts of an arbitrary alignment.
* A rig that scores well is not necessarily the right tone. Offer the
  listening test rather than declaring victory from a number.
"""


def _build_server():
    """Build the server object, supporting both MCP SDK generations."""
    try:  # MCP SDK v2
        from mcp.server.mcpserver import MCPServer

        return MCPServer(
            name="rigsmith", version=__version__, instructions=INSTRUCTIONS
        )
    except ImportError:
        try:  # MCP SDK v1
            from mcp.server.fastmcp import FastMCP

            return FastMCP("rigsmith", instructions=INSTRUCTIONS)
        except ImportError as exc:  # pragma: no cover - dependency missing
            raise SystemExit(
                "The MCP SDK is not installed. Run:  pip install 'rigsmith[mcp]'"
            ) from exc


mcp = _build_server()

try:  # MCP SDK v2 - the only exception type whose message reaches the agent
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # pragma: no cover - v1 or a future rename

    class ToolError(RuntimeError):  # type: ignore[no-redef]
        """Fallback when the SDK does not provide one."""


def surface_errors(func):
    """Re-raise expected failures as ToolError so the agent sees the reason.

    Anything else raised inside a tool is reported by the SDK as a bare
    "Error executing tool <name>", which tells the agent nothing it can act
    on. "slot 2 is already occupied. Free slots: [6, 7, 8]" is a correction
    it can use; "error" is a dead end.

    ``RigsmithError`` covers the whole domain - including the hardware and
    dependency errors that used to sit outside the caught set and only
    reached the agent intact because each call site remembered to catch them
    inline. ``TypeError`` is included because an MCP client can still send a
    badly shaped argument past the JSON schema.
    """
    expected = (RigsmithError, ValueError, TypeError, KeyError, LookupError, OSError)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except ToolError:
                raise
            except expected as exc:
                raise ToolError(str(exc)) from exc

        return async_wrapper

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ToolError:
            raise
        except expected as exc:
            raise ToolError(str(exc)) from exc

    return wrapper


def tool(func=None, **kwargs):
    """Register a tool with error surfacing already applied.

    Using this instead of bare ``@mcp.tool()`` makes it impossible to add a
    tool that strands the agent on failure - which was previously a
    convention maintained by hand across 1,185 lines.
    """
    def decorate(f):
        return mcp.tool(**kwargs)(surface_errors(f))

    return decorate(func) if func is not None else decorate

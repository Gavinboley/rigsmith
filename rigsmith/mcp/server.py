"""The rigsmith MCP server.

Assembly only: build the server object, pull in the tool modules for their
registration side effects, and run. Everything it exposes lives in
:mod:`rigsmith.mcp.tools`, and everything those do lives in
:mod:`rigsmith.core`.
"""

from __future__ import annotations

from . import tools  # noqa: F401  - importing registers every tool
from ._shared import INSTRUCTIONS, ToolError, mcp, surface_errors, tool

__all__ = ["mcp", "tool", "surface_errors", "ToolError", "INSTRUCTIONS", "main"]


def main() -> None:
    """Run the server over stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

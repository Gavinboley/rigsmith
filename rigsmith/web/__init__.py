"""The local web interface.

A single-page app served from ``rigsmith ui``, bound to loopback. Like the
CLI and the MCP server, it is a thin adapter over :mod:`rigsmith.core` - the
API layer translates core results to JSON and core errors to HTTP statuses,
and does no orchestration of its own.
"""

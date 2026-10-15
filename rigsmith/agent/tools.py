"""The tools agents can call - read from the MCP server, not re-declared.

Declaring tools twice (once for MCP, once for the in-app agents) is how the
two lists drift: a tool added to one surface and not the other, a parameter
renamed in one place. So the registry here *is* the MCP server's: names,
descriptions and JSON schemas come from it, calls go through it in-process,
and each tool's group is the module it was defined in.

Two adjustments are made for local models, and only for them:

* schemas are flattened - ``anyOf: [X, null]`` becomes ``X``, titles go - which
  roughly halves their size and avoids the ``anyOf`` that some local servers
  are reported to drop silently;
* descriptions are cut to their first paragraph.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

#: Tools that change the device or delete work. An agent may propose them;
#: a person approves each call before it runs.
NEEDS_APPROVAL = {
    "write_rig",
    "delete_draft",
    "delete_take",
    "delete_recipe",
    "install_capture",
    "workspace_prune",
    "midi_select_rig",
    "midi_set_block",
    "adopt_loop_route",
    "apply_tuning",
}

#: Tools that reach the internet - refused in local mode before they run.
ONLINE = {
    "search_captures",
    "download_capture",
    "search_song",
}


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict
    group: str
    needs_approval: bool = False
    online: bool = False

    def openai(self, *, compact: bool = False) -> dict:
        description = self.description
        schema = self.schema
        if compact:
            description = description.strip().split("\n\n", 1)[0][:400]
            schema = flatten(schema)
        return {"type": "function", "function": {"name": self.name, "description": description, "parameters": schema}}

    def anthropic(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.schema}


def flatten(schema):
    """Drop titles and collapse nullable ``anyOf`` - for small local models."""
    if isinstance(schema, dict):
        if "anyOf" in schema:
            options = [o for o in schema["anyOf"] if o.get("type") != "null"]
            if len(options) == 1:
                merged = {k: v for k, v in schema.items() if k != "anyOf"}
                merged.update(options[0])
                return flatten(merged)
        return {k: flatten(v) for k, v in schema.items() if k != "title"}
    if isinstance(schema, list):
        return [flatten(item) for item in schema]
    return schema


_registry: dict[str, ToolSpec] | None = None


def registry() -> dict[str, ToolSpec]:
    """Every tool, by name."""
    global _registry
    if _registry is None:
        from ..mcp import server

        tools = asyncio.run(server.mcp.list_tools()) if not _loop_running() else _list_in_thread(server)
        managed = server.mcp._tool_manager._tools
        found = {}
        for tool in tools:
            function = getattr(managed.get(tool.name), "fn", None)
            module = getattr(function, "__module__", "") or ""
            group = module.rsplit(".", 1)[-1] if module.startswith("rigsmith.mcp.tools") else "other"
            found[tool.name] = ToolSpec(
                name=tool.name,
                description=tool.description or "",
                schema=dict(getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}),
                group=group,
                needs_approval=tool.name in NEEDS_APPROVAL,
                online=tool.name in ONLINE,
            )
        _registry = found
    return _registry


def _loop_running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _list_in_thread(server):
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(server.mcp.list_tools())).result()


def groups() -> dict[str, list[str]]:
    by_group: dict[str, list[str]] = {}
    for spec in registry().values():
        by_group.setdefault(spec.group, []).append(spec.name)
    return {k: sorted(v) for k, v in sorted(by_group.items())}


def select(names: list[str] | None = None, *, groups_wanted: list[str] | None = None) -> list[ToolSpec]:
    """Tools by name and/or group, in a stable order."""
    reg = registry()
    chosen: dict[str, ToolSpec] = {}
    for group in groups_wanted or []:
        for spec in reg.values():
            if spec.group == group:
                chosen[spec.name] = spec
    for name in names or []:
        if name in reg:
            chosen[name] = reg[name]
    return [chosen[k] for k in sorted(chosen)]


@dataclass
class ToolOutcome:
    name: str
    arguments: dict
    ok: bool
    text: str
    refused: bool = False
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "arguments": self.arguments, "ok": self.ok, "text": self.text, "refused": self.refused}


def call(name: str, arguments: dict | None = None, *, max_chars: int = 12000) -> ToolOutcome:
    """Run one tool in-process, the way the MCP server would."""
    from ..core import appsettings
    from ..mcp import server

    arguments = dict(arguments or {})
    spec = registry().get(name)
    if spec is None:
        known = ", ".join(sorted(registry())[:40])
        return ToolOutcome(name, arguments, False, f"there is no tool called '{name}'. Some that exist: {known}")
    if spec.online and appsettings.load().local:
        return ToolOutcome(name, arguments, False, f"'{name}' reaches the internet, and rigsmith is in local mode.", refused=True)

    def run():
        return asyncio.run(server.mcp.call_tool(name, arguments))

    try:
        if _loop_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor(1) as pool:
                result = pool.submit(run).result()
        else:
            result = run()
    except Exception as exc:  # the SDK raises for a tool error; that is a result, not a crash
        return ToolOutcome(name, arguments, False, f"{type(exc).__name__}: {exc}")

    texts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            texts.append(text)
    text = "\n".join(texts) or json.dumps(getattr(result, "structured_content", None) or {}, default=str)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [{len(text) - max_chars} more characters cut]"
    return ToolOutcome(name, arguments, not bool(getattr(result, "is_error", False)), text)


def reset() -> None:
    global _registry
    _registry = None

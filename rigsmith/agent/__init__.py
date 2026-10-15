"""Agents: the chat in the app, and the workers it sends off.

Everything rigsmith can do is already a tool - the same functions the MCP
server exposes to Claude Code. This package puts a model in front of them,
inside the app:

* :mod:`.tools` - the tool registry, read straight from the MCP server so
  the two can never disagree, grouped by what they work on.
* :mod:`.roles` - the agents: a router that talks to you, and specialists
  (researcher, song mapper, luthier, tone matcher, rig builder) each with only
  the tools its job needs. A small local model given 100 tool schemas spends
  its context on schemas; given 15, it gets the job done.
* :mod:`.providers` - who answers. In accuracy mode, Claude through Claude
  Code (with rigsmith's MCP server and web search) or the Anthropic API; in
  local mode, whatever OpenAI-compatible server is running on this machine -
  LM Studio, Ollama, llama.cpp - with Qwen's tool-call formats handled.
* :mod:`.runner` - the loop: ask the model, run the tools it calls, stop for
  a person's approval before anything touches the device, repeat.
"""

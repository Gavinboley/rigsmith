# Running rigsmith's agents on a local model

In **local mode** nothing leaves your machine: the chat and every agent run on
an OpenAI-compatible server you run yourself. This page says which servers
work, which models to load, and what to expect from them.

Researched October 2026. Model families move every few weeks; where a claim
could not be confirmed it is marked.

## Servers

| Server | Default endpoint | Notes |
|---|---|---|
| LM Studio | `http://127.0.0.1:1234/v1` | Start the server from the Developer tab (or `lms server start`). The default in rigsmith. |
| Ollama | `http://127.0.0.1:11434/v1` | OpenAI-compatible endpoint alongside its own `/api/chat`. |
| llama.cpp | `http://127.0.0.1:8080/v1` | Start `llama-server` with `--jinja`, or tool calls are not parsed at all. |

```bash
rigsmith settings mode local
rigsmith settings llm --local --provider openai-compatible --url http://127.0.0.1:1234/v1
rigsmith agent status          # is it answering, and what is loaded
```

Leave the model blank and rigsmith asks the server what is loaded and picks
the best chat model it recognises (embedding models are skipped).

## Which model

Agents call tools, so what matters is reliable function calling over
several rounds, not raw chat quality. Small models choose less reliably as
the list grows, so a local model is handed 18-22 tools per role - the
everyday ones - where Claude gets every one (up to 32); the rest stay
reachable through the specialists the assistant dispatches.

| VRAM | Load | Why |
|---|---|---|
| 20-24 GB | **Qwen3-30B-A3B-Instruct-2507** (Q4_K_M or similar, ~18-20 GB) | A mixture-of-experts with ~3B active parameters: fast, and explicitly tuned for tool use. Third-party BFCL v3 puts it around 65 (instruct) to 72 (thinking). |
| 20-24 GB, alternative | **Qwen3.8-27B** (dense, ~17 GB at Q4_K_M) | Open weights since August 2026 (Apache 2.0, 262K context), first in rigsmith's auto-pick order. Only community GGUFs exist; keep the embedded chat template when a runtime offers to replace it, since tool calls depend on it. Its calls use the XML form, which rigsmith parses. Thinking is on by default; rigsmith turns it off for agents. No tool-calling benchmark could be confirmed. |
| 12-16 GB | **Qwen3-8B** (Q4_K_M, ~5 GB) | Dense, trained with agents in mind, leaves room for context. The 30B-A3B also works here with partial CPU offload, slower. |
| 8 GB or less | Qwen3-4B-Instruct-2507 | Works for single tool calls; multi-turn tool use is weak (BFCL multi-turn around 22%). Expect retries. |

Below 4B, multi-step tool use is not reliable enough to be worth it.

Benchmarks say how a model does on somebody else's tools. To see how the one
you have loaded does on rigsmith's, press **Test it on rigsmith's tools** in
Settings (or `rigsmith settings check-model`): six everyday requests, each a
fresh conversation with the assistant's real tools and system prompt, scored
on whether the first reply calls the right tool with the arguments the
request named. Nothing is run or changed, so it is safe on any workspace;
on a 24 GB card it takes about a minute.

The 3.7 / 3.8 flagship and Omni-Flash models are API-only or far too large for
a desktop; they are not options for local mode. Neither is
**Qwen3.8-Flash-Next** (open weights since 26 August 2026, an early look at
the Qwen 4 architecture), despite only 6B active parameters: it is 180B in
total, about 100 GB even at 4 bits, under its own qwen-community-1.0
license, and its card names no tool-call format - a 128 GB workstation
might hold it, a desktop GPU will not. Qwen 4 (including a 27B open-weights
tier) was announced in September 2026 but, as of 20 October, had not
shipped and had no date; when it does and is loaded, the auto-pick puts it
ahead of any older Qwen on the same server.

## What rigsmith does about Qwen's quirks

- **Two tool-call formats.** Up to Qwen3 the model writes Hermes-style
  `<tool_call>{json}</tool_call>`; Qwen3-Coder and Qwen3.5 onward use an XML
  form, `<tool_call><function=name><parameter=x>...`. A server whose parser
  does not match the model returns the call as plain text. rigsmith parses
  both (and a fenced JSON call) out of the text, and only runs names that are
  real tools.
- **Thinking.** `<think>...</think>` is stripped from answers and shown as
  reasoning. Thinking is off by default for agents (`/no_think`, and
  `enable_thinking: false` where the server honours it); turn it on with
  `rigsmith settings llm --local --thinking` for harder tasks at the cost of
  speed. With thinking on, the reasoning is carried into the next turn, as
  Qwen recommends for multi-turn tool use.
- **Schemas.** Local models get compacted tool schemas - nullable `anyOf`
  collapsed, titles dropped, descriptions cut to a paragraph - about half the
  size, and without the `anyOf` some servers are reported to drop.
- **Small toolsets.** Each agent role sees only its own tools (14-26), not all
  100+, so a small model's context goes on the task rather than on schemas.
- **Streaming.** Replies are requested as server-sent events and shown as they
  are written, so a model that takes a minute does not look hung. The live
  text hides `<think>` (finished, unfinished, or opened by the chat template)
  and anything from a tool call written as text onwards, including one half
  written at the end; tool calls that arrive in pieces are reassembled by
  index. A server that answers in one piece still works.
- **Finding the server.** Status checks open a bare socket with a short
  timeout first: Windows retries a refused connection to localhost for about
  two seconds, which made every page that asked "is a model running?" stall
  when none was.

## What local mode cannot do

- **Research from sources.** With no web, a local model can only use what it
  learned in training, and it will happily invent a pedal setting. rigsmith's
  recipes refuse a "stated" or "reported" claim without a source, so whatever
  a local model contributes lands as *inferred* or *guessed* - visible as such.
- **TONE3000 and song lookup** refuse, by design.

## Not verified

- Exact Ollama tags for the `-2507` models (check `ollama.com/library/qwen3`).
- Whether parallel tool calls work for Qwen on llama.cpp or Ollama; rigsmith
  handles them if they arrive, and does not depend on them.
- Local audio-capable Qwen models (Qwen3-Omni-30B-A3B and its Captioner) run
  through `transformers`/vLLM; llama.cpp support was experimental as of April
  2026, and no LM Studio or Ollama packaging was found. rigsmith does not
  depend on them.

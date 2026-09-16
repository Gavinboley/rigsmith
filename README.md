# rigsmith

**Recreate a recorded guitar tone on a HeadRush Core.** Work out what gear made it, find a NAM capture that matches, build the rig over local Wi-Fi, and measure how close you actually got.

Three ways in, all over the same core: a **web UI**, a **CLI**, and an **MCP server** so an AI agent can do the slow parts for you.

The web UI has six panels: **Research** (the recipe, with every claim's source and confidence), **Rig** (a 14-slot pedalboard you drag), **Captures**, **Match** (waveforms and blind A/B), **Device**, and **Workspace**. They hand work to each other: an unresolved amp goes looking for a capture, a compiled recipe opens as a rig, a rig goes off to be measured.

```bash
pip install -e ".[all]"
rigsmith ui
```

> **Status: works without hardware; one thing needs it.** Rig authoring, validation, NAM analysis, tone matching, catalog extraction, USB backup, MIDI and the web UI are all done and covered by 489 tests. The Remote Editor's wire protocol still has to be captured against a powered-on Core before `write_rig` can write — everything else runs today. See [docs/PROTOCOL.md](docs/PROTOCOL.md).

---

## Why this exists

HeadRush firmware 4.0+ makes the Core serve a **Remote Editor** web app over Wi-Fi, and 5.1 added **native NAM A1/A2 support with TONE3000 integration**. So the device already has a network control surface, and already runs the open-source capture format that makes accurate amp recreation possible. Nothing was missing except something to drive it.

As far as public sources show, nothing like this exists for HeadRush — no MCP server, no published API, no documented `.rig` format, no third-party editor for the Prime/Core generation. The equivalents exist for Line 6 and Fractal gear; HeadRush has none.

## Safety

This project does **not** modify, patch, or reflash device firmware, and it never will.

- Device writes go through the same interfaces the official Remote Editor uses.
- Every write is gated: **validate → backup → diff → confirm → write → verify**.
- `RIGSMITH_READONLY=1` disables every write at startup.
- The web UI binds to **loopback only** and warns loudly if you override that — it can control an amp modeler on your network, and exposing it to that network would be a real footgun.
- Firmware analysis is read-only static inspection for interoperability. No vendor firmware, binaries, or assets are committed here — only derived documentation.
- If something goes wrong on the device, holding footswitches **4+5** at power-on restores factory rigs.

## Quickstart

```bash
rigsmith doctor        # what this install can and cannot do, and how to fix it
rigsmith ui            # the web interface
```

Put the Core on the same Wi-Fi network, enable **Global Settings → Wi-Fi → HeadRush Remote**, then:

```bash
rigsmith discover              # find it
rigsmith probe --dump-app      # map the editor's API surface
rigsmith protocol record       # watch the real editor work, and log every call
```

### No hardware yet?

```bash
rigsmith protocol simulate --port 8100
rigsmith --device 127.0.0.1:8100 discover
rigsmith --device 127.0.0.1:8100 probe --dump-app
rigsmith catalog extract --capture ~/.rigsmith/captures/<timestamp>
```

A stand-in device, so the whole device-facing toolchain is exercisable
today. **It is not the real protocol** - nobody has captured that yet, and
every response it gives is invented. What it is good for is developing
against: discovery, endpoint extraction, catalog extraction and traffic
recording all run end to end on it. Once real traffic *is* captured,
`--replay` serves that instead, and the simulator becomes a faithful fixture
rather than a guess.

`rigsmith rig write` really does write, too - the client is driven by a
**protocol map**, which expresses one device and firmware's API as data
(`rigsmith protocol maps`). That is what let the whole write path be finished
before the real protocol is known: when it is captured, it drops in as
another JSON file and nothing about the client changes. Unverified maps are
labelled everywhere they are used, including in the write report, so a guess
can never quietly pass for knowledge.

It has already paid for itself once: its bundle builds its WebSocket URL by
concatenation and names message types in comparisons, and the extractor
caught neither. Both are what real minified bundles do.

If mDNS is blocked, double-tap "HeadRush Remote" on the device to read its IP and pass `--device 192.168.1.42`.

### Research a tone

A **recipe** is where the research goes before any rig exists: what gear the
tone used, where each claim came from, and how confident it is.

```bash
rigsmith recipe new "Cherub Rock" --artist "Smashing Pumpkins" --year 1993   --summary "Saturated but articulate. The fuzz is doing the work, not the amp gain."

rigsmith recipe gear cherub-rock fuzz "Big Muff Pi" --confidence stated   --source "Siamese Dream liner notes" --source-note "Listed in the equipment credits."

rigsmith recipe gear cherub-rock amp "Marshall JMP" --category nam --confidence reported   --source "Guitar World interview" --alt "Marshall JCM800"

rigsmith recipe gear cherub-rock cab "1960A 4x12" --category ir --confidence inferred

rigsmith recipe scene cherub-rock "Clean verse" --off fuzz --fs 1
rigsmith recipe question cherub-rock "Which Big Muff revision?"

rigsmith recipe compile cherub-rock      # -> a draft rig

# ...then, after testing it, the half research alone never closes:
rigsmith recipe measured cherub-rock "jcm800-hot" stem.wav --composite 0.031 --won
rigsmith recipe measured cherub-rock "jcm800-hot" stem.wav --kind listen --won --trials 3
```

A recipe also records what happened when you tested it. Confidence says how well-sourced a claim is; a measurement says whether acting on it worked. Only a blind listening verdict marks a recipe **verified** — a metric result is a ranking, not a decision.

`stated` and `reported` are **refused without a source**. That is the point:
an undocumented claim dressed as a documented one is worse than an honest
guess, and this is the one field that lets a reader tell your findings from
your reasoning. Gear compiles into conventional chain order regardless of the
order you researched it in, and scenes reference gear by role, so they resolve
to whatever slots the gear lands in.

### Build a rig

```bash
rigsmith rig new "Cherub Rock" --notes "Siamese Dream"
rigsmith rig add cherub-rock effect "Big Muff"      --param drive=0.72
rigsmith rig add cherub-rock nam    "Marshall JMP"  --param size='"A2 Lite"'
rigsmith rig add cherub-rock ir     "1960A-SM57"    --param length='"1024"'
rigsmith rig add cherub-rock effect "Analog Delay"  --param mix=0.18

rigsmith rig scene cherub-rock "Clean verse" --off 1 --off 4 --fs 1
rigsmith rig scene cherub-rock "Big chorus"  --on 1 --on 4 --fs 2

rigsmith rig validate cherub-rock
rigsmith rig preview  cherub-rock
```

### Match it against the record

```bash
rigsmith tone3000 search "JCM800" --architecture 2
rigsmith tone3000 download 4821 --install         # fetch, verify, copy to the device

rigsmith match isolate song.wav                   # guitar stem via Demucs
rigsmith match capture-di --seconds 20            # play the riff once
rigsmith match search di.wav stem.wav ./captures  # render every capture, rank them
rigsmith match score stem.wav a.wav b.wav --region 41.2:58.0   # just the solo
rigsmith match explain stem.wav a.wav --region 41.2:58.0   # what is still wrong
rigsmith match ab start s1 "Cherub Rock" a.wav b.wav c.wav
```

`--region` matters more than it looks. A track is not one tone — the clean intro, the rhythm verse and the solo are often three different amps — and scoring the whole file averages them into something no capture actually matches. In the **Match** panel you drag across the waveform instead of counting seconds.

Then the same panel runs the blind listening test — both clips are decoded up front and switching is a gain crossfade, so you compare the same instant of each rather than a reload.

### Use it from Claude Code

```bash
claude mcp add rigsmith -- rigsmith serve
```

Ask for a tone. The agent researches the gear, finds an A2 capture, builds the rig with scenes on footswitches, validates it, and shows a diff before anything is written.

## How it's put together

```
              rigsmith/core/          one implementation
                    |
    +---------------+---------------+
    |               |               |
  cli.py       mcp/tools/*      web/api.py
  (click)       (65 tools)       (FastAPI)
```

Each surface is a thin adapter: parse input, call core, format output. That rule exists because the first version broke it — orchestration lived inside the MCP tools, so anything not reachable from a tool was reachable from nowhere, and whole subsystems ended up built and unwired.

Transports to the device:

| Layer | Mechanism | State |
|---|---|---|
| **A** | Native HTTP/WS client for the Remote Editor | Built and driven by a protocol map. Works against the simulator; needs a captured map to reach real hardware |
| **B** | Playwright driving the real editor UI | **Designed, not built.** A fallback that cannot drift from what the device accepts — but it needs the same powered-on Core that capturing the protocol needs, so it unblocks nothing that A does not |
| **C** | USB mass storage | Built. Backup/restore, `.nam` and IR delivery |
| **D** | MIDI PC/CC | Built. Rig and scene switching, tap tempo, and verification independent of whatever performed the write |

We never need to reverse-engineer the `.rig` binary format: the Remote Editor can create rigs, so the device writes its own files.

## Tone testing

Three layers, usable independently:

1. **Offline** — render a DI through a NAM capture and cab IR in Python. No hardware, fast, approximate. `rigsmith match search` does the whole sweep and hands back a ranking.
2. **Hardware** — the Core's USB interface is 6-in/4-out with a dry DI on channel 5. Play a riff *once*, then reamp that exact performance through unlimited candidates.
3. **Blind A/B** — the arbiter. Identities hidden until scored.

**On metric honesty.** A commercial recording is not a DI and is not the same performance. Sample-aligned ESR is only valid comparing *your* DI through the Core against *your* reference; against a separated stem, only distribution-level metrics apply, as a ranking. rigsmith enforces that rather than reporting a confident number it cannot justify — see [docs/TONE_MATCHING.md](docs/TONE_MATCHING.md), which opens with the measurement that makes the point.

## Install

```bash
pip install -e ".[all]"     # everything, including the UI and test deps
pip install -e .            # core only: discovery, protocol capture, rig authoring
```

Extras: `ui`, `mcp`, `midi`, `audio`, `match`, `separation`, `browser`, `dev`. `rigsmith doctor` tells you which you're missing and why you'd want each.

## Documentation

- [docs/PROTOCOL.md](docs/PROTOCOL.md) — what is known about the Remote Editor protocol, what is not, and how it gets captured
- [docs/TONE_MATCHING.md](docs/TONE_MATCHING.md) — how tone comparison works and where the numbers stop being trustworthy

## What's left

- [ ] **Capture the Remote Editor protocol against hardware** — the one remaining blocker
- [x] ~~Native device client and the real `write_rig`~~ - built and tested against the simulator; needs a verified protocol map to reach real hardware
- [ ] Extract the full model catalog from a real device (`rigsmith catalog extract` works; it needs a bundle to read)

## License

MIT — see [LICENSE](LICENSE). Vendored front-end libraries keep their own licences, recorded in [`rigsmith/web/static/vendor/README.md`](rigsmith/web/static/vendor/README.md).

Not affiliated with or endorsed by HeadRush, inMusic Brands, or TONE3000. "HeadRush" and "Core" are trademarks of their respective owners.

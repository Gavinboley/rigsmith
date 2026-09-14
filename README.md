# acro

**AI tone authoring for the HeadRush Core.** Research a song's guitar tone, build the matching rig on the device over local Wi-Fi, and test the result against the source recording.

`acro` is an [MCP](https://modelcontextprotocol.io) server plus a CLI. It lets an AI agent do the slow parts of tone recreation — working out what amp, pedals, and cab were actually used on a record, finding a NAM capture that matches, assembling a 14-block rig with Scenes, and then measuring how close the result really is.

> **Status: early.** Device discovery, protocol capture, and the CLI shell are working. The rig authoring, NAM, and tone-matching layers are in progress. See [Roadmap](#roadmap).

---

## Why this exists

HeadRush firmware 4.0+ makes the Core serve a **Remote Editor** web app over Wi-Fi, and firmware 5.1 added **native NAM A1/A2 support with TONE3000 integration**. So the device already has a network control surface, and already runs the open-source capture format that makes accurate amp recreation possible. Nothing was missing except something to drive it.

As far as public sources show, nothing like this exists for HeadRush — no MCP server, no published API, no documented `.rig` format, no third-party editor for the Prime/Core generation. The equivalent tools exist for Line 6 and Fractal gear; HeadRush has none.

## Safety

This project does **not** modify, patch, or reflash device firmware, and it never will.

- Device writes go through the same interfaces the official Remote Editor uses.
- Every write is gated: **validate → backup → diff → confirm → write → verify**.
- `ACRO_READONLY=1` disables all write tools at startup.
- Firmware analysis, where used, is read-only static inspection for interoperability. No vendor firmware, binaries, or bundled assets are committed to this repo — only derived documentation.
- If something does go wrong on the device, holding footswitches **4+5** at power-on restores factory rigs.

## Install

```bash
git clone https://github.com/Gavinboley/acro-plan
cd acro-plan
pip install -e ".[all]"
```

Minimal install (`pip install -e .`) covers discovery and protocol capture. The extras are opt-in: `mcp`, `midi`, `audio`, `match`, `browser`.

## Quickstart

Check what your environment supports:

```bash
acro doctor
```

Put the Core on the same Wi-Fi network, then enable **Global Settings → Wi-Fi → HeadRush Remote = On** and find it:

```bash
acro discover
```

If mDNS is blocked on your network, double-tap "HeadRush Remote" on the device to read its IP and pass it directly:

```bash
acro --device 192.168.1.42 discover
```

Then map the editor's API surface:

```bash
acro probe --dump-app
```

This fetches the same files a browser would when you open the editor, mines them for endpoints, WebSocket URLs, and message types, and writes an inventory to your workspace (`~/.acro/captures/`).

## How it talks to the device

Four transports, tried in order, so the tool degrades instead of failing:

| Layer | Mechanism | Role |
|---|---|---|
| **A** | Native HTTP/WS client for the Remote Editor | Primary — fast and precise |
| **B** | Playwright driving the real editor UI | Fallback that cannot drift from what the device accepts |
| **C** | USB mass storage | Backup/restore, `.nam` and IR delivery |
| **D** | MIDI PC/CC | Rig and Scene switching, plus independent verification that a write landed |

We never need to reverse-engineer the `.rig` binary format: the Remote Editor can create rigs, so the device writes its own files.

## Tone testing

Three layers, usable independently:

1. **Offline simulation** — render a DI through a candidate NAM capture and cab IR in Python. Fast, no hardware, approximate.
2. **Hardware in the loop** — the Core's USB interface is 6-in/4-out with a dry DI on channel 5 alongside stereo wet on 1–2, and supports reamping. Play a riff *once*, then reamp that exact performance through unlimited candidates automatically.
3. **Human blind A/B** — ranked candidates presented without labels; your rating feeds the refinement loop.

**On metric honesty.** A commercial recording is not a DI and is not the same performance. Sample-aligned ESR — the metric the NAM ecosystem trusts — is only valid when comparing *your* DI through the Core against *your* reference. Against a source-separated stem from a record, only distribution-level metrics apply, and only as a *ranking* signal. `acro` enforces that distinction rather than reporting a confident number it cannot justify.

## Roadmap

- [x] Device discovery (mDNS, zeroconf browse, subnet sweep)
- [x] Remote Editor bundle capture and API surface extraction
- [x] CLI shell
- [ ] Live traffic recorder
- [ ] Firmware static analysis (read-only) for the model/parameter catalog
- [ ] Rig / Block / Scene model, validation, diffing
- [ ] Device client and write pipeline
- [ ] MCP server
- [ ] TONE3000 client and NAM capture selection
- [ ] Tone matching harness

## License

MIT — see [LICENSE](LICENSE).

Not affiliated with or endorsed by HeadRush, inMusic Brands, or TONE3000. "HeadRush" and "Core" are trademarks of their respective owners.

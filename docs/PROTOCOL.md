# The HeadRush Remote Editor protocol

**Status: not yet captured.** This document records what is established from the vendor's own documentation, what is unknown, and how `acro` goes about closing the gap. It will be updated with real endpoint data once capture has run against hardware.

There is, as far as public sources show, no existing documentation of this protocol anywhere — no published API, no third-party editor for the Prime/Core generation, and no reverse-engineering write-up. So this starts from zero.

---

## What is established

From the HeadRush Core user guide (§4.22) and vendor support articles:

| Fact | Source |
|---|---|
| The Remote Editor is a web app **served by the device** over Wi-Fi | User guide §4.22 |
| Requires firmware **4.0 or later** | Support article |
| Enabled at Global Settings → Wi-Fi → **HeadRush Remote = On** | User guide |
| Reachable at **`headrushcore.local`**, or the IP shown on the device | Support article |
| Double-tapping "HeadRush Remote" reveals the raw IP | Support article |
| Native browsers only — Edge, Safari, Chrome | Support article |

The editor is documented as able to: create a new rig, load next/previous rig, browse/rename/delete rigs, select and adjust block parameters, **drag to reorder blocks**, edit the signal path, toggle blocks, set block LED colours, save/rename/delete block presets, open an advanced block-edit page, manage setlists, show the CPU meter, and access HeadRush Cloud.

**That the editor can create rigs is the load-bearing fact for this project.** It means the device writes its own `.rig` files, so the binary format never has to be decoded.

Not exposed through the editor: IR, Amp Clone, and NAM management. Those go through the File Browser, USB, or Cloud.

## What is unknown

Everything about the wire format:

- **Port** — no port appears in the documented URL, so HTTP on 80 is the working assumption. Unconfirmed.
- **HTTP or HTTPS** — unconfirmed. A bare `.local` URL implies plain HTTP.
- **REST, WebSocket, or something else** — unknown. A live-updating editor with a CPU meter suggests a push channel, but that is inference.
- **Endpoint paths, methods, payload shapes** — entirely unknown.
- **Authentication** — unknown. Likely none on a LAN-local service, but that is an assumption, not a finding.
- **Whether the protocol is stable across firmware releases** — unknown, and assumed not to be.

Also unknown and worth capturing while we are in there: the maximum number of Scenes per rig, the Program-Change-to-rig numbering, and the file extensions used inside the `Blocks` and `Setlists` folders (`.rig`, `.wav`, `.ac`, and `.nam` are confirmed; those two are not).

## How acro closes the gap

Three independent approaches, so no single failure blocks the project.

### 1. Static analysis of the served bundle

`acro probe --dump-app` fetches `/` from the device and follows every script, stylesheet, and module reference it finds, staying on the device's own host. It then mines the retrieved text for:

- endpoint paths, ranked by evidence — a path appearing at a `fetch()` or `axios` call site is far stronger evidence than a bare string literal that merely looks like a path
- HTTP verbs, read from the call sites
- WebSocket URLs and constructor call sites
- message-type vocabulary (`type:`, `cmd:`, `event:` and friends, quoted or bare)

This is ordinary HTTP GET traffic — the same requests a browser makes when you open the editor. Nothing is written to the device.

Output is an `Inventory`, saved as `inventory.json` alongside the archived bundle in `~/.acro/captures/<timestamp>/`.

### 2. Live traffic capture

Static analysis recovers the API *surface*. It does not recover which call corresponds to which user action, nor the exact payload shapes. For that, a recording proxy sits between the browser and the device while a human performs known actions in the real editor — add a block, change a parameter, save — mapping each action to the wire calls it produces.

### 3. Firmware static analysis

`acro firmware analyse` inspects a firmware image offline. The image contains the same web bundle plus the model catalog and the code that serialises rigs, so it can yield the protocol *and* the per-model parameter schemas without the device being present.

This path is entirely read-only: the module opens local files and nothing else. There is no code path that contacts or reflashes a device. See [the firmware section](#firmware-analysis-boundaries) below.

### Cross-checking

Where two sources disagree — say the firmware lists a model the live device does not — the disagreement is **reported, not silently merged**. `Catalog.merge()` returns a list of conflicts, and an unverified catalog is labelled as such everywhere it surfaces.

## Paths that do not depend on any of this

Two interfaces work today, regardless of protocol progress:

**MIDI** is fully documented and is `acro`'s verification channel:

| CC | Function |
|---|---|
| 1 | Expression pedal |
| 49–53 | Footswitch 1–5 — the only way to trigger Scenes |
| 64 | Tap tempo |
| 65–74 | Looper transport |
| 75–88 | Toggle block 1–14 |
| 89 | Mic dry out |
| 102–115 | Practice tool, metronome |

Program Change selects rigs. There is **no scene-select control** — scenes are reached by simulating their footswitch, which is why footswitch assignment is part of the rig model.

MIDI is write-only here: the device does not report block state back, so it confirms a rig loads and responds, not that its contents match. `verify_rig` says so rather than implying more.

**USB mass storage** (Menu → USB Transfer) mounts the device as a drive named `HeadRush`:

```
Rigs/                     .rig
Setlists/
Blocks/
Impulse Responses/User/   .wav
Amp Clones/User/          .ac
NAM/User/                 .nam    side-loaded
NAM/TONE3000/             .nam    downloaded on-device
```

Individual files only — folders and archives are rejected by the device. After copying, eject the drive and tap **Sync**, or the files sit on the storage without entering the device's index.

## Firmware analysis boundaries

- Read-only. The module opens local files; it never contacts a device and cannot reflash one.
- Firmware images and anything extracted from them live in `~/.acro/firmware/` and are excluded from version control. The repository carries **derived documentation only** — never vendor binaries, bundles, or assets.
- Containers Python can open (zip, tar, gzip) are unpacked natively. Anything else is identified and the required external tool is named, rather than guessed at.
- Magic-byte matches shorter than four bytes are marked **weak**: a 2-byte signature occurs roughly every 65 KB of random data, so without that distinction 2 MB of noise appears to contain a dozen filesystems.
- Zip extraction refuses path traversal. A firmware image is untrusted input.

## Versioning

Protocol maps are stored per firmware version at `acro/protocol/maps/fw-<version>.json`. The client asserts the version it was built against and **fails loudly on a mismatch** rather than issuing requests it cannot justify — a wrong guess here corrupts a rig on someone's hardware.

## Reproducing the capture

```bash
# 1. Device on the same Wi-Fi, HeadRush Remote enabled
acro discover

# 2. If mDNS is blocked, read the IP off the device and pass it
acro --device 192.168.1.42 discover

# 3. Fetch and mine the bundle
acro probe --dump-app

# 4. Optionally, the same from a firmware image
acro firmware analyse fw-5.1.0.bin --unpack-to ./unpacked --strings
```

Results land in `~/.acro/captures/<timestamp>/`.

# The HeadRush Remote Editor protocol

**Status: captured and verified against a Core.** The verified map is [`rigsmith/protocol/maps/headrush-core.json`](../rigsmith/protocol/maps/headrush-core.json) (`verified: true`); every method signature in it was read from the device's own declarations, and a full rig was written with it and read back with zero mismatches. The sections after this one are kept as the record of how that was reached.

## What the capture found

- **Pairing, not passwords.** `POST /api/auth` starts a pairing; the person at the hardware accepts it with a PIN shown on the device, and the reply sets a session cookie. rigsmith keeps that cookie so the next process is still paired. The server identifies as `AZ0X Web Server`.
- **An object tree, not a REST API.** Everything lives under `/api/v1` as nodes rooted at `/Evil`: `GET /subtree{node}` (metadata, methods, live values), `GET /object-meta{node}` (types, ranges, option lists), `GET`/`PUT /object-properties{node}` (live values), and `POST /object-method{node}/{method}` (calls, answered in `methodReturnValue`).
- **A rig is whatever is loaded.** There is no rig resource to post. Building one is: `Rigs/makeNewRig` (with `makeNewRigConfirm` if the loaded rig is dirty), set `Engine/Patch/Chain` `ModuleType1..14` to module-table indices, set each block's properties, then `Rigs/saveRigAs` under a name.
- **Values are 0-1 on the wire.** The minimum and maximum a node publishes are its display range. The taper between the two is not published, so `protocol/calibrate.py` measures it per control.
- **Every block type exists twice** (`Amp` and `Amp 2`), so a rig can hold two of anything; the second is addressed by its own node.
- `Blocks/serializeBlock` returns a block's whole state as JSON in display units - the cheapest read-only snapshot of a block before a write.

## The rig's own tempo

`/Evil/Engine/Patch/Rig` holds the rig-level settings, beside its blocks.
`Tempo` is 0-1 on the wire across 30-240 BPM (the default, 0.4286, is
120), and `TempoFromMaster` chooses between `Fixed` (false: the rig loads
with its own tempo) and `Current` (true, the default: it keeps the tempo the
device is already at). A rig with a synced delay or tremolo is written
Fixed at the song's tempo - `rigsmith rig tempo`, or set by autopilot -
so its dotted eighth lands on the beat without tapping. The global tempo is
`/Evil/Engine/Tempo` (`Tempo`, plus `TempoMaj`/`TempoSub` for the screen's
whole and fractional parts).

## Scenes: found, not yet written

The loaded rig's scenes live on `/Evil/Engine/FootSwitch` (from the
September capture): for each of ten footswitches a `ModeNew{N}` (0 Toggle,
1 Hold, 2 Scene), a label in `UserFootSwitchText{N}`, and for every slot
`Scene{N}_{S}_Effect` (the block's name) and `Scene{N}_{S}_Mode`, an integer
0-2, plus `Scene{N}Slot{S}Preset` for preset recall and a second set
(`State2Scene...`) for two-state switches. With no scenes set every Mode is
0, so 0 is almost certainly *No Change*; which of 1 and 2 is *On* and which
is *Off* has not been seen.

Until it has, a write does not send scenes (guessing could invert every one)
and the write report lists what to set on each footswitch instead. To settle
it: on the device, make FS1 a scene that turns one block on and another off,
then run `rigsmith device scenes` - it prints each slot's raw Mode number.

, no existing documentation of this protocol anywhere — no published API, no third-party editor for the Prime/Core generation, and no reverse-engineering write-up. So this starts from zero.

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

## How rigsmith closes the gap

Three independent approaches, so no single failure blocks the project.

### 1. Static analysis of the served bundle

`rigsmith probe --dump-app` fetches `/` from the device and follows every script, stylesheet, and module reference it finds, staying on the device's own host. It then mines the retrieved text for:

- endpoint paths, ranked by evidence — a path appearing at a `fetch()` or `axios` call site is far stronger evidence than a bare string literal that merely looks like a path
- HTTP verbs, read from the call sites
- WebSocket URLs and constructor call sites
- message-type vocabulary (`type:`, `cmd:`, `event:` and friends, quoted or bare)

This is ordinary HTTP GET traffic — the same requests a browser makes when you open the editor. Nothing is written to the device.

Output is an `Inventory`, saved as `inventory.json` alongside the archived bundle in `~/.rigsmith/captures/<timestamp>/`.

### 2. Live traffic capture

Static analysis recovers the API *surface*. It does not recover which call corresponds to which user action, nor the exact payload shapes. For that, a recording proxy sits between the browser and the device while a human performs known actions in the real editor — add a block, change a parameter, save — mapping each action to the wire calls it produces.

### 3. Firmware static analysis

`rigsmith firmware analyse` inspects a firmware image offline. The image contains the same web bundle plus the model catalog and the code that serialises rigs, so it can yield the protocol *and* the per-model parameter schemas without the device being present.

This path is entirely read-only: the module opens local files and nothing else. There is no code path that contacts or reflashes a device. See [the firmware section](#firmware-analysis-boundaries) below.

### Cross-checking

Where two sources disagree — say the firmware lists a model the live device does not — the disagreement is **reported, not silently merged**. `Catalog.merge()` returns a list of conflicts, and an unverified catalog is labelled as such everywhere it surfaces.

## Paths that do not depend on any of this

Two interfaces work today, regardless of protocol progress:

**MIDI** is fully documented and is `rigsmith`'s verification channel:

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
- Firmware images and anything extracted from them live in `~/.rigsmith/firmware/` and are excluded from version control. The repository carries **derived documentation only** — never vendor binaries, bundles, or assets.
- Containers Python can open (zip, tar, gzip) are unpacked natively. Anything else is identified and the required external tool is named, rather than guessed at.
- Magic-byte matches shorter than four bytes are marked **weak**: a 2-byte signature occurs roughly every 65 KB of random data, so without that distinction 2 MB of noise appears to contain a dozen filesystems.
- Zip extraction refuses path traversal. A firmware image is untrusted input.

## Versioning

Protocol maps are stored per firmware version at `rigsmith/protocol/maps/fw-<version>.json`. The client asserts the version it was built against and **fails loudly on a mismatch** rather than issuing requests it cannot justify — a wrong guess here corrupts a rig on someone's hardware.

## Reproducing the capture

```bash
# 1. Device on the same Wi-Fi, HeadRush Remote enabled
rigsmith discover

# 2. If mDNS is blocked, read the IP off the device and pass it
rigsmith --device 192.168.1.42 discover

# 3. Fetch and mine the bundle
rigsmith probe --dump-app

# 4. Optionally, the same from a firmware image
rigsmith firmware analyse fw-5.1.0.bin --unpack-to ./unpacked --strings
```

Results land in `~/.rigsmith/captures/<timestamp>/`.

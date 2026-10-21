# rigsmith

**Recreate a recorded guitar tone on a HeadRush Core.** Work out what gear made it, find a NAM capture that matches, build the rig over local Wi-Fi, and measure how close you actually got.

Three ways in, all over the same core: a **web UI**, a **CLI**, and an **MCP server** so an AI agent can do the slow parts for you.

The web UI is grouped the way the work goes. **Chase a tone**: **Autopilot** (a song in, a draft rig and a listening test out, every decision shown), **Songs** (the record mapped into sections and guitar layers - pick the part you are chasing) and **Research** (the recipe, with every claim's source and confidence). **Your gear**: **Guitar lab** (teach it your guitar with a guided, clicked session, then play any riff on it), **My playing** and **Captures**. **Build and test**: **Rig** (a 14-slot pedalboard you drag), **Match** (waveforms and blind A/B), **Tune** (let it turn the knobs, and see the search) and **Loop** (the real Core between an output and an input). Then **Device**, **Workspace** and **Settings** (accuracy or local mode, and which model answers). An **Ask** drawer on every page (Ctrl+J) is a chat with agents that can use all of it, and Ctrl+K goes straight to any page, song, rig, guitar or recipe by name, or to a thing you can do ("knob curves", "local model", "tone sheet" - it opens the page and points at the control, leaving the click to you) - or asks the question you typed. The panels hand work to each other: a chosen part goes to the tuner, an unresolved amp goes looking for a capture, a compiled recipe opens as a rig.

```bash
pip install -e ".[all]"
rigsmith ui
```

> **Status: everything runs today.** Rig authoring, validation, NAM analysis, tone matching, catalog extraction, USB backup, MIDI, guitar learning, song maps, the hardware loop, the tuner, autopilot, the chat and the web UI are covered by 1169 tests. The Remote Editor protocol has been captured against a real Core and a rig written to it and read back exactly - see [docs/PROTOCOL.md](docs/PROTOCOL.md). What still wants hardware is *measuring*: the loop and the blind A/B need the Core and an interface plugged in - [docs/FIRST_SESSION.md](docs/FIRST_SESSION.md) walks through that first session.

---

## Why this exists

HeadRush firmware 4.0+ makes the Core serve a **Remote Editor** web app over Wi-Fi, and 5.1 added **native NAM A1/A2 support with TONE3000 integration**. So the device already has a network control surface, and already runs the open-source capture format that makes accurate amp recreation possible. Nothing was missing except something to drive it.

As far as public sources show, nothing like this exists for HeadRush — no MCP server, no published API, no documented `.rig` format, no third-party editor for the Prime/Core generation. The equivalents exist for Line 6 and Fractal gear; HeadRush has none.

## Safety

This project does **not** modify, patch, or reflash device firmware, and it never will.

- Device writes go through the same interfaces the official Remote Editor uses.
- Every write is gated: **validate → backup → diff → confirm → write → verify**. The backup is a gate, not a courtesy — without one the write is refused, because a rig built on the device's own screen exists nowhere else. Waiving it is explicit (`--no-backup`) and says what it cost.
- `RIGSMITH_READONLY=1` disables every write at startup.
- The web UI binds to **loopback only** and warns loudly if you override that — it can control an amp modeler on your network, and exposing it to that network would be a real footgun.
- Loopback is not the whole boundary, because any page in the same browser can reach 127.0.0.1. So the UI also refuses a request whose Host is not this machine (DNS rebinding) and one a browser sends from any origin but its own page - a form on another site cannot write a rig, and another site cannot open its event stream. The CLI and scripts send no Origin and are unaffected.
- Firmware analysis is read-only static inspection for interoperability. No vendor firmware, binaries, or assets are committed here — only derived documentation.
- If something goes wrong on the device, holding footswitches **4+5** at power-on restores factory rigs.

## Quickstart

```bash
rigsmith doctor        # what this install can and cannot do, who answers the chat, and how to fix each
rigsmith ui            # the web interface, at http://127.0.0.1:8765 - start on the Autopilot page
```

The Autopilot page's **Getting set up** card lists what to do and links to
each step; autopilot works before any of it is done, on a synthetic guitar
and the simulated bench, and says so on every result. With no song to hand,
**Try the demo song** (`rigsmith autopilot demo`) runs it on one made on the
spot - a clean intro, a double-tracked rhythm and a lead, each through known
settings of the simulated amp - and ends with a rig with a scene for each,
saying for each part which voicing it chose and how far each knob landed
from the real one. To bring in the Core,
put it on the same Wi-Fi network, enable **Global Settings → Wi-Fi →
HeadRush Remote**, then:

```bash
rigsmith discover              # find it
rigsmith device pair           # accept the PIN on the Core's screen
rigsmith autopilot run "Going Under" --artist Evanescence --mix going-under.flac --bench virtual
```

[docs/FIRST_SESSION.md](docs/FIRST_SESSION.md) is the first session with the
hardware, step by step: your guitar, the loop, a run on the device, the
blind test, the write, and finishing it by ear.

### Working on the protocol

The Remote Editor protocol is captured and verified (a rig written to a
Core and read back exactly); these are the tools that did it, for a new
firmware or another device:

```bash
rigsmith probe --dump-app      # map the editor's API surface
rigsmith protocol record       # watch the real editor work, and log every call
```

### A simulated Core, for the protocol tools

```bash
rigsmith protocol simulate --port 8100
rigsmith --device 127.0.0.1:8100 discover
rigsmith --device 127.0.0.1:8100 probe --dump-app
rigsmith catalog extract --capture ~/.rigsmith/captures/<timestamp>
```

A stand-in device, so the whole device-facing toolchain can be exercised
without the Core. **It is not the real protocol**: it speaks an invented
REST shape, while a real Core is an object tree - captured, and verified by
writing a rig and reading it back, in `headrush-core.json` (see
[docs/PROTOCOL.md](docs/PROTOCOL.md)). What the simulator is good for is
developing against: discovery, endpoint extraction, catalog extraction and
traffic recording all run end to end on it, and `--replay` serves a
recording of real traffic instead.

`rigsmith rig write` really does write - the client is driven by a
**protocol map**, which expresses one device and firmware's API as data
(`rigsmith protocol maps`). That is what let the write path be finished
before the real protocol was known; when it was captured it dropped in as
another JSON file, and the client gained the object-tree write it
describes. Unverified maps are labelled everywhere they are used, including
in the write report, so a guess can never quietly pass for knowledge.

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
rigsmith rig tempo cherub-rock 140     # synced delays load on the beat
rigsmith rig nudge cherub-rock "a bit brighter, too much reverb"   # by ear, in words
rigsmith rig undo  cherub-rock         # that was worse: step back (--redo to step forward)

rigsmith rig scene cherub-rock "Clean verse" --off 1 --off 4 --fs 1
rigsmith rig scene cherub-rock "Big chorus"  --on 1 --on 4 --fs 2

rigsmith rig validate cherub-rock
rigsmith rig preview  cherub-rock
rigsmith rig sheet    cherub-rock --out cherub-rock.html   # printable, in the screen's units
```

The **tone sheet** is the rig as you would dial it in by hand: each block's
controls as the Core's screen shows them (front-panel order, synced delays
by their note value), the scenes, and - for a draft autopilot built - the
song and part it chased, what the research said, which candidate won and
by whose judgement, and what still differs from the record. *Tone sheet* on
the Rig page or an autopilot run opens it as a page to print or keep open
beside the Core.

Every edit to a draft can be stepped back through - the last 30, from the
Rig page's *Undo* (Ctrl+Z), the command line, or by asking - so trying a
nudge costs nothing. *History* under the board names each edit, newest
first, and goes back to before any of them in one click
(`rigsmith rig history`, then `rig undo --steps N`). *Compare with...*
(`rigsmith rig diff a b`) shows what differs between two drafts - a rig
you finished by ear beside its rebuild from a blind test, which keeps
yours rather than replacing it.

### Use your own guitar

Every comparison here is against a record made on somebody else's instrument.
The one variable you can remove is yours.

```bash
rigsmith take add riff.wav --kind di --guitar "Les Paul Standard" \
  --pickup bridge --tuning "E standard" --note "the riff, twice"

rigsmith take record --seconds 20 --guitar "Les Paul Standard" --pickup bridge
    # plays once, keeps both halves: the wet tone and the dry DI

rigsmith take pull --list            # what is on the pedal's USB storage
rigsmith take pull --guitar "Les Paul Standard"   # import loops off it

rigsmith take list
rigsmith take compare stem.mp3 --nam ./captures/jcm800.nam   # which guitar?
```

Adding a take measures it: how far off concert pitch the guitar was, and the
lowest note in the recording. Thirty cents flat is inaudible as tuning and
very visible as tone — every harmonic lands slightly off where the record's
does — so it gets said before you spend an evening chasing captures for it.

`take compare` is the one that surprises people. A capture sweep answers
"which amp gets closest"; this answers the question underneath it - which
guitar should you be holding. Same rig, same target, one variable, and it is
the difference no amount of capture hunting fixes.

A take id then works **anywhere a filename does**:

```bash
rigsmith match score stem.mp3 cherub-riff --region 41.2:58.0
rigsmith match search cherub-riff stem.mp3 ./captures
```

Only a **DI** can be rendered through a candidate rig - it is the one signal
that has not been through an amp yet. A loop off the pedal has, so it arrives
labelled as a loop: reamping through one would stack two amps, which sounds
like neither.

### Teach it your guitar

A take is one recording. A **profile** is the instrument itself, learned:
play a guided sequence once - four notes a string, or every fret of every
string - and rigsmith cuts it into labelled notes, measures the guitar (level,
brightness, hum, tuning), and can then play *any* part on it.

```bash
rigsmith learn new "LP bridge" --guitar "Les Paul Standard" --pickup bridge
rigsmith learn guide chromatic --click     # what to play, and a click to play it to
rigsmith learn from lp-bridge my-di.wav --protocol chromatic
rigsmith learn show lp-bridge              # which frets it has heard

rigsmith learn part lp-bridge --midi riff.mid --stem stem.wav --region 41.2:58.0 --take
rigsmith learn part lp-bridge --stem stem.wav --region auto --take   # transcribe instead
```

`learn part` is the one that matters: the record's guitar part, played on
*your* guitar, on the record's clock. A tab's MIDI export gives the right
notes and is warped onto the record's timing; without one, the stem is
transcribed (exact on single-note parts, approximate on distorted chords,
and it says how much of the stem its notes explain). Candidates rendered from
that DI play the same notes at the same moments as the record, so they can be
scored with `--mode performance` - frame against frame, not just as a
distribution.

Frets it has not heard are built rather than stretched: a voice model of
each string (every harmonic's level and decay, and the string's
inharmonicity) interpolates between the nearest notes, and once a profile
has 48 notes or more a small network trained on all of them is averaged in.
On notes held out of a chromatic session the blend was about 5% closer than
interpolation alone; with fewer notes the network loses, so it waits.

No guitar to hand? `rigsmith learn demo --into demo` walks the whole flow on
a synthetic string, and says so everywhere it shows up.

To hear a riff on a learned guitar (`rigsmith learn play`, or the Guitar lab),
type it the way you would hum it: `@140 E2 G2 A2:4 (E2+B2+E3):2 - (A2 A#2)x2 A2!`
- an eighth note unless `:4`, `:16` or a dotted `:8.` says otherwise, `+`
stacks a chord, `-` rests, `x2` repeats, `!` accents. Exact timings work too:
one `start duration note [velocity]` per line.

**When the record was made on a different guitar.** A passive pickup and
its cable are a resonant low-pass - a single coil peaks high and sharp
(around 4-5 kHz), a humbucker lower and broader (2-3 kHz) - and that peak is
most of what makes a clean DI sound like one guitar rather than another.
rigsmith measures yours from every note it has learned, and can move a DI
toward another: a type (`strat`, `tele`, `p90`, `paf`, `hot-humbucker`,
`active`, `filtertron`), another guitar you have taught it, or any amount in
between. The DI stays sample-aligned, so performance scoring still works.

```bash
rigsmith learn pickup lp-bridge               # 2.8 kHz, Q 1.6 - reads as a PAF
rigsmith learn swap my-di --like strat --amount 0.7 --profile lp-bridge --take
rigsmith song di going-under-evanescence verse-2-left lp-bridge --like "EMG 81"
```

Autopilot does this on its own: when the research says the record was a
Strat, the part DI is moved to a single coil before any amp is heard
(`--like` to choose, `--keep-my-pickup` to opt out).

### Map the song, then pick the part

A track is not one tone - the clean intro, the double-tracked verse and the
solo are often three different amps. A **song** is the record you own plus a
map of it: sections (found by self-similarity, named by how they repeat),
guitar layers (a double-tracked rhythm is found by the two sides playing the
same notes *incoherently*; a centred lead by being what both sides share), and a tone
sketch for every section x layer - gain, mid scoop, low end, a delay and its
time against the tempo, room, tremolo.

```bash
rigsmith song new "Going Under" --artist Evanescence --mix ~/Music/going-under.flac --lookup
rigsmith song isolate going-under-evanescence      # the guitar stem, via Demucs
rigsmith song analyse going-under-evanescence      # sections x layers -> parts
rigsmith song tempo going-under-evanescence --double   # if it heard half notes as beats
rigsmith song choose going-under-evanescence verse-2-left --only
rigsmith song di going-under-evanescence verse-2-left lp-bridge --midi tab.mid
rigsmith song di going-under-evanescence intro-1-left lp-bridge --reading arpeggio  # no tab: say how to hear it
rigsmith song brief going-under-evanescence        # what a researcher starts from
```

The lookup fills in album, year and recording credits; it never fetches the
song itself - the record you chase is the copy you own.

The tempo decides each delay's note value and the tempo a draft rig is
saved with, and a beat tracker's known mistake is an octave (70 for 140).
Halve it, double it, type it, or tap along with the record on the song's
page; a tempo set by hand survives re-mapping.

Already recorded yourself playing the part, along with the record or to a
click at its tempo? `rigsmith song take <song> <part> <take>` (or *my take*
on the part) lines it up - one offset and one tempo ratio, found by matching
its notes against the record's - stretches it onto the part's clock with the
pitch kept, and makes it the part's DI, so it is scored frame by frame like
a rendered one. A take that does not follow the record (a different part,
a long rubato, noise) is refused with the reason, and autopilot's `--di`
falls back to comparing it as a distribution. Once a part has your take, every
later autopilot run on that part uses it as the DI without being told
(`--render-di` renders one from your guitar's voice instead).

### Two modes

**Accuracy** uses whatever gives the best answer: Claude through Claude Code
for research and agents, the network for lookups and TONE3000. **Local**
keeps everything on the machine - agents run on a local model (LM Studio,
Ollama, llama.cpp), and every call that would reach the internet refuses.
The Core on your LAN is still reachable. `rigsmith settings mode local`, or
`RIGSMITH_MODE=local` for one run.

Which local model? **Test it on rigsmith's tools** in Settings (or
`rigsmith settings check-model`) asks the loaded one six everyday requests
with the assistant's real tools and scores whether it reaches for the right
one - nothing is run. In accuracy mode, Settings says when Claude Code is
installed but signed out, and the app falls back to the API or a local model
until you `/login`. See [docs/LOCAL_MODELS.md](docs/LOCAL_MODELS.md).

### Put the real Core in the loop

The offline renderer approximates the Core; the loop *is* the Core. A DI
leaves your interface, goes down a cable into the Core's guitar input,
through whatever rig is loaded, and comes back in.

```bash
rigsmith loop suggest                 # routes that fit what is plugged in
rigsmith loop suggest --adopt analog-e2x2
rigsmith loop tone                    # is anything actually connected?
rigsmith loop bench                   # does the loaded rig have an Amp and a Cab to switch?
rigsmith loop bench-rig               # if not: a one-amp, one-cab draft to write
rigsmith loop calibrate --profile lp-bridge --direct guitar-straight-in
rigsmith loop reamp cherub-riff-di --label jcm800-try --take
rigsmith loop assess offline-render.wav jcm800-try-return   # same DI, two renderers
```

Three wirings: **analog** (interface out -> Core in -> Core out -> interface
in, one clock), **usb-reamp** (the Core's own USB - computer channel 3 is the
one that reaches the rig; 1-2 bypass it), and **hybrid** (interface into the
Core, recorded over its USB, where channel 5 shows the level the Core
actually received). Every capture carries sync marks at both ends, found
again by GCC-PHAT, so latency, polarity and clock drift are measured on
every pass rather than trusted from an old calibration - and the return comes
back on the DI's own timeline, ready for a sample-aligned comparison.

Windows lists one interface as a handful of devices per host API -
`Analog 1/2 (E2x2)` in, `Playback 1/2 (E2x2)` out, four times over.
`loop suggest` pairs them back into one box on one clock, prefers WASAPI
(which neither resamples behind your back like MME nor drops the box's name
like WDM-KS), skips loopback ports, and runs at the box's own sample rate.

Calibration matters more than it looks: an interface's line output is far
hotter than a pickup, so an uncalibrated reamp drives the amp like a guitar
on eleven. `loop calibrate` sets the send so the Core receives your DI at
the level your guitar gives it, and every capture says when that has not
been done.

No hardware connected? `rigsmith loop demo` sets up a simulated interface and
Core - with jittery latency, drifting clocks and a hot send, the faults the
real loop has to undo - and labels everything made on it as simulated.

### Let it turn the knobs

A capture search picks which amp; `tune` turns its knobs. Each setting is
rendered, scored against the part, and the next one chosen from what the
scores so far say - offline, or **live on the Core**: written to the loaded
patch over the network and reamped through the loop, so every evaluation is
the device's own output.

```bash
rigsmith tune run verse-target.wav --di verse-on-my-lp --mode performance \
    --renderer loop --draft going-under --budget 40
rigsmith tune apply 20261015-051226-38ea going-under --eq
```

Built for small budgets - a hardware evaluation is seconds of real audio - so
it spreads a few starting points (an amp's gain landscape has more than one
basin) and then pattern-searches from the best. What the knobs cannot close
is handed to a match EQ fitted by least squares to the Core's own G EQ bands
or a block's slot EQ, with the remaining difference measured, not promised.
In performance mode it finds its way back to the settings that made a target
from a different performance of the same notes; in distribution mode the low
end is barely constrained, which is why the part DI exists.

### What it can read

Whatever libsndfile on your machine can: **wav, aiff, flac, ogg, and mp3**
on a current install. An mp3 of the record is a perfectly normal target, and
the decoder handles the encoder's delay so it lines up.

Lossy audio is worth avoiding in one specific place - as the DI you render
candidates through, where the encoder's artefacts get printed into every
candidate equally and then measured as though they were tone. rigsmith says
so when you use one that way, and stays quiet when you do not. It also says
when a take is clipped, since clipping is distortion and distortion is the
thing these metrics measure.

Comparing a wav candidate against an mp3 target needs care, and gets it: the
candidate is re-encoded to match before measuring. Without that the codec's
own error is bigger than the difference between candidates — enough to put
the right answer last — and it is reported when it happens rather than done
quietly.

`m4a` and other AAC files - what iTunes sells - are not something
libsndfile decodes; with the `codecs` extra (`pip install
"rigsmith[codecs]"`) they are read through PyAV, the encoder's priming
samples taken off so they line up, and marked lossy like an mp3.

### Match it against the record

```bash
rigsmith tone3000 search "JCM800" --architecture 2
rigsmith tone3000 download 4821 --install         # fetch, verify, copy to the device

rigsmith match isolate song.wav                   # guitar stem via Demucs
rigsmith match capture-di --seconds 20            # play the riff once
rigsmith match search di.wav stem.wav ./captures  # render every capture, rank them
rigsmith match score stem.wav a.wav b.wav --region 41.2:58.0   # just the solo
rigsmith match score stem.wav a.wav b.wav --region auto        # or let it find the part
rigsmith match explain stem.wav a.wav --region 41.2:58.0   # what is still wrong
rigsmith match ab start s1 "Cherub Rock" a.wav b.wav c.wav
```

`--region` matters more than it looks. A track is not one tone — the clean intro, the rhythm verse and the solo are often three different amps — and scoring the whole file averages them into something no capture actually matches. In the **Match** panel you drag across the waveform instead of counting seconds.

Then the same panel runs the blind listening test — both clips are decoded up front and switching is a gain crossfade, so you compare the same instant of each rather than a reload.

### Or let autopilot do all of it

```bash
rigsmith autopilot run "Going Under" --artist Evanescence --mix going-under.flac \
    --profile lp-bridge --bench device --route analog-e2x2
```

One run, ten stages: find the song and its credits, isolate the guitar, map
it, pick the part (yours, or the most prominent), research the gear into a
recipe (every claim sourced, or marked as measured), play the part on your
guitar (moved to the record's pickup when the research names the guitar),
hear a field of the Core's amps chosen from the part's measured gain, and
one from every other gain range so a gain read wrong cannot leave the right
kind of amp unheard - each one switched live on the device, through its
usual cab, and reamped through the loop - hear the best amp through the other cabs worth trying (a
Greenback and a V30 4x12 on one head are different records), tune the best
two amps, fit a G EQ to what is left (it goes into the draft only if it
scores closer), then build a draft rig - with the gate, boost, chorus,
tremolo, delay and reverb the part measures as having, the boost left off
unless the draft is heard closer with it - and set up a blind
A/B of the finalists - three different amps, the best of each. A difference smaller than the score can resolve is called a tie,
not a win.

Choose several parts (the clean intro, the verse, the solo) and each gets a
run of its own, then one **song rig**: up to two amp-and-cab sounds side by
side in the chain, and a scene per part on its own footswitch that turns on
that part's sound and the effects it measures as having. A HeadRush scene
switches blocks rather than holding knob values, so the main part keeps its
sound, the second is whichever leaves the others nearest theirs, and a part
whose sound did not make the two is moved to the one nearest what its own
run found - and the run says which.
`rigsmith autopilot song-rig <run> <run> ...` builds one from finished runs,
and `--whole-song` (or **every sound in the song** on the page) picks the
parts itself: the most prominent part of each kind of section, with two
that are one sound - one layer, a gain within a quarter - chased once.

The effects are set to what was measured rather than left at their
defaults: a delay synced to the note value its echo is at the song's tempo
(a dotted eighth is the Core's 3/16), a tremolo synced when its rate sits
on the beat and set to the depth it was heard at, a reverb of the kind and
length the room measures as - and the rig saved with the song's tempo, so
the synced ones load on the beat. Times and rates are sent through the
catalog's measured knob curves where it has them. When the research
recorded how the amp was set ("gain on 7, mids at noon"), tuning starts
there; and once a part has your own take lined up with it, every later run
uses that as the DI. Each draft has a **tone sheet**: the rig as the Core's
screen reads it, and where it came from, to print or keep beside the
device.

Then listen. Once the blind test is done, `rigsmith autopilot listen <run>`
(or the button on the run) lets your ears decide: every finalist's verdict
goes into the song's recipe as a listening measurement, and if you preferred
something other than the score's winner, the draft is rebuilt from your pick. Every stage records what it decided and why, a failed stage
can be resumed, and nothing is written to the device: that stays yours.

`--bench virtual` runs the whole thing on the simulated amp - a rehearsal
that needs no hardware, and says so on every result.

It also learns your **ear habits**. After a run you finish by ear, what
the draft holds is compared with what autopilot built; when the same knob
goes the same way on three or more rigs - most of the ones you finished -
that is your room or your monitors, not the song. `rigsmith autopilot
habits` lists them ("Treble down on 3 of 4 rigs, typically -8%"), kept
apart per bench since a simulated amp's knobs are not the Core's, and
`--ear-habits` (a box on the form, or asking) starts the amp from them -
as one edit after the build, so one undo is back to what measured closest.
Undone turns do not count; only what the drafts hold now does.

A title can be all it takes: with no file given, autopilot looks for your
own copy in your music library by name (Artist/Album/NN Title, lossless
first; `rigsmith song find "Going Under" --artist Evanescence` shows what it
would pick), and only then, in accuracy mode, falls back to the store's
30-second preview. The library folders are a setting; nothing is uploaded.

### Ask for it in plain words

Everything above is a tool, and the app has a chat in front of all of them.
Type what you want; the assistant looks things up, and hands bigger jobs to
specialists - a researcher, a song mapper, a guitar coach, a tone matcher, a
rig builder - that work in the background with only the tools their job
needs. Anything that would change the device or delete work stops and asks
you first.

```bash
rigsmith chat                                   # the same assistant, in a terminal
rigsmith agent run researcher "the gear on Going Under's verse riff"
rigsmith agent status                           # which models are available
```

In accuracy mode the agents are Claude, through Claude Code (with rigsmith's
tools and web search) or the Anthropic API. In local mode they run on
whatever OpenAI-compatible server is on this machine - LM Studio, Ollama,
llama.cpp - with Qwen's tool-call formats handled, including the ones it
writes into its text instead of the proper field. See
[docs/LOCAL_MODELS.md](docs/LOCAL_MODELS.md) for which models to load.

With no model answering - nothing loaded yet, or Claude Code signed out -
the chat says so, and then offers what needs no model: "a bit darker"
becomes a button that turns the rig, "undo that" one that steps it back,
and "where do I measure the knob curves?" the pages and controls whose
names match. Each is a click you make.

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
  (click)       (135 tools)       (FastAPI)
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

Extras: `ui`, `mcp`, `midi`, `audio`, `match`, `separation`, `browser`, `claude` (the Anthropic SDK, for agents on the API rather than through Claude Code), `tune` (Optuna, for larger tuning budgets), `transcribe` (Spotify's basic-pitch: open six-string chords, slow driven fingerpicking, drop-tuned chugs among chord stabs and the odd reverb-soaked arpeggio, which the built-in transcriber reads less well - single notes, riffs, palm-muted chugs, arpeggios, Travis picking, strummed and power chords it reads well and voices as played; it runs as its own command, so if it will not install on rigsmith's Python, install it apart - `pipx install basic-pitch --python 3.11` - and it is found on the PATH, or set `RIGSMITH_BASIC_PITCH` to its command; used automatically once installed, and not in `all` for its size), `codecs` (FFmpeg through PyAV, so a bought .m4a/AAC song reads as it is, and candidates are re-encoded as AAC to compare fairly against it), `dev`. `rigsmith doctor` tells you which you're missing and why you'd want each.

## Documentation

The guides open in the app too (Ctrl+K, or `/docs/<name>`).

- [docs/WHATS_NEW.md](docs/WHATS_NEW.md) — everything this version does, by what you would do with it, and what still needs you
- [docs/LOCAL_MODELS.md](docs/LOCAL_MODELS.md) — which local server and model to run the agents on, and what to expect
- [docs/FIRST_SESSION.md](docs/FIRST_SESSION.md) — the order that works the first time the Core and an interface are plugged in, from teaching it your guitar to writing the rig
- [docs/PROTOCOL.md](docs/PROTOCOL.md) — what is known about the Remote Editor protocol, what is not, and how it gets captured
- [docs/TONE_MATCHING.md](docs/TONE_MATCHING.md) — how tone comparison works and where the numbers stop being trustworthy
- [docs/AUTOPILOT.md](docs/AUTOPILOT.md) — how autopilot decides: its stages, which parts and amps, what a run at the Core costs, a whole song's rig, and what to trust
- [docs/READING_NOTES.md](docs/READING_NOTES.md) — how a part's notes are read for its DI, what reads well, how to check them, and how to put them right

## What's left

- [x] ~~Capture the Remote Editor protocol against hardware~~ - captured, and a rig written to a Core and read back exactly
- [x] ~~Extract the full model catalog from a real device~~ - 327 models, verified
- [ ] **A first measured session on the Core** - the loop, a reamped search, and a blind A/B; everything up to it is built and rehearsed on the simulated bench ([docs/FIRST_SESSION.md](docs/FIRST_SESSION.md))
- [ ] Measure the knob curves - 39 measured on the Core so far, 1113 once shared across each block's models; the Device page measures the loaded rig's
- [ ] Write scenes over the network - built and read back after each write; it needs the Core's on/off numbers learned once, from one scene set by hand (Device -> *Learn on/off from FS1*), and until then the write report lists what each footswitch should do

## License

MIT — see [LICENSE](LICENSE). Vendored front-end libraries keep their own licences, recorded in [`rigsmith/web/static/vendor/README.md`](rigsmith/web/static/vendor/README.md).

Not affiliated with or endorsed by HeadRush, inMusic Brands, or TONE3000. "HeadRush" and "Core" are trademarks of their respective owners.

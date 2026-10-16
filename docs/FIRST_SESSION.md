# Your first session with the hardware

Everything in rigsmith runs without the Core plugged in - the simulated
bench rehearses every step - but a tone only becomes *yours* once the real
gear is in the loop. This is the order that works, start to finish. Each step
names where it lives in the app (`rigsmith ui`) and the command that does the
same thing.

The **Getting set up** card on the Autopilot page tracks most of this and
links to each step.

## 1. Choose who answers

**Settings.** *Accuracy* uses Claude (through Claude Code, or the API) for
research and the network for song lookups; *local* keeps everything on the
machine with a local model. For local, start LM Studio, Ollama or
llama.cpp first - see [LOCAL_MODELS.md](LOCAL_MODELS.md) - and press
*Detect loaded models*.

If Settings shows Claude Code as *signed out*, run `claude` once in a
terminal and `/login`; until then accuracy mode quietly uses the API or a
local model, and an autopilot run's research stage says it was skipped and
why.

## 2. Teach it your guitar

**Guitar lab -> New guitar**, then pick the input your guitar is plugged
into (on a Windows interface that is the *Analog* port, never *Loopback*).

1. **Check my level**: play hard for three seconds. Aim for "good" - peaks
   between -30 and -3 dBFS, hum well below the playing.
2. **Start the guided session**: *Quick* is a minute; *Every note on every
   string* is the accurate one, and from 48 notes a small network joins the
   voice model for frets it has not heard.

```bash
rigsmith loop level "Analog 1/2 (E2x2)"
rigsmith learn new "LP bridge" --guitar "Les Paul Standard" --pickup bridge
rigsmith learn guide chromatic --click
```

## 3. Add the song

**Songs -> New song**: your copy of the record (an iTunes .m4a works with
`rigsmith[codecs]`), and an isolated guitar stem if you have one -
`rigsmith[separation]` makes one with Demucs. **Map it**, then pick the part
you are chasing in the map.

## 4. Pair with the Core

**Device -> Pairing -> Pair**: the PIN appears on the page and on the Core's
screen; accept it on the device. `rigsmith device pair` does the same.

Then, while you are on that page, **measure the knob curves**: load a rig
holding the blocks you use (an Amp, a Cab, a delay, a reverb, a G EQ) and
press *Measure the loaded rig's knob curves*. It reads, writes nothing to
the Core, and saves into the catalog how each control's 0-1 maps to what
its screen shows - a curve measured on the Amp block serves all its amp
models. Until a control's curve is measured, the tone sheet marks its
reading `~` and autopilot sets times and rates assuming it is linear.
`rigsmith catalog calibrate` does the same.

## 5. Wire the loop

**Loop**: the suggestion for your interface shows its wiring - interface
output 1 into the Core's guitar input (through a reamp box if you have one),
the Core's 1/4" output L back into input 1.

1. **use this**, then **test tone**: it should come back well above the
   noise.
2. **calibrate**, with your guitar selected, so the Core receives your DI as
   hard as your guitar hits it.
3. **check the Core's rig**: autopilot switches amp and cab models on the
   loaded rig, so it needs an Amp and a Cab block. If it says no, **make a
   bench rig draft** and write it from the Rig panel (the write backs up
   first).

## 6. Run autopilot on the device

**Autopilot**: the song, your guitar, bench *device*, the loop route. It
maps, researches, renders the part on your guitar (moved to the record's
pickup when the research names the guitar), hears amps and cabs through the
Core, tunes, and builds a draft.

## 7. Listen, then let your ears decide

**Blind A/B the finalists**, all the way through - the names stay hidden
until you finish. Then **use what my ears picked**: the verdicts go into the
recipe, and the draft follows your ears if they disagreed with the score.

## 8. Write it

**Rig -> Write to device**: validate, back up, diff, confirm, write, verify.
Nothing reaches the Core without this step. A draft with a synced delay or
tremolo carries the song's tempo (the BPM beside the signal path), saved as
the rig's *Fixed* tempo so it loads on the beat; tap tempo still moves it.

Not paired yet, or would rather dial it in yourself? **Tone sheet** shows
every block's settings the way the Core's screen reads them.

A song rig's **scenes need one thing learned first**: which number this
Core uses for on and which for off. Two minutes, once: on the Core, make
footswitch 1 a scene that turns the **first** block in the chain on and the
**second** off, save, and press **Device -> Learn on/off from FS1** (or run
`rigsmith device scenes --learn`) with that rig loaded. Every rig written
after that carries its scenes, read back to check; until then the write
report lists what each footswitch should do.

## 9. Finish it by ear

With the rig written and loaded, play along with the record and say what
is off. **Rig -> By ear** takes the words - *too boomy*, *a bit brighter*,
*way too much reverb*, *fewer repeats* - and moves the amp, reverb or delay
a step that way; tick **on the Core too** and the loaded patch changes as
you press Enter, so you hear it at once. The draft keeps every move; write it
again when it sounds right, since the Core's own copy is only saved by a
write. The same works from the chat ("it's too fizzy") and from
`rigsmith rig nudge`.

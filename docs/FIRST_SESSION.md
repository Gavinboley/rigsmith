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

If Claude Code says it cannot authenticate, run `claude` once in a terminal
and `/login`.

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

A song rig's **scenes are not written over the network yet** - the write
report lists what each footswitch should do, to set by hand. While you are
there, two minutes settles it for next time: make FS1 a scene that turns
one block on and one off, and run `rigsmith device scenes`.

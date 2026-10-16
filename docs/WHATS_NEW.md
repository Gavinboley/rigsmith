# What's new on the autopilot branch

Everything below runs today on the simulated bench and a synthetic guitar;
the parts that need the Core, your guitar or your copy of a song say so.
[FIRST_SESSION.md](FIRST_SESSION.md) is the order to try them in.

## A song in, a rig out

- **Autopilot** takes a title (your own copy is found in your music library
  by name), maps the record, picks the part, researches the gear, plays the
  part on your guitar, hears a field of the Core's amps and cabs, tunes the
  best, fits a G EQ when it earns its place, and builds a draft with the
  gate, boost, modulation, delay and reverb the part measures as having -
  at the song's tempo, so synced delays land on the beat.
- **Several parts** become one rig with a scene per part, inside the DSP
  budget, with a delay each where the parts want different ones.
- **The blind test decides**: a blind A/B of the finalists, and "use what
  my ears picked" rebuilds the draft from your choice - keeping the draft
  you had already finished by ear beside it.
- **The tone sheet** is the rig as you would dial it in by hand, where it
  came from, what still differs from the record, and what you changed
  since it was built.

## The song map

- Sections and guitar layers (left, right, centre, doubled) with names
  guessed from how songs go; pick the part you are chasing.
- Measured from the record: gain, chorus, vibrato, tremolo and its depth,
  the delay and the room's reverb, tempo (correctable - halve, double, tap),
  chords and riffs transcribed for the DI.
- A re-map keeps your choices, even where sections moved.

## Your guitar and the loop

- **Guitar lab**: a guided minute of playing (or every note on every
  string) learns your guitar; a small network fills the frets you did not
  play. Check the input level first.
- **Loop**: interface out, through the Core, back in - routes suggested for
  what is plugged in, wiring drawn, calibrated for level and latency, every
  pass aligned to the sample.
- Your own take of a part can stand in for the rendered DI, on the record's
  clock.

## Finishing by ear

- **By ear** on the Rig page: "a bit brighter", "too boomy", "way too much
  reverb", "fewer repeats" - the amp, the reverb or the delay, a step that
  way; tick *on the Core too* to hear it at once.
- **Undo** (Ctrl+Z), **History** back to before any edit, and **Compare
  with...** for two drafts side by side.
- **Ear habits**: the knobs you keep turning after autopilot, offered as the
  next run's starting point - never assumed.
- **Finish by ear** on a finished run opens its draft with the words ready.

## The Core

- Pairing, writing a rig with a backup, diff and read-back, the catalog of
  its 327 models, knob curves measured from the app and shared across a
  block's models, scene numbers learned once (Device -> *Learn on/off from
  FS1*) and then scenes written, MIDI to load and verify.

## Asking for it

- **The chat** (Ctrl+J) has agents behind it - a researcher, song mapper,
  guitar coach, tone matcher and rig builder - on Claude in accuracy mode or
  a local model (Qwen) in local mode. Anything that changes the device asks
  first. With no model answering, it still offers what needs none.
- **Ctrl+K** goes to any page, song, rig or guitar, any thing to do (it
  points at the control), any of these guides - or asks.
- `rigsmith doctor` says what is missing and who would answer the chat;
  `rigsmith ui` is at http://127.0.0.1:8765.

## What still needs you

- Claude Code is signed out: run `claude` once and `/login` (or load a
  local model for local mode).
- `pip install 'rigsmith[midi]'` for MIDI over the Core's USB cable.
- Your copy of the song you want first.
- An hour with the Core and the interface: the steps are in
  [FIRST_SESSION.md](FIRST_SESSION.md).

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
- **The blind test decides**: a blind A/B of three different amps, the
  best of each, and "use what my ears picked" rebuilds the draft from your
  choice - keeping the draft you had already finished by ear beside it.
- **The tone sheet** is the rig as you would dial it in by hand, where it
  came from, what still differs from the record, and what you changed
  since it was built.
- **A demo song** to try it all on with no file: made here - a clean intro,
  a double-tracked riff and chorus, a lead solo, a band under them -
  through known settings of the simulated amp, a dotted-eighth delay on
  the lead and a room on the intro, so each part's run ends with how close
  it came: the voicing it chose, each knob against the real one, and
  whether its effects were heard and made it into the draft. `rigsmith autopilot demo`, **Try the demo song** on the Autopilot
  page, or ask the assistant.
- **The whole song** in one run: `--whole-song`, or **every sound in the
  song**, chases the most prominent part of each kind of section - two that
  are one sound only once - and builds one rig with a scene for each.

## The song map

- Sections and guitar layers (left, right, centre, doubled) with names
  guessed from how songs go; pick the part you are chasing.
- Measured from the record: gain, chorus, vibrato, tremolo and its depth,
  the delay and the room's reverb, tempo (correctable - halve, double, tap),
  and the notes transcribed for the DI: single notes, riffs, palm-muted
  chugs, strummed and power chords read right through gain - voiced the way
  they were played, a two-note E5 as two notes, a close triad close, a
  drop-D low string kept - and arpeggios and Travis-style picking note for
  note, each left to ring; a drum kit or a bass bleeding into a separated
  stem is not taken for notes, and a delay's repeats are taken out before
  the notes are read. Open six-string chords, slow driven fingerpicking,
  chugs among chord stabs in a drop tuning, and an arpeggio drenched in
  reverb want basic-pitch (`pip install 'rigsmith[transcribe]'`), used
  automatically once installed - or a tab's MIDI.
  [READING_NOTES.md](READING_NOTES.md) has it all in one place. A part read the wrong way can be
  re-read as single notes, chords or an arpeggio ("on my guitar" on its
  card), and keeps that reading for autopilot.
- **Notes** on a rendered part (and on an autopilot run's DI) draws what
  the DI plays as a piano roll; "save as MIDI", fix a note in a tab
  editor, and choose or drop the file back into "on my guitar".
- A re-map keeps your choices, even where sections moved, and the names
  you gave sections; a run keeps a song's map rather than redrawing it.
- Sections start on the downbeat where the music changes, not up to a
  second late with the next section's start inside them.
- Power chords up the neck are read as chords, not as the note an octave
  under them; B standard and other low tunings are read at their own
  octave when the research has not named the tuning.
- A lead centred between a double-track comes out with less of the rhythm
  in it, and the sides with less of the lead: the centre is taken as what
  the two sides share. On the demo song's solo the lead came out 5.5 dB
  clear of the rhythm, from 3.4, its notes read at 0.55 from 0.44, and its
  closest amp moved from 0.394 to 0.353; a re-map picks it up.
- Effects are told apart: a centred lead's delay is found though its layer
  hides it, a chorus is not mistaken for a room nor a room for a chorus,
  and parts that never pause are not given a room they do not have.

## How the amps are judged

- **Every gain range is heard** - every simulated voicing, or at the Core
  one amp from each range - so a gain read wrong cannot keep the right kind
  of amp out of the running.
- **Like with like**: the record's delay is taken out before the amps are
  compared, and a part in a room has its amps heard through one like it -
  left in, either one let the wrong voicing win most of the time.
- **At the Core**, a long part is heard on its loudest 16 seconds, about
  ten minutes a part, and the page says how long before it starts.
- **A boost is heard, not assumed**: a tight high-gain part's draft has
  one in front, left on only where the whole draft is heard closer to the
  record with it - the amp was tuned with nothing there, so its gain
  already reaches the record's. At the Core it goes in off, for you to try.
- **A song rig's third sound** goes to the kept amp nearest what its own
  run found; [AUTOPILOT.md](AUTOPILOT.md) says how every choice is made.

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
- **Hear it** on the Rig page (`rigsmith rig hear`): your DI through the
  whole draft - amp, cab, G EQ, boost, the delay at the rig's tempo, the
  room - on the simulated bench, beside the record and scored against it;
  a song rig a scene at a time, each against its own part. A rehearsal
  before anything is written - and on a finished run, **Hear the draft**
  and a **hear** by each of its song rig's scenes, without leaving it.

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

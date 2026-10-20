# How autopilot decides

A run takes a song and gives back a draft rig and a blind listening test.
Every choice it makes along the way is written on the run, with the reason;
this is what those choices are and where to change them.

## The stages

| Stage | What it does | Change it |
|---|---|---|
| song | Finds the song, or makes it from your file; looks up credits in accuracy mode | Give it your copy, or a title your music library has |
| stem | Pulls the guitar out of the mix (Demucs, when installed) | Give it a stem you already have |
| map | Sections, guitar layers, a sketch of every part - kept when the song is already mapped | Re-map on the Songs page; rename a section there and it stays named |
| parts | The parts to chase | Choose them on the Songs page first |
| research | A researcher agent writes the gear into a recipe, every claim sourced - or the recipe is seeded from the measurements | Edit the recipe; name the tuning and guitar there |
| di | The part played on your guitar, on the record's clock | "on my guitar" on a part: another reading, a tab's MIDI, your own take |
| candidates | A field of amps heard through the bench and scored against the record | The research and your past blind tests come first |
| cabs | The best amp through the other cabs worth trying | |
| tune | The best two amps' knobs turned toward the record, then a G EQ if it scores closer | The tuning budget |
| rig | A draft with the amp, the cab and the effects the part measures as having; a blind A/B of the finalists | Finish it by ear on the Rig page |

A stage that went wrong can be fixed and the run resumed from it.

## Which parts

The parts you chose on the Songs page; with none chosen, the most prominent
guitar part - loud, in a chorus, on a layer of its own. **Every sound in the
song** (or "one per sound" on the Parts card, to see and change the pick
first) takes the most prominent part of each kind of section, and two that
are one sound only once: on one layer - either side of a double-track
counts as one - with gains within a quarter of each other. Each part gets a
run of its own, and the song one rig with a scene per part.

## Your guitar, and the notes

The DI is the part's notes played on the guitar you taught rigsmith - or on a
synthetic string if you have not - moved toward the record's pickup when the
research names its guitar. The notes are read from the stem; when they are
wrong, every candidate is heard playing a different part from the record.
[Reading a part's notes](READING_NOTES.md) is how to check them and put them
right.

## Which amps are heard

The part's measured gain picks a range - clean, edge of breakup, crunch,
high, modern - and the field is three amps from it and one from each range
beside it, after any the research names and any your ears chose before. The
measurement can be two ranges out (a lead centred between a driven
double-track reads as barely broken up), so:

- **at the Core**, one amp from every other range joins the field - two more
  reamps, and the right kind of amp is always heard;
- **on the simulated bench**, every one of its four voicings is heard.

The amps are heard with nothing after them, so a delay on the record is taken
out of it before they are compared - left in, every amp was scored against
repeats it could not make, and the wrong one often won. The delay itself
goes on the draft, at the time and note value the map measured. A part in
a room the map could time is heard the other way round: each amp through a
room like it.

A mastering limiter rides the level under an echo and can hide it - on a
limited copy of the demo song, the lead's delay went unheard and its draft
had none. When a part plainly has a delay and its draft does not, add one
by ear; the part's sketch says what was measured.

## Tuning, and how long it takes

The best two amps have their gain, bass, mid, treble, presence and master
turned toward the record, starting from what the research says they were set
to, or from where the untuned sound says to start. On the simulated bench
that takes seconds. At the Core every amp, cab and setting is reamped in real
time, on the part's loudest 16 seconds: about ten minutes a part at the
default budget - the form says how long for yours.

## A whole song's rig

A HeadRush scene switches blocks on and off; it does not hold knob values. So
a song rig carries up to two amp sounds side by side, and each scene turns
its part's on. With three or more, the main part keeps its sound, the second
kept is the one that leaves the others nearest their own, and a part whose
sound did not make the two goes to the one nearest what its own run found -
the run says which parts were moved.

## What to trust

The score ranks; your ears decide. A difference smaller than the score can
resolve is a tie. **Hear it** on the Rig page plays your DI through the
whole draft - the boost, EQ and effects the run added after the amp too -
beside the record, before anything is written. The blind A/B plays the finalists without their names, and
"use what my ears picked" rebuilds the draft from your choice.

Knobs that land away from the record's settings are usually making up for
what differs from it - your guitar, the cab - not a miss: the closest sound
on your guitar is not the record's settings on its guitar.

**The demo song** (Try the demo song, or `rigsmith autopilot demo`) is a song
made on the spot through known settings of the simulated amp - and a delay
on its lead, a room on its clean intro - so each of its parts ends with an
answer: the voicing autopilot chose, each knob against the one the song was
made with, and whether its effects were heard and are on the draft. It is the quickest check that the whole
chain works on your machine.

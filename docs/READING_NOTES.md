# Reading a part's notes

To test a tone against a record, rigsmith plays the record's part on your
guitar - the same notes at the same moments - and runs that DI through the
candidates. It reads the notes from the guitar stem. When it reads them
wrong, every candidate is heard playing a different part from the record,
and the search tunes the amp to make up for notes. So the notes are worth a
glance before anything is tuned against them.

How much, measured end to end - a known amp, a DI from the true notes or from
a wrong reading, tuned, then the tone it found heard on the true notes beside
the record's: on a clean power-chord part a wrong reading left the tone twice
as far off; through a driven amp, about a quarter further at most. Wrong notes
also raise every candidate's score (0.09 to 0.20 on a crunch part), which
hides how close the best one really is.

## What it reads well

Measured on synthetic parts, clean through high gain (F1 a note, 1.0 is every
note right):

| Part | Reads at |
|---|---|
| Single-note riffs, leads, palm-muted chugs | 0.98-1.00 |
| Strummed triads, sevenths, eighth-note re-strums | 0.84-1.00 |
| Power chords - E or drop C, two or three strings, low or up the neck | 0.46-1.00 (high gain lowest) |
| Arpeggios and Travis picking, each note left to ring | 0.62-1.00 |

Chords are voiced the way they were played - a two-note E5 as two notes, a
close triad close, a drop-D low string kept - by reading the octaves after
each attack, not just the chord's name. A power chord up the neck is read as
the chord: its root and fifth together sound like one note an octave under
the root, and a part of E5s on the A string used to come back as low E
single notes, or through distortion as almost nothing. That note has no 5th
or 7th harmonic of its own - a string always does - which is how it is told
apart from a low riff in a drop tuning.

## What a recording does to it, and what is done back

- **A delay's repeats** read as notes played: a lead came back with twice
  its notes. The echo's time, mix and feedback are read off the stem and
  the delay is undone before the notes are read (delayed parts back from
  0.6-0.9 to 0.91-0.93). The transcription says when it did.
- **A drum kit bleeding into the stem** read as attacks - a chord on every
  hi-hat. Attacks are read from the guitar's band, below 6 kHz.
- **A bass bleeding in** rang an octave under the chords and was taken for
  their root. A chord fits its octaves first.
- **The first note** of a part cut on its downbeat used to go missing; it
  is kept.
- **The lowest string** is inferred from the stem - from its fundamental,
  or, where a cab has taken that away (a B1 keeps little of it), from the
  5th and 7th harmonics only a string sounds. B standard read an octave up
  before (F1 0.38); it reads note for note. A drop tuning played mostly
  above its low string still reads as standard. When the research names the
  tuning (the recipe's), the notes are read down to it.
- **A low riff's own note** can stand in the cepstrum like a slapback - a
  dry drop-A riff showed an "echo" every five A1 cycles - and is no longer
  taken out as one.

## Checking them

On a part's card, once its DI is rendered:

- **heard as** - single notes, chords or an arpeggio, or "notes from the tab".
- **notes** - the piano roll of what the DI plays, with the count and the
  range. Click it to hear the DI from that moment. "136 notes, A1 to E6" is
  a transcription to doubt; so is a lead two octaves under where it sits on
  the record.
- A render warns when notes go past a guitar's 24th fret or spread over
  three octaves, and when they go below your guitar's lowest string.
- From the command line: `rigsmith song notes <song> <part>`, with
  `--midi-out` for the MIDI.

The tone sheet says what played the part into the rig, and autopilot's DI
step says how the notes were read.

## Putting them right

1. **Read it another way.** "On my guitar" on the part asks how to read the
   stem: let it decide, single notes, chords, or an arpeggio (one note per
   pick, left to ring). The part keeps the reading, and autopilot uses it.
2. **Give it a tab's MIDI.** A Guitar Pro or Songsterr export of the part,
   aligned to the record by its notes. Or **save as MIDI** from the notes
   view, fix the wrong note in a tab editor, and choose or drop the file
   back in the same dialog.
3. **Name the tuning.** Research records it (`set_recipe_guitar`); a drop
   tuning it knows is read down to its low string.

## Where it stops

These still read poorly, and want basic-pitch or a tab:

- open six-string chords (0.55-0.70),
- slow, driven fingerpicking,
- chugs among chord stabs in a drop tuning (0.1-0.4),
- a clean arpeggio drenched in a long reverb (read as chords),
- parts that switch between single notes and chords within a bar.

**basic-pitch**, Spotify's transcriber, is used automatically once it can be
run: `pip install 'rigsmith[transcribe]'`. It runs as its own command, so if
it will not install on rigsmith's Python, install it apart -
`pipx install basic-pitch --python 3.11` - and it is found on the PATH, or
set `RIGSMITH_BASIC_PITCH` to its command. `rigsmith doctor`, and the setup
card on the Autopilot page, say which one is used.

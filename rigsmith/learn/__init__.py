"""Learning your guitar, so candidates can be heard through it.

Everything else in rigsmith compares a candidate rig against a record made on
somebody else's instrument. The takes library removed one layer of that - play
the part yourself, render through your own DI. This package removes the next
one: it learns the instrument well enough to *play parts you have not
recorded*.

Three levels, each usable on its own and each a strict improvement on the one
before it:

1. **Notes** (:mod:`.segment`, :mod:`.protocols`) - play a guided sequence,
   anything from the open strings to every fret of every string, and it is cut
   into labelled notes: pitch, string, fret, how hard, how long it rings.
2. **A sampler** (:mod:`.profile`, :mod:`.synth`) - those notes become a
   multisampled instrument, so a note sequence renders into a DI in your
   guitar's voice.
3. **A voice model** (:mod:`.voice`) - harmonic and noise envelopes fitted per
   string and register, which fills the frets you did not record without the
   chipmunk artefacts that stretching a sample a fifth would print.

:mod:`.transfer` joins them to a record: transcribe the guitar part from an
isolated stem, play it back through your profile, and you have a clean DI of
*that song's part on your guitar* - the thing that makes testing an effects
chain against the record a like-for-like comparison.
"""

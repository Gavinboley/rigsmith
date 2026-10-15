"""Understanding a song well enough to chase one part of it.

A track is not one tone. The clean intro, the double-tracked verse riff and
the solo are routinely three different amps, and a guitar stem is all of
them stacked. Everything downstream - which capture to try, which DI to
render, which seconds to score - goes better when it knows which of those it
is looking at, so this package maps the song first:

* :mod:`.structure` - tempo, key, and where the sections change, labelled by
  how they repeat (the chorus is the loud thing that comes back).
* :mod:`.layers` - how many guitars there are and where they sit: a
  double-tracked rhythm hard left and right, a lead up the middle. Each can
  be pulled out on its own from the stereo stem.
* :mod:`.describe` - what each part sounds like, in the terms a rig is built
  from: how much gain, how bright, how scooped, how much room, a delay and
  its time against the tempo.
* :mod:`.acquire` - finding the song: metadata, credits, a preview to hear.

The result is a *song map*: sections down one side, layers across the top,
and every cell a part that can be picked, researched, rendered and matched
on its own.
"""

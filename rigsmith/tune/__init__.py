"""Tuning a rig towards a target, by listening to it.

A capture search picks *which* amp; this turns its knobs. Each candidate
setting is rendered - offline, on the simulated bench, or on the real Core by
writing the parameters live over the network and reamping a DI through the
loop - scored against the part being chased, and the next setting is chosen
from what the scores so far say.

Hardware evaluations cost real time (a few seconds of audio each), so the
optimisers here are built for small budgets: tens of evaluations, not
thousands. And a tuned setting is a ranking's winner, not a verdict - it goes
to the blind A/B like everything else.
"""

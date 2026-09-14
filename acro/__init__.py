"""acro - AI tone authoring for the HeadRush Core.

Research a song's guitar tone, build the matching rig on the device over
local Wi-Fi, and test the result against the source recording.

Nothing in this package modifies or reflashes device firmware. Every write
to the device goes through the same interfaces the official HeadRush Remote
Editor uses, and is gated behind backup -> diff -> confirm.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]

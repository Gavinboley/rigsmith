"""The hardware loop: play a DI into the Core, record what comes back.

The offline renderer approximates the Core; this *is* the Core. A DI leaves
an audio interface, goes down a cable into the Core's guitar input, through
whatever rig is loaded, and comes back into an input - and then it can be
compared against anything: the record, an offline render of the same rig, a
previous pass with one knob moved.

Three ways to wire it (:mod:`.routes`):

* **analog** - interface output -> cable -> Core guitar input; Core output ->
  interface input. One device, one clock. The Core hears exactly what a
  guitar cable gives it, which is the point.
* **usb-reamp** - the Core's own USB audio, computer channel 3 into the rig and
  channels 1-2 back. All digital, one clock, no cables at all.
* **hybrid** - interface output into the Core's guitar input, recorded from
  the Core's USB. Two clocks, but channel 5 carries the DI exactly as the
  Core received it, which makes level calibration exact.

Every capture carries its own sync sweeps (:mod:`.signals`), found again on
the way back by GCC-PHAT, so latency and polarity are measured on every pass
rather than trusted from a calibration done an hour ago - USB audio stacks
are not obliged to start a stream with the same latency twice. Where the
output and input are different devices, the second sweep measures how far
their clocks drifted over the capture, and the recording is resampled to
undo it.

:mod:`.virtual` is the same loop with nothing plugged in: a simulated
interface and a simulated Core running an amp model, with latency, gain,
noise, polarity and clock drift - so all of this is tested, and the app can be
demonstrated, before any cable is connected.
"""

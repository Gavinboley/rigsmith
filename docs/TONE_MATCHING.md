# Tone matching: what the numbers mean, and what they don't

This document explains how `rigsmith` decides whether a rig sounds like the record — and, more importantly, where that judgement stops being trustworthy. If you read one section, read [The trap](#the-trap).

---

## The trap

Take a guitar recording. Shift it by 122 samples — about 2.5 ms, far too short to hear as a timing difference. Compare the shifted copy against the original using ESR, the error-to-signal ratio the Neural Amp Modeler community trains and validates against:

| Shift | ESR | MFCC distance |
|---|---|---|
| 61 samples | 2.014 | 0.00200 |
| 122 samples | **3.774** | 0.00221 |
| 245 samples | **0.005** | 0.00338 |
| 500 samples | 0.131 | 0.00293 |
| 1000 samples | 0.418 | 0.00545 |

Same audio every time. ESR swings across three orders of magnitude; the timbre metric barely moves.

The reason is arithmetic, not mystery. The fundamental here is 196 Hz — a period of about 245 samples at 48 kHz. Shift by 245 and the waveform realigns, so ESR reports a near-perfect match. Shift by 122 — half a period — and every peak lands on a trough, so ESR reports a catastrophe. The *sound* is identical in both cases.

By the usual reading of ESR (≈0.04 is good, >0.1 means something broke), that table says the same recording is simultaneously a perfect match and a total failure, depending on an offset nobody can hear.

Now consider what happens when the target is a commercial recording. It is a **different performance**, played by a different person, on a different guitar, with different pick attack, inside a mix. There is no correct offset to align to. Whatever ESR you compute is an artefact of an arbitrary alignment — and it will confidently rank your best candidate last.

This is the single easiest way to build a tone-matching tool that produces authoritative-sounding garbage.

## The two modes

`rigsmith` therefore splits comparison into two modes, and **the mode is a required argument, never inferred**:

### `ALIGNED`

Both signals are the *same performance*, time-aligned. In practice: your own DI, reamped through a candidate rig, compared against your own reference recording of that same DI.

Valid metrics:

- **ESR** — `sum((target - pred)²) / sum(target²)`. The metric this domain trusts. ≈0.04 is a good match; above 0.1 usually means something is genuinely wrong rather than merely different.
- **Multi-resolution STFT loss** — spectral convergence plus log-magnitude L1 across several FFT sizes, catching both transient and sustained differences. `auraloss` implements the reference version.

### `DISTRIBUTION`

The signals are different performances. This is the mode for anything compared against a record.

Valid metrics:

- **Spectral profile** — centroid, 85% rolloff, flatness, bandwidth, crest factor. Brightness, fizz, and compression, roughly.
- **MFCC distribution distance** — per-coefficient means and standard deviations, not frame-by-frame.
- **Log spectral distance** — RMS difference between time-averaged log spectra, in dB.

In this mode the sample-wise metrics are **not computed at all**. Not computed and hidden — not computed. `score_candidate` returns a result object with no `esr` entry, and `ComparisonMode.ALIGNED` additionally refuses signals whose lengths differ by more than 50%, since those cannot be the same performance.

The output of distribution mode is a **ranking**. The ordering is informative; the absolute numbers are not. Every rendered ranking says so.

## Your guitar is a variable too

A capture that matches a record through a maple-necked Strat may not match it
through a Les Paul, and no metric here can tell you which of those you own.
So the DI matters: render candidates through *your* playing on *your*
instrument, and the ranking answers the question you actually asked.

The library keeps those takes with the instrument attached - guitar, pickup,
tuning - because six months later the filename is a guess and the label is
not. A take id works anywhere a path does.

One rule the library enforces: only a **DI** can be rendered through a
candidate rig. Anything already through an amp - a loop off the pedal, a wet
USB capture - is a reference to match against, never a source to render from.
Rendering a wet signal through an amp model stacks two amps, which sounds
like neither and measures like nothing.

## The composite, and why its weights are scaled

Each mode blends several metrics into one number for ranking. Aligned mode
weights ESR at 0.5 because ESR is what this domain argues in; distribution
mode weights the spectral profile at 0.45.

Those weights are only real if the metrics are on the same scale, and they
are not. ESR is interesting between 0 and 0.1. The spectral profile distance
runs past 20 on pathological input. Measured on a realistic aligned pair, the
nominal "0.5 ESR" blend was **1% ESR and 64% spectral profile** - aligned mode
was a distribution score wearing a different hat.

So every term is clamped to the range over which it discriminates and divided
by it before weighting. ESR's range is 0.25, which is 2.5x the community's
"broken" figure, so the whole good-to-broken span sits inside the term rather
than in the first 2% of it. Each term now contributes at most its weight, the
composite lands in 0 to 1, and one wild candidate can no longer swamp the
blend.

The composite is still a ranking and not an absolute. Two candidates scored
against different targets, or in different modes, are not comparable.

## The three layers

### Layer 1 — offline simulation

Render a DI through a candidate NAM capture and cabinet IR in Python. Fast, needs no hardware, approximate. Use it to narrow a large field of captures to a shortlist.

Cabinet IR convolution is faithful — it is a plain linear convolution and matches the hardware closely. NAM inference is delegated to the reference `neural-amp-modeler` package rather than reimplemented, because a second subtly-different implementation of someone else's model is worse than no offline rendering at all: a capture that renders differently offline than on the device would silently corrupt every ranking.

### Layer 2 — hardware in the loop

The Core presents a 6-in/4-out USB interface:

| Channel | Content |
|---|---|
| 1–2 | stereo wet, XLR main outputs |
| 3–4 | stereo wet, 1/4" main outputs |
| **5** | **mono dry DI from the guitar input** |
| 6 | mono dry from the microphone input |

Channel 5 is what makes this work. Recording it alongside the wet channels means **one take** yields both a reference tone and the dry DI that produced it. That single performance can then be reamped through unlimited candidate rigs, each scored against the same reference — so trying more candidates costs compute, not playing time.

Round-trip latency is **measured**, by sending a short burst and cross-correlating the return, not assumed. Given the table at the top of this document, an uncompensated few-millisecond offset would wreck every aligned comparison.

This layer is the only one that produces the device's actual tone.

### Layer 3 — human blind A/B

The arbiter. Metrics rank; ears decide.

Candidates are relabelled with opaque ids (`X1`, `X2`, …) assigned from a shuffled order, left/right placement is randomised per trial, and the mapping is withheld until results are requested. Knowing which one is "the A2 capture the agent recommended" is enough to hear it as better.

Sessions persist to JSON, so listening can be spread over days, and verdicts carry optional confidence and comments that feed back into refinement.


Blinding hides the label, and that is only half of it. A clip served from
`jcm800-hot-offline.wav` announces itself in a terminal, in a URL bar, and in
the page's own DOM - so the clips are copied to opaque filenames when the
session is created, and the web UI serves them through
`/api/ab/<session>/audio/<blinded id>` rather than by path. A clip that could
not be copied stays in the test, because a missing side is worse than a named
one, but every surface says so: `ab_leaks()` reports which, and both the CLI
and the panel warn that the result is compromised.
## Getting a target to compare against

Source separation via Demucs `htdemucs_6s` — the only widely available open model with a first-class **guitar** stem (four-stem models fold guitar into "other"). It runs offline; expect minutes per track on CPU.

A separated stem is **not a DI and not ground truth**. It still carries the record's amp, cabinet, microphone, room, and mix processing — compression, EQ, saturation, bus glue — plus separation artefacts. It is a timbral reference for ranking, and `rigsmith` labels it as such everywhere it appears.

## The cab you did not mean to add twice

Most captures on TONE3000 are of a whole miked rig, not an amp. Putting a
cabinet IR after one of those sends the signal through two speakers, and the
result is boxy and dull in a way that is very hard to trace - it sounds like a
bad capture rather than a wiring mistake.

A `.nam` file often says which it is, in `metadata.gear_type`. When the
capture named by a NAM block is on disk, rigsmith reads it and says plainly
which of the two mistakes you are making, or confirms that a missing IR is
correct. When the file does not say, or the capture is not downloaded, it
says that instead of guessing - because "remove that IR" is bad advice for a
capture that genuinely needs one.

## Choosing the passage

A track is not one tone. The clean intro, the rhythm verse, and the solo are
often three different amp settings, sometimes three different amps. Scoring
the whole file averages them together, and the winner of that average can be
a capture that matches none of the three.

So every scoring surface takes a region:

```
rigsmith match score target.wav a.wav b.wav --region 41.2:58.0
score_candidates(..., target_region="41.2:58.0")        # MCP
drag across the waveform                                 # web UI
```

The two sides are separate — `--candidate-region`, `candidate_region` — because
the passage rarely sits at the same time code in both. The solo is at 2:41 of
the record and 0:04 of the DI you played.

In `ALIGNED` mode they must be the same span, and the code refuses anything
else. That mode compares sample against sample, so trimming the two signals
differently would shift one against the other — reintroducing exactly the
offset contamination the mode exists to avoid.

A region shorter than 0.25 s is refused outright: at a 2048-sample window
there is almost nothing left to average, and a number computed from two
frames looks as authoritative as one computed from two thousand.

## What is wrong, not just how wrong

A composite ranks. It cannot tell you what to change, and "0.031" is not an
instruction. So `rigsmith match explain` compares the two signals band by
band, in bands named the way an amp's controls are:

```
bright-capture against the target, biggest differences first:

  fizz       +5.9 dB  too much (6000-11000 Hz) - buzz on top of distortion
  presence   +2.4 dB  too much (1600-3200 Hz) - attack and cut
  low end    -1.8 dB  not enough (80-180 Hz) - weight and thump
```

Both signals are RMS-normalised first, so the answer is about tone rather
than about whichever was recorded louder. Differences under 1.5 dB are not
listed: roughly the point below which an A/B stops being obvious.

The readings attached to each band are conventional, and conventional is not
always right. A bright candidate might be the mic position, the cab, the
amp's presence control, or the fact that the target is a finished mix with
treble taken off it. Change one thing and measure again.

When everything sits inside 1.5 dB, it says so - and says that what is still
wrong is feel, dynamics and attack, which none of this measures.

## Practical workflow

```
1. isolate_guitar     record.wav            -> guitar stem (timbral reference)
2. search_captures    "Marshall JMP" A2     -> candidate captures
3. capture_di         play the riff once    -> your dry DI + wet reference
4. for each candidate:
     load the rig on the Core
     reamp_di          your DI              -> that rig's actual output
5. score_candidates   mode="distribution"   -> ranking vs the record's stem
                      target_region="2:41"     (the passage, not the track)
6. start_ab_test      top 3 candidates      -> blind listening
7. submit_ab_verdict  x N                   -> the verdict that counts
```

Step 5 ranks. Step 7 decides.

## What is deliberately not claimed

- No absolute "this is 87% accurate" figure. Against a record, no such number is meaningful.
- No inference of the comparison mode from the audio. The caller knows whether it is the same performance; the code cannot reliably tell, and guessing wrong is the failure this design exists to prevent.
- No claim that a metric win is a tone win. It is a shortlist, offered for listening.

## References

- ESR thresholds and NAM capture validation — [TONE3000 capture guides](https://www.tone3000.com/guides/capture-your-gear-with-nam-dry-wet)
- `auraloss`, the reference audio loss implementations — [csteinmetz1/auraloss](https://github.com/csteinmetz1/auraloss)
- Demucs and the six-stem model — [facebookresearch/demucs](https://github.com/facebookresearch/demucs)
- NAM A2 architecture — [TONE3000: NAM A2, the complete guide](https://www.tone3000.com/guides/nam-a2-the-complete-guide)

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

## Getting a target to compare against

Source separation via Demucs `htdemucs_6s` — the only widely available open model with a first-class **guitar** stem (four-stem models fold guitar into "other"). It runs offline; expect minutes per track on CPU.

A separated stem is **not a DI and not ground truth**. It still carries the record's amp, cabinet, microphone, room, and mix processing — compression, EQ, saturation, bus glue — plus separation artefacts. It is a timbral reference for ranking, and `rigsmith` labels it as such everywhere it appears.

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

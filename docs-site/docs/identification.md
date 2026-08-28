---
id: identification
title: How a track is identified
---

# How a track is identified

The set is **sampled**, not cut up. That distinction is the design.

## Why not look for the boundaries first

The obvious approach is to find where one record ends and the next begins, then
ask what each piece is. It does not work on a mixed set.

Spectral analysis finds *ruptures*, and a competent DJ never makes one.
Transitions are beatmatched across thirty-two to sixty-four bars, so the
detected boundary lands in the middle of the blend — the one place where two
records overlap and the fingerprinter can resolve neither.

So the order is inverted. Probe on a fixed cadence, ask what is playing at each
point, and let the boundaries fall out of where the answer changes. They become
a *result* rather than a hypothesis, unmatched stretches stay visible instead of
being swallowed, and every probe is independent, so the pass parallelises.

`--strategy spectral` keeps the old behaviour. It is genuinely better on
compilations and radio shows, where tracks *are* separated by hard cuts.

## What a probe is

A twelve-second window, pulled straight from the source with `ffmpeg -ss` and
handed over as bytes.

Twelve rather than more because the fingerprinter takes a **centred ten-second
window** of whatever it is given, in mono at 16 kHz. Sending it a whole
five-minute segment means decoding five minutes to use ten seconds of it — the
earlier version wrote segment files to disk to do exactly that.

`-ss` goes *before* `-i`, which selects input seeking: ffmpeg jumps to the
timestamp rather than decoding from zero. On a three-hour set that is the
difference between seconds and minutes.

## How often

| Input length | Cadence | Probes | Shortest track reported |
| --- | --- | --- | --- |
| under 2 min | every 5 s | ~12 | 4 s |
| under 5 min | every 8 s | ~22 | 6 s |
| under 10 min | every 12 s | ~25 | 10 s |
| under 30 min | every 25 s | ~51 | 20 s |
| under 90 min | every 35 s | ~118 | 20 s |
| under 3 h | every 45 s | ~160 | 20 s |
| longer | every 60 s | ~180 | 20 s |

A track runs three to eight minutes, so on a set this puts five to thirteen
probes inside one — several times what is needed to name it.

Short input is a different problem wearing the same clothes. Below about ten
minutes the thing is not a set: it is a reel, a radio edit, a promo cut, and
the tracks in it last seconds and stop dead between them. The set cadence does
not merely lose precision there, it loses whole tracks. A sixty-second clip of
four fifteen-second tracks got two probes, found two tracks, and gave half the
clip to a record that had stopped playing thirty seconds earlier — measured
with an identifier that answered correctly every time, so none of it was
Shazam's doing.

Being fine on short input costs nothing, because the input is short: twelve
probes for a minute against a hundred and eighteen for a set. The floor on how
short a reported track can be scales with it, for the same reason — twenty
seconds is right for a set, where anything briefer is a stray probe or a
sampled loop, and wrong for a reel, where a twelve-second snippet is the whole
point.

This is also why there is no cut detector. Hard cuts and beatmatched blends
would have to be told apart from the audio, and on the novelty curve at its
working smoothing they are indistinguishable — 1.9 against 1.9 by peak-to-median
ratio. Unsmoothed they separate by about 1.5×, on synthetic audio where the cut
is a clean discontinuity between two sine tones; real music, full of kicks and
breakdowns that spike the curve constantly, would erase that margin. The grid
handles hard cuts perfectly well once it is sampling finely enough, and
duration — known before a single sample is decoded — says when to.

An earlier version probed every twenty seconds, which put nine to twenty-four
inside each track. That is five-fold redundancy paid for in requests to a
service that rate-limits, and it was most of why a sixty-nine minute set once
took over two hours.

Density is not what makes identification good. Two things replaced it:
boundaries are refined against the novelty curve rather than snapped to the
nearest probe, and segments with thin evidence get extra probes aimed at them.
Starting sparse and adding where it matters beats blanketing the set.

## Turning probes into tracks

**Merge.** Consecutive probes naming the same track become one segment.

**Bridge.** An unidentified gap with the same track on both sides is closed.
Fingerprinting a mix fails constantly and unevenly — breakdowns, filter sweeps,
overlapping records, passages the database does not have. Without bridging, a
set where a quarter of probes match comes apart: one record appeared three
times as thirty-second fragments and read as three plays.

Only unidentified gaps are crossed, and only up to four minutes. A different
track in between means the record really did change; a longer silence means
two plays rather than one.

**Confirm.** Segments resting on too little evidence get extra probes, placed
as far as possible from the ones already taken — a probe next to an existing
one re-reads the same audio and confirms nothing. They can overturn the result:
if the newcomers agree with each other rather than with the original, the
majority takes the segment.

**Refine.** Boundaries are placed at the strongest change in the novelty curve
between the two probes that disagree — an easy local question, unlike finding
one blind.

## Saying how sure it is

Two numbers, answering different questions.

| | Measures |
| --- | --- |
| **Confidence** | Share of **all** probes in the segment that named this track |
| **Agreement** | Share of the probes that **named anything** which agreed |

The difference matters more than it sounds. **Silence is not dissent.** A probe
that came back empty says nothing about whether the track is right — it says
fingerprinting failed there. Counting it against the track makes a record
established across seven minutes read as contested.

`strength` is what the interface shows, and it combines the two:

| Strength | Means |
| --- | --- |
| `strong` | Three or more probes agreed, none dissenting |
| `medium` | Two agreed, or three with one dissenting |
| `weak` | One probe, or a contested majority |

A single probe reports 100% confidence because it agrees with itself. That is
the weakest possible finding wearing the strongest possible number, which is
why `strength` exists and why the interface leans on it rather than the
percentage.

Only doubtful tracks are marked. Marking everything would turn the signal into
wallpaper; what a digger needs to see is which findings not to trust.

## What it cannot do

A white label with no fingerprint anywhere comes back as `ID ?`, and that is
the correct answer.

On experimental and unreleased material the hit rate is low — a recent Irène
Drésel set matched 28 probes out of 118. Probing harder does not help: the
failures are "this is not in the database", not "we did not look often enough".
Those stretches are exactly what a digger is looking for, so they stay in the
tracklist with their timestamps rather than being hidden.

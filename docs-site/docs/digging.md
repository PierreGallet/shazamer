---
id: digging
title: Digging
---

# Digging

The library is what makes this more than a converter. A tracklist you download
and forget is a one-shot; a library accumulates.

## Tracks that keep coming back

The strongest signal in the whole tool: **this track appears in four of your
sets**. It falls straight out of storing tracks rather than files, and it is
usually how you find the records that matter to the DJs you follow.

Shown at the top of the library, ordered by how many sets a track turns up in.

## The crate

Star a track and it collects in the crate, filterable by the things you
actually sort on — BPM range, Camelot key, label, free text.

## Following channels

Follow a YouTube channel, a SoundCloud artist or a Mixcloud series, and new
uploads are analysed without being asked. It runs every six hours.

Two restraints, both deliberate:

- **The first check records without analysing.** A channel with years of back
  catalogue is not a reason to start forty analyses; the point of following is
  what appears next.
- **At most three per round.** An analysis takes an hour of a shared machine;
  a dozen at once would block everything else for most of a day. The rest are
  found again next time.

## What matching is based on

Everything — recurrence, the crate, enrichment, downloads — keys on a
normalised `artist::title`, which collapses the decorations that make one
record look like several: `(Original Mix)`, `- Extended`, `[Remastered 2019]`,
`feat.` credits, accents, punctuation.

It deliberately does not collapse remixes. `Track (Skee Mask Remix)` is a
different record from `Track`, and treating them as one would quietly merge
two things you own separately.

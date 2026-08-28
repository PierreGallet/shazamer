---
id: intro
title: What Shazamer is
# Makes this the site root, since docs are served at baseUrl.
slug: /
sidebar_position: 1
---

# What Shazamer is

Shazamer takes a mix — a URL or a file — and gives you a timestamped tracklist
with BPM and key, drawn on a waveform you can scrub through. Every track links
out to where you can buy it, and everything you analyse collects in a library
you can dig through afterwards.

It is built for one person: a DJ who listens to sets and wants to know what is
in them.

## What it does that a plain fingerprinter does not

**It keeps the gaps.** A stretch nobody can identify stays in the tracklist
with its timestamp, marked `ID ?`. Dubs, edits and unsigned promos are the
point of digging, and the tool that hides them is hiding the interesting part.

**It says how sure it is.** A track found by one probe and a track confirmed by
four are not the same finding, and the interface says which is which. See
[Identification](identification).

**It remembers.** A track appearing across four of your sets is the strongest
signal there is, and it falls straight out of having a library rather than a
pile of text files.

**It points at the record.** Label, catalogue number and year are looked up
after the analysis, so you have something to search a shop with — not just a
title.

## What it will not do

It will not find everything. A white label with no fingerprint anywhere will
come back as `ID ?` no matter how long you look at it, and that is the correct
answer rather than a bug.

It will not replace listening. The confidence scoring exists precisely because
some answers are shaky, and a shaky answer presented confidently is worse than
no answer.

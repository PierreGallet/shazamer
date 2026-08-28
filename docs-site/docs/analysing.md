---
id: analysing
title: Analysing a set
---

# Analysing a set

Paste a link or drop a file. The set is playable and the tracklist complete
before anything else happens; labels arrive a minute or two later.

## What you get

A **waveform** with a ribbon along the top: one block per detected segment,
numbered when identified, dashed and hollow when not. Click to seek, scroll to
zoom, shift-drag to pan. The tracklist below follows the playhead.

A **tracklist** with timestamp, artist, title, label, catalogue number, BPM and
Camelot key — and rows for the stretches nobody could name.

## Reading the confidence

Only doubtful tracks are marked. A finding with no badge is solid; marking
everything would turn the signal into wallpaper.

| Badge | Means |
| --- | --- |
| *(none)* | Three or more probes agreed |
| `likely` | Two probes agreed, or three with one dissenting |
| `unsure` | One probe only, or a contested majority |

`unsure` usually means a short track, or one sitting across a transition where
two records overlap. It is a prompt to listen, not a failure.

## Following an analysis

An analysis runs on the server, not in your browser: close the tab, shut the
laptop, it carries on. The header carries a pill with live progress from
wherever you are, and `/analyzing/<id>` is a real address — reloading
re-attaches to it.

When it finishes, the header shows how long it took. Clicking that opens a
breakdown per stage, which is the fastest way to see whether a slow run was
slow at decoding, at identifying, or somewhere else entirely.

## Exports

| Format | For |
| --- | --- |
| **Rekordbox XML** | A playlist with BPM and key already filled in. Traktor and Serato import it too. |
| Text | The classic tracklist, the kind people paste under a mix |
| CSV | Every field, for a spreadsheet |
| M3U | A playlist file |
| JSON | Everything, raw |

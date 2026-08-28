---
id: pipeline
title: The pipeline
---

# The pipeline

Six stages. The set is complete and usable at the end of the fifth; enrichment
follows separately.

| Stage | Progress | What happens |
| --- | --- | --- |
| Reading | 2% | Duration, without decoding a sample |
| Downloading | 1–13% | Only for a URL |
| Decoding | 5–35% | Stream the audio, accumulate features |
| Identifying | 36–80% | Probe the grid, in parallel |
| Merging | 81% | Probes become segments |
| Confirming | 82–87% | Extra probes where evidence is thin |
| BPM and key | 88–95% | Per track, from a stable window |

Ranges live in one place as named constants. A stage added between two others
once made the bar run backwards, which reads as a bug even when the work is
fine.

## Decoding, and why it streams

ffmpeg decodes to a pipe and blocks of thirty seconds are consumed one at a
time. Only the feature vectors are kept — about 1.2 MB per hour of audio.

The version this replaced loaded the whole file and took its STFT in one call.
Measured, that cost **68.9 MB per minute of audio**, which crosses a 6 GB
container at one hour twenty-seven — while the guard admitted sets up to two
hours. Sets passed validation and then died.

The streaming result is bit-identical to the one-shot computation. Frames carry
across block boundaries, so nothing is analysed twice or dropped.

Feature extraction runs in a thread. Awaited inline it pins the event loop for
nearly the whole run: the server stops answering, the health probe times out,
and a working container is killed.

## Identifying, and what bounds it

Probes run concurrently, bounded by a semaphore that covers **extraction as
well as identification**. Only guarding the network call meant one ffmpeg per
probe was spawned up front, all alive at once — about 95 processes for a
half-hour set and 430 for a three-hour one, which is how a container was
OOM-killed.

Concurrency defaults to four. Measured against the live service, throughput
plateaus around three probes per second whatever is asked for: two in parallel
and eight in parallel move the same number. The extra slots buy nothing and are
what tips the service into refusing.

A refusal is retried, not recorded as "no match". Under load the service stops
returning JSON and serves something else; treated as an answer, that
manufactured gaps — one run filed 113 of 206 probes as unidentified because it
could not ask, not because there was nothing there.

Retries live in one place. The client library retries twenty times with a
sixty-second ceiling, and four attempts layered on top gave a worst case of
eighty minutes for a single probe. Each probe now has a hard timeout, so one
stalled request cannot hold a slot for ever.

## What it costs

A sixty-nine minute set, measured end to end:

| Stage | Time | Share |
| --- | --- | --- |
| Decoding | 211 s | 31% |
| Identifying | 284 s | 41% |
| Confirming | 109 s | 16% |
| BPM and key | 86 s | 13% |
| **Total** | **690 s** | |

The same set took over two hours before the fixes above.

Every analysis records this breakdown, shown behind the elapsed time in the set
header. The stages are unequal and which one dominates moves with the set and
the service — from outside, a run that spent two hours identifying looks
exactly like one that split its time evenly, and that difference is the whole
diagnosis.

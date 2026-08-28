---
id: pipeline
title: The pipeline
---

# The pipeline

Seven stages, one of them conditional. The set is complete and usable once
key and BPM are in; enrichment follows separately.

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

Concurrency defaults to four. It was eight, from a measurement that no longer
holds: throughput used to plateau around three probes per second whatever was
asked for, so extra slots were merely useless. They are not merely useless now.
Every parallel slot is another request feeding a rate limit that then pauses
*all* of them, and on a seventy-five minute set the refusals cost more
wall-clock than the parallelism saved.

A refusal is not an answer, and the two must not be confused. "No match" is
real, common and useful — it is how a dub or an unsigned edit shows up. "The
service would not talk to us" is not an outcome at all, and filing it as one
manufactures gaps in the tracklist.

Shazam answers a rate-limited request with `HTTP 429` and a 142-byte HTML
page. The client library funnels every non-JSON body into one
`FailedDecodeJson`, so the failure worth treating specially arrived disguised
as a parsing problem — and the code did the worst thing available with it,
retrying inside the library *and* outside it, eight requests for one refused
probe. One run lost 85 of its 128 probes that way, and the retries were part
of why.

Rate limiting is now its own exception, and the backoff belongs to the
**service** rather than to whichever probe discovered the problem. Per-probe
backoff cannot work with four probes running: the one that sleeps wakes into a
limit the other three have been feeding. One refusal pauses everyone, a
simultaneous wave counts once, and success walks the penalty back down. The
progress line says when it is waiting and for how long, because a run sitting
out a limit exactly as designed is otherwise indistinguishable from a hung one.

Waiting it out is the right answer for a tool you leave running, so refusals
get a longer and separate budget from ordinary retries — being turned away is
not a failed attempt at an answer. Probes still lost to a limit are counted,
not folded silently into the tracklist.

Retries live in one place. The client library retries twenty times with a
sixty-second ceiling, and four attempts layered on top gave a worst case of
eighty minutes for a single probe. Each probe now has a hard timeout, so one
stalled request cannot hold a slot for ever.

## What it costs

A sixty-nine minute set, measured end to end on a quiet service:

| Stage | Time | Share |
| --- | --- | --- |
| Decoding | 211 s | 31% |
| Identifying | 284 s | 41% |
| Confirming | 109 s | 16% |
| BPM and key | 86 s | 13% |
| **Total** | **690 s** | |

The same set took over two hours before the fixes above.

A seventy-five minute set measured while Shazam *was* rate-limiting, for
contrast — same code, 32 shared pauses, no probe lost:

| Stage | Time | Share |
| --- | --- | --- |
| Decoding | 361 s | 28% |
| Identifying | 374 s | 29% |
| Confirming | 444 s | 34% |
| BPM and key | 122 s | 9% |
| **Total** | **1303 s** | |

Confirmation overtakes identification there, because its extra probes pay the
same pauses. The run before the rate-limit fix finished sooner and was worth
much less: 85 probes lost, 13 records found instead of 32, 35% of the set
covered instead of 64%. Finishing fast is not the goal.

Every analysis records this breakdown, shown behind the elapsed time in the set
header. The stages are unequal and which one dominates moves with the set and
the service — from outside, a run that spent two hours identifying looks
exactly like one that split its time evenly, and that difference is the whole
diagnosis.

---
id: lessons
title: What went wrong, and why
---

# What went wrong, and why

A record of the failures that shaped this codebase. Kept because most of them
share a shape, and the shape is more useful than the individual fixes.

## Silence read as an answer

The recurring one. Four times, in four different places, "I could not ask" was
recorded as "there is nothing there":

- **Shazam** returns non-JSON under load. Caught and filed as no-match, it
  blanked 113 of 206 probes in one run — the tracklist showed gaps that were
  never gaps.
- **MusicBrainz** answers a busy server with HTTP 200 and an error body.
  Treated as "not found", it recorded tracks as having no label, and did it
  more often the busier the service was.
- **An invalid `inc` parameter** on a MusicBrainz recording lookup returns 200
  with the releases silently dropped, which reads as "this recording has no
  releases". Every lookup came back with an empty label.
- **Silent probes inside a segment** were counted as disagreement rather than
  as nothing said, making a track established across seven minutes read as
  contested.

The pattern: a failure that returns a *shape* rather than an error is more
dangerous than one that raises, because it looks like data. Every one of these
produced confidently wrong output rather than a visible failure.

## Protections removed without noticing

Rewriting a component while keeping what was visible and losing what was not:

- A `run_in_executor` with a comment explaining it kept CPU work off the event
  loop. The streaming rewrite kept the memory optimisation it sat next to and
  dropped this one.
- A `finally` that deleted failed downloads. Without it, seven failed attempts
  at one set left 503 MB behind.
- A semaphore placed around the network call rather than around the whole
  operation, so process spawning went unbounded.

All of these were implicit. The audit that found the last two took three
commands — diffing the old module against the new and listing every guard —
and would have caught all of them if run before calling the refactor done.

## Measuring one thing and concluding about another

- `tracemalloc` measures Python allocations. Used to size a container, it was
  an order of magnitude low and production was OOM-killed.
- Feature extraction was measured and found flat. The conclusion drawn — that
  the pipeline was flat — skipped the stage that actually scaled.

## Retries that multiplied

The fingerprinting library retries twenty times with a sixty-second ceiling.
Four attempts added on top gave a worst case of eighty minutes for one probe,
and an analysis hung for half an hour with the CPU idle — waiting inside a
library that was waiting.

Check whether the thing you are wrapping already retries.

## Deployment reporting success while doing nothing

- The force-recreate named only the API, so the worker kept whatever code it
  started with. A fix written specifically to unstick a hung analysis deployed,
  reported success, and never reached the process running it.
- The `.env` was rewritten wholesale, so anything set by hand survived until the
  next push.
- A config block that read an environment variable before the `.env` was loaded
  failed its own guard and wrote nothing. A skipped conditional is not an error.
- A health probe asked the worker about an HTTP port only the API serves. It
  failed forever and Swarm killed a healthy container in a loop.

Every one of these was silent. The post-deploy check now verifies replica
counts for both services, because "a healthy container exists" was true in
several of these cases and meant nothing.

## Tests that passed for the wrong reason

A test simulating a stalled request with `asyncio.sleep` — while the fixture
replaced `asyncio.sleep` to keep backoff instant. The stall never happened.

When a test passes immediately after being written, check that it can fail.
Several of the tests here were verified by reintroducing the bug and watching
them break.

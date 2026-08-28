---
id: architecture
title: Architecture
---

# Architecture

Three processes and a broker.

```
  browser ──▶ app (FastAPI)  ──enqueue──▶  redis  ──▶  worker
                   │                                     │
                   └────── SQLite library ◀───────────────┘
                           shared volume
```

**app** answers requests and serves the interface. It analyses nothing, so it
stays responsive while a set is being decoded.

**worker** runs the analyses, the enrichment and the downloads. One at a time:
each already saturates several cores, and two would slow both.

**redis** is the broker and nothing else. Task state lives on the volume both
containers mount, so there is one source of truth rather than two copies to
reconcile.

**slskd** sits alongside when Soulseek is configured, on the internal network
only.

## Why the split

Analyses used to run inside the API with `asyncio.create_task`. Two things went
wrong with that, both in the same afternoon.

Every deploy killed whatever was in flight. A sixty-nine minute set is long
enough to be near-certain to meet one, and three were lost in a row.

And the CPU-bound work starved the API. The health probe timed out, Swarm
concluded the container was sick, and killed a container that was working
perfectly — taking the analysis with it.

## The code

| | |
| --- | --- |
| `core/audio.py` | ffmpeg: streaming decode, probe extraction, duration |
| `core/features.py` | Spectral centroid, RMS, BPM, key — incremental |
| `core/segment.py` | Probe grid, merging, bridging, confidence |
| `core/pipeline.py` | The stages, in order, with progress and timings |
| `identify/` | Fingerprinting. `shazam.py` today; the protocol admits others |
| `enrich/` | MusicBrainz lookups and the cache |
| `acquire/` | Soulseek: ranking, transfers, verification, tagging |
| `sources/` | yt-dlp, and the quality ladder per platform |
| `store/library.py` | SQLite: sets, tracks, crate, watches, downloads |
| `jobs/` | The queue, the worker, the scheduled checks |
| `web/` | TypeScript + SolidJS interface |

Each of `identify`, `enrich` and `acquire` is a protocol with one
implementation. Adding a second provider is a file, not a refactor.

## Choices worth knowing

**SQLite, not Postgres.** No server to run, the file sits next to the audio, and
it backs up by being copied. Reads and writes are dispatched to a thread so the
event loop is never blocked, and WAL lets the API read while the worker writes.

**arq, not Celery.** The pipeline is async throughout, so a job *is* a
coroutine and the worker calls the same function the API used to. Celery is
synchronous-first and would have needed a bridge for nothing. The trade is
familiarity: Celery is far better known. Swapping would touch `jobs/` only —
nothing above it knows which queue is calling.

**SolidJS, not React.** The waveform needs a playhead at 60 fps against a track
list that must not re-render. Fine-grained reactivity is exactly that shape.

**Python for the analysis.** librosa, numpy and shazamio have no equivalent
elsewhere, and the fingerprinter is already Rust under the hood. Rewriting the
glue around it would gain nothing on a workload dominated by network waits.

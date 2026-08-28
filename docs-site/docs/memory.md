---
id: memory
title: The memory rewrite
---

# The memory rewrite

The app fell over on long sets. The cause was structural rather than a tuning
problem, and the measurements are worth keeping because the conclusion was not
the obvious one.

## What it cost

`librosa.load()` materialised the whole file and `spectral_centroid` took its
STFT in one call. Both scale with duration. Measured with `tracemalloc` across
four durations, linearity R² = 0.996:

| Path | MB per minute | 1 h | 2 h | Crosses 6 GB at |
| --- | --- | --- | --- | --- |
| `n_fft=2048` — the deployed web path | 68.9 | 4.1 GB | 8.3 GB | **1 h 27** |
| `n_fft=1024` — the CLI path | 37.1 | 2.2 GB | 4.5 GB | 2 h 42 |
| Streaming — now | 0, flat | 24 MB | 24 MB | never |

Two things stand out.

The web and CLI paths had **drifted**. A progress subclass re-implemented
boundary detection and dropped the parameters that halved STFT memory, so the
deployed path used twice the RAM its own comments described.

And the guard was **mis-calibrated**: a two-hour cap on a path that gave out
around one hour twenty-seven. Sets passed validation and then died.

Reproduce with `PYTHONPATH=. python docs/measure_memory.py`.

## What that 24 MB is, and what it is not

A `tracemalloc` peak: Python-level allocations for the feature stage alone.
**Not the container's footprint**, and treating it as one was expensive.
Measured on the running production container mid-analysis:

| | |
| --- | --- |
| Fresh interpreter with numpy, scipy, librosa | 33 MB |
| The app under analysis (RSS) | ~450 MB |
| One ffmpeg subprocess | ~50 MB |

The gap is librosa's working buffers, the executor thread, and arenas the
allocator never returns. All still flat with duration — the shape of the claim
holds — but the absolute number is an order of magnitude higher.

The container limit was set to 1 GB from the 24 MB figure and production was
OOM-killed. **Size a memory limit from RSS under real load, never from
`tracemalloc`.**

## Bounding work, not just memory

Streaming fixed how much audio is held at once. Two other resources scale with
set length and had to be bounded separately.

**Subprocesses.** `extract_probe` spawns one ffmpeg per probe, and the
identifier's semaphore only guarded its network call — so gathering over every
probe started them all at once. About 95 processes for a half-hour set, 430 for
a three-hour one. The bound now covers extraction and identification together.

**The event loop.** Feature extraction is librosa doing an STFT per block,
hundreds of milliseconds of solid CPU, with blocks arriving as fast as ffmpeg
can decode. Awaited inline it pins the loop thread for nearly the whole run: the
server stops answering and the health probe kills a container that is working
correctly. It runs in a thread — which the pre-1.0 code did, with a comment
saying why, and the streaming rewrite dropped.

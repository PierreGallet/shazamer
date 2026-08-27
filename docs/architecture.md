# Design notes

## Why the pipeline streams

The previous implementation called `librosa.load()` on the whole file, then
handed the result to `spectral_centroid` in one call. Both scale with duration.

Measured with `tracemalloc` on this project's venv (librosa 0.10.2, numpy
2.1.2), across four durations from 2 to 20 minutes. Figures include the decoded
signal, not just the STFT:

| Path | MB per minute of audio | 1 h | 2 h | Crosses the 6 GB container at |
| --- | --- | --- | --- | --- |
| `n_fft=2048` (what the web path used) | 68.9 | 4.1 GB | 8.3 GB | **1 h 27** |
| `n_fft=1024` (what the CLI used) | 37.1 | 2.2 GB | 4.5 GB | 2 h 42 |
| Streaming (now) | 0 — flat | 24 MB | 24 MB | never |

### What that 24 MB is, and what it is not

It is a `tracemalloc` peak: Python-level allocations for the feature stage
alone. **It is not the container's footprint**, and treating it as one was an
expensive mistake. Measured on the running production container mid-analysis:

| | |
| --- | --- |
| Fresh interpreter, numpy + scipy + librosa imported | 33 MB |
| The app under analysis (RSS of the uvicorn process) | ~450 MB |
| One ffmpeg subprocess | ~50 MB |

The gap is librosa's working buffers, the executor thread, and arenas the
allocator never hands back. All of it is still flat with duration — the shape
of the claim holds — but the absolute number is an order of magnitude higher.

The container limit was set to 1 GB from the 24 MB figure and production was
OOM-killed. **Set a memory limit from RSS under real load, never from
`tracemalloc`.** `docs/measure_memory.py` reports the tracemalloc comparison
because that is what isolates the algorithmic change; sizing the container is a
separate question with a separate measurement.

Two things stand out. The web path and the CLI path had drifted: a progress
subclass re-implemented boundary detection and dropped the parameters that
halved STFT memory, so the deployed path used twice the RAM of the one the
comments described. And the guard was mis-calibrated — a 2 h cap on a path that
gave out around 1 h 27, so sets passed validation and then died.

Streaming removes the class of problem rather than tuning it. What grows with
duration is now the block size (30 s → 2.6 MB) plus the feature vectors
(~1.2 MB per hour), and nothing else — the rest is a fixed cost paid whatever
the set length.

Reproduce with `./venv/bin/python docs/measure_memory.py`.

## Why probes beat boundary detection

Spectral centroid and RMS find *ruptures*. A competent DJ never makes one:
transitions are beatmatched across 32 to 64 bars, so a detected boundary tends
to land in the middle of the blend — the one place where two tracks overlap and
the fingerprinter is least able to resolve either.

Probing on a fixed cadence and merging consecutive matches inverts this.
Boundaries become a result rather than a hypothesis, unmatched stretches stay
visible instead of being swallowed, and every probe is independent, so the pass
parallelises perfectly. The novelty curve is still computed — but only to
*refine* a boundary once two probes have established that one exists between
them, which is a far easier question than finding it blind.

`--strategy spectral` keeps the old behaviour, which is genuinely better on
compilations and radio shows where tracks *are* separated by hard cuts.

## Why only 12 seconds per probe

`shazamio_core`'s Rust recognizer takes a **centred 10-second window** of
whatever it is given, converted to mono 16 kHz. The previous code wrote whole
segments — 90 s to 5 min of WAV, 4 to 13 MB each — which shazamio then decoded
in full to use a tenth of. A 12 s window extracted with `ffmpeg -ss` before
`-i` (input seeking, so ffmpeg jumps rather than decoding from zero) removes the
write, the read and the wasted decode, and is handed over as bytes so there is
no temp file to clean up or leak.

## Confidence

The old score counted entries in Shazam's `matches` array, which reflects how
many internal fingerprint hits came back — not how sure the answer is. It is now
the share of probes within a segment that agreed on the winning track, which is
a real signal because the probes are independent.


## Bounding work, not just memory

Streaming fixed how much audio is held at once. Two other resources scale with
set length and had to be bounded separately — both found in production, both
now covered by tests that fail if they regress.

**Subprocesses.** `extract_probe` spawns one ffmpeg per probe. The identifier's
semaphore only guards its HTTP call, so gathering over every probe started them
all at once: ~95 processes for a 30-minute set, ~430 for a three-hour one, and
the container was OOM-killed. The bound now covers extraction and
identification together.

**The event loop.** `features.push()` is librosa doing an STFT per block —
hundreds of milliseconds of solid CPU — and blocks arrive as fast as ffmpeg can
decode. Awaited inline it pins the loop thread for nearly the whole run: the
server stopped answering, the healthcheck timed out, and Swarm killed a
container that was working correctly. It runs in a thread now, which is what
the pre-1.0 code did and the streaming rewrite dropped.

The healthcheck was implicated too. It spawned a Python interpreter and
imported urllib — 1.8 s at idle, paid out of the same CPU quota the analysis is
saturating. A probe that is merely slow is not evidence of a sick process; only
sustained silence is. See `scripts/healthcheck.py`.

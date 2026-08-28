---
id: running
title: Running it
---

# Running it

## What you need

- **Python 3.12.** shazamio does not work on 3.13 or later.
- **ffmpeg.** Every decode, seek and probe goes through it. It is the one hard
  system dependency.
- **Node 18+**, to build the interface.
- **Redis**, if you want analyses to survive a restart. Optional — see below.

```bash
brew install ffmpeg node redis          # macOS
sudo apt-get install ffmpeg nodejs redis # Debian/Ubuntu
```

## Getting started

```bash
make install     # Python environment and frontend dependencies
make web         # builds the interface and serves everything on :8000
```

For development, `make dev` runs the API on `:8000` and Vite with hot reload on
`:5173`, proxying `/api` through.

## With or without a queue

Analyses run in a worker process when `REDIS_URL` is set, and in the API
process when it is not.

The difference is what happens when something restarts. Without a queue, an
analysis in flight is lost — which for a three-hour set means an hour of work
gone to a deploy. With one, the job is handed back and picked up again.

```bash
make worker      # the analysis worker; needs REDIS_URL
```

The fallback is deliberate rather than accidental: it keeps development and the
test suite working without Redis, and keeps the app degraded rather than broken
if Redis goes down. The log says which path was taken.

## From the command line

```bash
make analyze FILE="~/Music/dj_set.mp3"

./venv/bin/python -m src.shazamer mix.mp3 \
    --strategy grid --interval 30 --concurrency 4 \
    --formats json,txt,rekordbox
```

| Option | |
| --- | --- |
| `--strategy grid` | Probe on a cadence and merge. The default; better on mixed sets. |
| `--strategy spectral` | Detect boundaries first. Better on hard-cut compilations. |
| `--interval` | Seconds between probes. Derived from set length by default. |
| `--concurrency` | Parallel identification requests. |
| `--probe-duration` | Seconds per probe; the fingerprinter uses a centred 10 s of it. |
| `--no-musical-features` | Skip BPM and key detection. |
| `--formats` | `json,txt,csv,m3u,rekordbox` |

## Configuration

Copy `.env.example` to `.env`. Everything has a working default; the ones worth
knowing are documented in that file, with the reasoning next to each.

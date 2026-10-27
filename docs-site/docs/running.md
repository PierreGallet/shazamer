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

For development, `make run` runs the API on `:8000` and Vite with hot reload on
`:5173`, proxying `/api` through.

## With Docker

```bash
cp .env.example .env     # then fill in the mail settings below
docker compose up -d     # API, worker and Redis on http://localhost:8000
```

The Compose file has the same shape as production: an API, a worker that
runs the analyses, and Redis between them. It reads your `.env`, and still
starts without one.

## Signing in needs mail

Accounts are passwordless: you sign in with a one-time code sent by email.
So an instance that cannot send mail cannot sign anyone in, and the sign-in
form says so instead of waiting for a code that will never arrive. Set
`SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD` and `MAIL_FROM` in `.env`. The file
lists providers that work and the port for each.

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

Soulseek stays off unless `SLSKD_URL` is set. It needs your own account and a
shared folder in return — the network runs on reciprocity — which is why the
purchase links are the default path and this is opt-in.

## Tests

```bash
make test              # the suite, no network
make test-integration  # hits YouTube and SoundCloud for real
make lint              # frontend typecheck
```

The suite runs against synthetic audio with a stub identifier: what is being
verified is segmentation, merging and the streaming maths, not that Shazam's
servers are reachable.

## Importing pre-1.0 tracklists

Before 1.0 results were loose JSON files in `outputs/`, listed by scanning the
directory. The library reads SQLite instead, so those files became invisible —
still on disk, nothing reads them. Bring them across:

```bash
PYTHONPATH=. python scripts/import_legacy.py outputs --dry-run
PYTHONPATH=. python scripts/import_legacy.py outputs --min-tracks 5
```

They arrive keyed the same way as new sets, so an imported track links up with
the same track found later. What cannot come across, because it was never
recorded: waveform, BPM and key; unidentified stretches, which the old pipeline
dropped; and repeats, which it deduplicated before saving. Re-analyse a set to
get those. `--min-tracks` keeps one-track leftovers from testing out of the
library; whatever it excludes is listed by name.

# Shazamer 🎛️

> Identify every track in a DJ set, see it on the waveform, and go find the records.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![CI](https://github.com/pierregallet/shazamer/actions/workflows/ci-cd.yml/badge.svg)](https://github.com/pierregallet/shazamer/actions)

Shazamer takes a mix — a URL or a file — and gives you a timestamped tracklist
with BPM and key, drawn on an interactive waveform you can scrub through. Every
track links out to where you can actually buy it, and everything you analyse
collects in a library you can dig through afterwards.

## What it does

- **Streams, never loads.** Audio is decoded through ffmpeg block by block, so
  peak memory is flat regardless of set length — measured at ~24 MB whether the
  set is 2 minutes or 6 hours. There is no duration limit.
- **Probes in parallel.** Identification is I/O bound, so it runs concurrently
  instead of one segment at a time behind a rate limiter.
- **Finds boundaries by result, not by guess.** Probing on a fixed cadence and
  merging matching neighbours beats hunting for transitions in a beatmatched
  set, where the "boundary" lands in the middle of a 32-bar blend.
- **Keeps the gaps.** A stretch nobody can name stays in the tracklist with its
  timestamp. Dubs, edits and unsigned promos are the point of digging.
- **BPM and Camelot key** per track, so the tracklist is usable at the decks.
- **Exports to Rekordbox XML**, plus M3U, CSV, text and JSON.
- **Remembers.** Sets land in a SQLite library; a track appearing across several
  of them is surfaced, which is the strongest digging signal there is.
- **Points at the record.** Bandcamp, Beatport and Discogs first; Soulseek via
  [slskd](https://github.com/slskd/slskd) if you configure your own instance.

## Requirements

- **Python 3.12** — shazamio is incompatible with 3.13+
- **ffmpeg** — every decode, seek and probe goes through it
- **Node 20+** — to build the frontend

```bash
brew install ffmpeg node          # macOS
sudo apt-get install ffmpeg nodejs # Debian/Ubuntu
```

## Install and run

```bash
make install     # Python venv + frontend dependencies
make web         # builds the UI and serves everything on :8000
```

For development, `make dev` runs the API on `:8000` and Vite with hot reload on
`:5173`, proxying `/api` to the backend.

### Command line

```bash
make analyze FILE="~/Music/dj_set.mp3"

# or directly, with options
./venv/bin/python -m src.shazamer mix.mp3 \
    --strategy grid --interval 20 --concurrency 8 \
    --formats json,txt,rekordbox
```

| Option | Meaning |
| --- | --- |
| `--strategy grid` | Probe on a cadence and merge (default; best on mixed sets) |
| `--strategy spectral` | Detect boundaries first (better on hard-cut compilations) |
| `--interval` | Seconds between probes (default: derived from set length) |
| `--concurrency` | Parallel identification requests (default 8) |
| `--probe-duration` | Seconds per probe; Shazam fingerprints a centred 10 s of it |
| `--no-musical-features` | Skip BPM and key detection |
| `--formats` | `json,txt,csv,m3u,rekordbox` |

## The waveform

Click to seek, scroll to zoom, shift-drag to pan. The ribbon along the top is
one block per segment: numbered and filled when identified, dashed and hollow
when not. The played portion of the envelope is lit, and the tracklist below
follows the playhead.

## How it works

```
URL ──▶ yt-dlp ──▶ ffmpeg (30 s blocks) ──▶ spectral centroid + RMS
                                                     │  ~3 MB of vectors
                                                     ▼
                            probe grid ──▶ ffmpeg -ss (12 s) ──▶ Shazam ×8
                                                     │
                                    merge ──▶ segments ──▶ BPM + key ──▶ library
```

The design notes behind this — including the measurements that motivated the
streaming rewrite — are in `docs/`.

### Audio quality

The download ladder never transcodes upward. YouTube's ceiling is Opus at
~160 kbps; re-encoding that to "320 kbps MP3" produces a bigger file carrying
strictly less information, so the native stream is kept. SoundCloud is the
opposite case: when the uploader enabled downloads, yt-dlp exposes the original
file — often WAV-sourced — and that is requested first.

## Configuration

Copy `.env.example` to `.env`. Everything has a working default; the ones worth
knowing:

| Variable | Default | Purpose |
| --- | --- | --- |
| `SHAZAM_CONCURRENCY` | `8` | Parallel identification requests |
| `KEEP_AUDIO_DAYS` | `14` | How long set audio is kept for playback |
| `ALLOWED_ORIGINS` | localhost | Browser origins allowed to call the API |
| `SLSKD_URL` / `SLSKD_API_KEY` | empty | Enables Soulseek acquisition |

Soulseek stays off unless `SLSKD_URL` is set. It needs your own account and a
shared folder in return — the network runs on reciprocity — which is why the
purchase links are the default path and this is opt-in.

## Tests

```bash
make test              # unit + integration-free suite
make test-integration  # hits YouTube and SoundCloud for real
make lint              # frontend typecheck
```

The suite runs against synthetic audio with a stub identifier: what is being
verified is segmentation, merging and the streaming maths, not that Shazam's
servers are reachable.

## Deployment

`docker-stack.yml` deploys to Docker Swarm behind Traefik. The Dockerfile builds
the frontend in a first stage and copies `web/dist` into the runtime image.

## License

MIT — see [LICENSE](LICENSE).

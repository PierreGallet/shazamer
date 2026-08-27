#!/usr/bin/env python3
"""Import pre-1.0 tracklists from outputs/ into the library.

Before 1.0 results were written as loose JSON files and listed by scanning the
directory. The library reads SQLite instead, so those files became invisible —
still on disk, but nothing looks at them. This brings them across.

What survives the trip: title, artist, timestamp, Shazam link, and the
normalised key that makes cross-set recurrence work, which is the main reason
to import at all.

What cannot, because it was never recorded:

- **Waveform.** Never computed. Imported sets draw a flat line.
- **BPM and key.** Never computed.
- **Unidentified stretches.** The old pipeline dropped them entirely, so an
  imported set has gaps that are simply absent rather than marked "ID ?".
- **Repeats.** The old pipeline deduplicated by artist and title before
  saving, so a track played twice appears once, at its first occurrence.
- **Segment ends.** Derived from the following track's start. The final track
  gets the median gap, since the set's real duration was never stored.

Imported sets are marked `source_kind="legacy"` so they are distinguishable,
and the id is derived from the filename so re-running replaces rather than
duplicates.

    PYTHONPATH=. python scripts/import_legacy.py outputs/ --db data/library.db
    PYTHONPATH=. python scripts/import_legacy.py outputs/ --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.timecode import format_timestamp          # noqa: E402
from src.identify.base import normalize_key             # noqa: E402
from src.store.library import Library                   # noqa: E402

DEFAULT_TAIL_SECONDS = 240.0


def set_id_for(path: Path) -> str:
    """Stable id from the filename, so a re-run replaces its own import."""
    digest = hashlib.sha1(path.name.encode("utf-8")).hexdigest()[:16]
    return f"legacy-{digest}"


def title_for(path: Path) -> str:
    stem = path.stem
    for suffix in ("_tracklist",):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    # Strip the old "20251229_024213_" timestamp prefix if present.
    parts = stem.split("_", 2)
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
        stem = parts[2]
    return stem.strip() or path.stem


def convert(path: Path) -> Optional[Dict[str, Any]]:
    """Turn one legacy file into the shape the library stores."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"  ! {path.name}: unreadable ({exc})")
        return None
    if not isinstance(raw, list) or not raw:
        print(f"  · {path.name}: empty, skipped")
        return None

    entries = sorted(raw, key=lambda t: float(t.get("start_time_seconds") or 0))
    starts = [float(t.get("start_time_seconds") or 0) for t in entries]

    gaps = [b - a for a, b in zip(starts, starts[1:]) if b > a]
    tail = statistics.median(gaps) if gaps else DEFAULT_TAIL_SECONDS
    duration = starts[-1] + tail

    tracks: List[Dict[str, Any]] = []
    for i, entry in enumerate(entries):
        start = starts[i]
        end = starts[i + 1] if i + 1 < len(starts) else duration
        title = (entry.get("title") or "").strip() or "Unknown"
        artist = (entry.get("artist") or "").strip() or "Unknown"
        tracks.append({
            "index": i + 1,
            "start": round(start, 3),
            "end": round(end, 3),
            "start_label": format_timestamp(start),
            "duration": round(max(0.0, end - start), 3),
            "identified": True,
            "title": title,
            "artist": artist,
            # The whole point of importing: this is what links a track to the
            # same track in a set analysed after 1.0.
            "key": normalize_key(artist, title),
            "url": (entry.get("shazam_url") or "").strip(),
            "cover_url": "", "album": "", "label": "", "year": "",
            "genre": "", "isrc": "",
            "confidence": 0.0,        # legacy match_count is not a confidence
            "votes": 0, "probes": 0,
            "bpm": None, "camelot": None, "musical_key": None,
        })

    return {
        "duration": round(duration, 3),
        "waveform": [],
        "tracks": tracks,
        "stats": {
            "identified": len(tracks),
            "unidentified": 0,      # never recorded, not "none found"
            "segments": len(tracks),
            "strategy": "legacy",
            "imported": True,
        },
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("source", nargs="?", default="outputs",
                        help="Directory holding *_tracklist.json (default: outputs)")
    parser.add_argument("--db", default="data/library.db", help="Library path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be imported, write nothing")
    parser.add_argument("--min-tracks", type=int, default=1, metavar="N",
                        help="Skip sets with fewer than N tracks. Directories "
                             "that doubled as a scratchpad accumulate one-track "
                             "results from testing; raising this keeps them out "
                             "of the library (default: 1, import everything)")
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_dir():
        print(f"Not a directory: {source}")
        return 1

    files = sorted(source.glob("*_tracklist.json"))
    if not files:
        print(f"No *_tracklist.json under {source}")
        return 0

    print(f"{len(files)} legacy tracklist(s) in {source}")
    library = None if args.dry_run else Library(Path(args.db))

    imported = tracks_total = 0
    too_small: List[str] = []
    for path in files:
        payload = convert(path)
        if payload is None:
            continue
        title = title_for(path)
        count = len(payload["tracks"])
        if count < args.min_tracks:
            too_small.append(f"{title[:50]} ({count})")
            continue
        stamp = datetime.fromtimestamp(
            path.stat().st_mtime, tz=timezone.utc).isoformat(timespec="seconds")

        print(f"  {'would import' if args.dry_run else 'importing'}: "
              f"{title[:58]:<58} {count:>3} tracks  {stamp[:10]}")

        if library is not None:
            await library.save_set(
                set_id_for(path), title, payload,
                source_kind="legacy", quality="unknown", created_at=stamp,
            )
        imported += 1
        tracks_total += count

    if too_small:
        print(f"\n{len(too_small)} set(s) below --min-tracks {args.min_tracks}, "
              f"left out:")
        for item in too_small:
            print(f"  · {item}")
        print("  Re-run with --min-tracks 1 to bring them in.")

    print(f"\n{imported} set(s), {tracks_total} tracks "
          f"{'would be imported' if args.dry_run else 'imported'}")
    if not args.dry_run:
        recurring = await library.recurring_tracks(min_sets=2)
        print(f"{len(recurring)} track(s) now appear in more than one set")
        for item in recurring[:5]:
            print(f"  {item['set_count']}×  {item['artist']} — {item['title']}")
    print("\nImported sets carry no waveform, BPM or key — those were never "
          "computed. Re-analyse a set to get them.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

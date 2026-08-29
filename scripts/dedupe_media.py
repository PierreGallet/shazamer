#!/usr/bin/env python3
"""Reclaim the disk taken by set audio stored twice.

Analysing the same mix again downloads it again and keeps both copies. New
analyses share bytes automatically now; this is for what accumulated before
that, and as a periodic tidy.

Nothing is deleted. Identical files are pointed at one copy of the bytes with
a hard link, so every set keeps its own path and its own player — the kernel
counts the references, and the audio goes only when the last set holding it
does.

    python scripts/dedupe_media.py --dry-run
    python scripts/dedupe_media.py
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.dedupe import deduplicate, file_digest  # noqa: E402


def _human(size: int) -> str:
    mb = size / (1024 ** 2)
    return f"{mb / 1024:.2f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-dir", default=None,
                        help="Where set audio lives (default: ./media)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would be shared, change nothing")
    args = parser.parse_args()

    folder = Path(args.media_dir) if args.media_dir else Path("media")
    if not folder.exists():
        print(f"No media directory at {folder}", file=sys.stderr)
        return 2

    files = [p for p in folder.iterdir()
             if p.is_file() and not p.name.endswith(".linking")]
    total = sum(p.stat().st_size for p in files)
    print(f"{folder}: {len(files)} file(s), {_human(total)}")

    groups = defaultdict(list)
    for path in files:
        digest = file_digest(path)
        if digest:
            groups[digest].append(path)

    # Counted by inode, so a run after a previous one reports nothing left to
    # do rather than re-reporting what it already shared.
    inodes = {}
    duplicated = 0
    for digest, paths in groups.items():
        if len(paths) < 2:
            continue
        seen = set()
        for path in paths:
            key = (path.stat().st_dev, path.stat().st_ino)
            if key in seen:
                continue
            seen.add(key)
        if len(seen) < 2:
            continue                       # already sharing
        print(f"  {len(paths)} copies of {_human(paths[0].stat().st_size)}:")
        for path in sorted(paths, key=lambda p: p.stat().st_mtime):
            print(f"    {path.name}")
        duplicated += paths[0].stat().st_size * (len(seen) - 1)
        inodes[digest] = paths

    if not inodes:
        print("  Nothing duplicated.")
        return 0

    print(f"\n  Reclaimable: {_human(duplicated)} "
          f"({duplicated / total * 100:.0f}% of the folder)")

    if args.dry_run:
        print("  Dry run — nothing changed.")
        return 0

    linked, reclaimed = deduplicate(folder)
    after = sum(
        size for size in
        {(p.stat().st_dev, p.stat().st_ino): p.stat().st_size
         for p in files if p.exists()}.values())
    print(f"  Linked {linked} file(s), reclaimed {_human(reclaimed)}.")
    print(f"  Folder now occupies {_human(after)} on disk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

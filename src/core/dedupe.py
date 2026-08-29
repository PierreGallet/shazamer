"""Sharing bytes between sets that hold the same audio.

Analysing the same mix twice — to pick up a fix, or because a first run went
badly — downloads it again and keeps both copies. Measured on this install:
325 MB of set audio, of which 144 MB was two pairs of byte-identical files.
Forty-four percent, and growing with every re-analysis.

Hard links rather than a shared path in the database. Both sets keep their own
filename, the kernel counts the references, and deleting one set unlinks one
name — the bytes go when the last set holding them goes. Pointing two rows at
one path would instead make one set's deletion silently empty another's
player, which is the kind of surprise that is only discovered later.

Content-addressed rather than keyed on the source URL: the same mix reached by
a different link, or re-uploaded, is still the same audio, and a URL is not
evidence about bytes.
"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Read in chunks so a three-hour set is not held in memory to be hashed —
# the whole point of this project's decode path.
CHUNK = 1024 * 1024


def file_digest(path: Path) -> Optional[str]:
    """SHA-256 of a file, or None if it cannot be read.

    Not a cryptographic requirement — this only has to distinguish files that
    differ. SHA-256 is here because it is in the standard library and fast
    enough that hashing 69 MB costs a fraction of a second against an
    analysis that takes twenty minutes.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
    except OSError as exc:
        logger.warning("Could not hash %s: %s", path.name, exc)
        return None
    return digest.hexdigest()


def _already_linked(a: Path, b: Path) -> bool:
    """Whether two paths are the same inode, and so already share bytes."""
    try:
        return a.stat().st_ino == b.stat().st_ino and \
            a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def link_to(existing: Path, duplicate: Path) -> bool:
    """Replace `duplicate` with a hard link to `existing`.

    Written to a temporary name and renamed over the target, so a failure
    partway leaves the original file intact rather than nothing at all. The
    audio is the thing this exists to preserve.
    """
    if _already_linked(existing, duplicate):
        return False
    staging = duplicate.with_name(duplicate.name + ".linking")
    try:
        staging.unlink(missing_ok=True)
        os.link(existing, staging)
        staging.replace(duplicate)
        return True
    except OSError as exc:
        logger.warning("Could not link %s to %s: %s",
                       duplicate.name, existing.name, exc)
        staging.unlink(missing_ok=True)
        return False


def deduplicate(folder: Path) -> Tuple[int, int]:
    """Point every duplicate in `folder` at one copy of its bytes.

    Returns (files linked, bytes reclaimed). Safe to run repeatedly: files
    already sharing an inode are left alone and counted as nothing.

    The oldest copy of each set of duplicates is kept as the original, so the
    inode that survives is the one other things are most likely to already
    point at.
    """
    if not folder.exists():
        return 0, 0

    by_digest: Dict[str, List[Path]] = {}
    for path in sorted(folder.iterdir(), key=lambda p: p.name):
        if not path.is_file() or path.name.endswith(".linking"):
            continue
        digest = file_digest(path)
        if digest:
            by_digest.setdefault(digest, []).append(path)

    linked = reclaimed = 0
    for digest, paths in by_digest.items():
        if len(paths) < 2:
            continue
        paths.sort(key=lambda p: p.stat().st_mtime)
        keeper, *rest = paths
        for duplicate in rest:
            size = duplicate.stat().st_size
            if link_to(keeper, duplicate):
                linked += 1
                reclaimed += size
                logger.info("Linked %s to %s (%d MB shared)",
                            duplicate.name, keeper.name, size // (1024 ** 2))
    return linked, reclaimed


def link_if_duplicate(folder: Path, candidate: Path) -> bool:
    """Link one new file to an identical one already in `folder`.

    The cheap path, for use right after a download: hash the new file once and
    compare against the others rather than rehashing the whole folder. Nothing
    happens when it is the first copy, which is the ordinary case.
    """
    if not candidate.exists():
        return False
    digest = file_digest(candidate)
    if digest is None:
        return False

    size = candidate.stat().st_size
    for other in folder.iterdir():
        if other == candidate or not other.is_file():
            continue
        # Size first: it rules out almost everything for the cost of a stat,
        # where hashing a neighbour costs reading it in full.
        try:
            if other.stat().st_size != size:
                continue
        except OSError:
            continue
        if file_digest(other) == digest and link_to(other, candidate):
            logger.info("%s is the same audio as %s; sharing one copy",
                        candidate.name, other.name)
            return True
    return False

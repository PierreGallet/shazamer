"""Measuring what a downloaded file is, once it has stopped moving.

`downloads` records where a file came from and whether the transfer finished.
It records nothing about the music, so a crate of two hundred files cannot be
sorted by tempo or filtered to a compatible key — which is the entire point of
digging.

Three sources, in ascending order of trust, each allowed to overwrite the last:

  1. The file's own tags. Free, already on disk, and a Soulseek rip usually
     carries a genre from whoever ripped it. Quality varies wildly.
  2. Discogs. Human-curated, and the only place the styles a DJ actually
     browses by are written down.
  3. The audio itself, through Essentia. Tempo, key, loudness, dynamics — the
     things no database can be wrong about because they are properties of the
     bytes.

A sweep rather than a step at the end of a download. Measured at 11.5s for a
58-second file, which is about 70s for a six-minute track: that cannot sit in
the request that marks a transfer complete, and a crate that fills in behind
you is better than one you wait for.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# How many files are described at once. One: `RhythmExtractor2013` is CPU-bound
# and the server runs five other projects, so a sweep that saturates it to
# finish sooner is a sweep that makes everything else slower for no reason
# anybody asked for.
CONCURRENCY = 1


@dataclass
class SweepReport:
    """What a sweep did, in terms somebody can act on."""

    queued: int = 0
    described: int = 0
    skipped: int = 0                    # the file is no longer on disk
    failed: int = 0                     # it is there and could not be read
    styled: int = 0                     # a genre or style was found
    unavailable: bool = False           # Essentia cannot be imported here
    errors: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"queued": self.queued, "described": self.described,
                "skipped": self.skipped, "failed": self.failed,
                "styled": self.styled, "unavailable": self.unavailable,
                "errors": self.errors[:5]}


def genre_from_tags(path: Path) -> str:
    """The genre whoever ripped this file wrote into it, or "".

    The cheapest source there is and the least reliable, which is why it is
    first and why what it produces is labelled. Never raises: a file with no
    tags, an unreadable header and a format mutagen does not know all mean the
    same thing here, which is that we learned nothing.
    """
    try:
        import mutagen

        loaded = mutagen.File(str(path), easy=True)
    except Exception as exc:            # noqa: BLE001
        logger.debug("Could not read tags from %s: %s", path.name, exc)
        return ""
    if loaded is None:
        return ""
    values = loaded.get("genre") or []
    first = str(values[0]).strip() if values else ""
    # "Other", "Unknown" and bare numbers are ID3 filler, not answers.
    if first.lower() in ("", "other", "unknown", "none") or first.isdigit():
        return ""
    return first[:64]


def _declared_quality(label: str) -> "tuple[int, bool]":
    """(bitrate in kbps, whether it claims to be lossless) from "MP3 320 kbps".

    Parsed back out of the stored label rather than kept as a column: the label
    is what the peer said, it is already recorded, and a second copy of it
    would be a second thing that can disagree.
    """
    import re

    upper = label.upper()
    lossless = any(f in upper for f in ("FLAC", "WAV", "AIFF", "AIF", "ALAC"))
    found = re.search(r"(\d+)\s*KBPS", upper)
    return (int(found.group(1)) if found else 0), lossless


async def describe_one(library, row: Dict[str, Any],
                       discogs=None) -> str:
    """Measure one download and store what came back.

    Returns a short word for the outcome: "described", "skipped", "failed".

    Always stamps `analysed_at`, even when nothing was found. A file whose
    tempo genuinely cannot be detected must not be re-measured on every sweep
    for ever, and "we looked and there was nothing" is itself a result.
    """
    from ..core import descriptors as audio_descriptors
    from ..store.library import _now

    local = Path(row.get("local_path") or "")
    if not local or not local.exists():
        return "skipped"

    fields: Dict[str, Any] = {"analysed_at": _now()}

    tag_genre = genre_from_tags(local)
    if tag_genre:
        fields["genre"] = tag_genre
        fields["style_source"] = "tag"

    if discogs is not None:
        try:
            found = await discogs.lookup(row.get("artist", ""),
                                         row.get("title", ""))
        except asyncio.CancelledError:
            raise
        except Exception as exc:        # noqa: BLE001
            logger.debug("Discogs lookup failed for %s: %s",
                         row.get("title", ""), exc)
            found = None
        # None means Discogs did not answer, which must never blank a style the
        # file's own tags already gave.
        if found is not None and not found.empty:
            if found.genre:
                fields["genre"] = found.genre
            fields["style"] = found.style
            fields["style_source"] = "discogs"

    # Whether the declared bitrate is true of the audio. Under a second, and
    # it is the one measurement here that can contradict something already on
    # screen — the rest add facts, this one corrects one.
    declared, lossless = _declared_quality(row.get("quality", ""))
    try:
        from ..core.bitrate import assess

        cutoff, quality_note = assess(local, declared, lossless)
        if cutoff is not None:
            fields["cutoff_hz"] = round(cutoff, 1)
        if quality_note:
            fields["quality_note"] = quality_note
    except Exception as exc:            # noqa: BLE001
        logger.debug("Could not assess %s: %s", local.name, exc)

    loop = asyncio.get_running_loop()
    # A thread: Essentia holds the CPU for seconds at a time and the event loop
    # is also serving the page the sweep's progress is watched from.
    measured = await loop.run_in_executor(
        None, audio_descriptors.describe, local)
    if measured is not None and not measured.empty:
        fields.update({k: v for k, v in measured.to_dict().items()
                       if v not in (None, "")})

    await library.update_download(int(row["id"]), **fields)
    if measured is None and not tag_genre and "style" not in fields:
        return "failed"
    return "described"


async def sweep(library, *, user_id: Optional[str] = None,
                limit: int = 500, use_discogs: bool = True) -> SweepReport:
    """Describe every finished download nobody has measured yet.

    Safe to run repeatedly and safe to run while downloads are arriving: the
    query selects on `analysed_at`, so a second sweep over the same crate
    queues nothing.
    """
    from ..core import descriptors as audio_descriptors

    report = SweepReport(unavailable=not audio_descriptors.available())

    rows = await library.undescribed_downloads(user_id=user_id, limit=limit)
    report.queued = len(rows)
    if not rows:
        return report

    discogs = None
    if use_discogs:
        from ..enrich.discogs import DiscogsEnricher

        discogs = DiscogsEnricher()

    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(row: Dict[str, Any]) -> None:
        async with semaphore:
            try:
                outcome = await describe_one(library, row, discogs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:    # noqa: BLE001 - one bad file must not
                # end a sweep over two hundred. A truncated MP3 is ordinary in
                # a peer-to-peer crate.
                logger.warning("Could not describe download %s: %s",
                               row.get("id"), exc)
                report.failed += 1
                report.errors.append(f"{row.get('title', '?')}: {exc}")
                return
        if outcome == "skipped":
            report.skipped += 1
        elif outcome == "failed":
            report.failed += 1
        else:
            report.described += 1

    await asyncio.gather(*(one(row) for row in rows))

    fresh = await library.recent_downloads(limit=limit, user_id=user_id or "")
    report.styled = sum(1 for d in fresh if d.get("style") or d.get("genre"))
    logger.info("Sweep: %d described, %d skipped, %d failed of %d",
                report.described, report.skipped, report.failed, report.queued)
    return report

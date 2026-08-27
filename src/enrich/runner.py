"""Enriching a whole set: cache first, provider second.

Kept out of the analysis on purpose. Lookups are paced at roughly one per
second, so a 26-track set adds a minute or two — time the analysis should not
spend, and a failure surface it should not carry. Enrichment runs afterwards
as its own job and updates tracks in place; a set is complete and usable
before any of this happens.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .base import Enricher, TrackMeta

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentReport:
    looked_up: int = 0
    from_cache: int = 0
    found: int = 0
    updated_rows: int = 0

    def as_dict(self) -> Dict[str, int]:
        return {"looked_up": self.looked_up, "from_cache": self.from_cache,
                "found": self.found, "updated_rows": self.updated_rows}


async def enrich_set(library, enricher: Enricher, set_id: str) -> EnrichmentReport:
    """Fill in label, catalogue number and year for a set's tracks.

    Only tracks with no label are considered, so re-running is cheap and a
    value the identifier supplied is never overwritten — that came from the
    audio, while this comes from a name lookup that can land on the wrong
    record.
    """
    report = EnrichmentReport()
    pending = await library.tracks_needing_enrichment(set_id)
    if not pending:
        return report

    logger.info("Enriching %d track(s) in set %s", len(pending), set_id)

    for track in pending:
        key = track["key"]
        cached = await library.cached_enrichment(key)
        if cached is not None:
            report.from_cache += 1
            if not cached.get("found"):
                continue                # a known miss; do not ask again
            meta = _from_row(cached)
        else:
            report.looked_up += 1
            meta = await enricher.lookup(track["artist"], track["title"],
                                         track.get("isrc", ""))
            await library.remember_enrichment(
                key, meta.to_dict() if meta else None)
            if meta is None:
                continue

        if meta is None or meta.empty:
            continue

        report.found += 1
        # Merged against the track as it stands, so nothing already known is
        # replaced by a guess.
        existing = {"label": "", "year": "", "album": "", "genre": "",
                    "isrc": track.get("isrc", "")}
        report.updated_rows += await library.apply_enrichment(
            key, meta.merged_over(existing))

    logger.info("Enrichment of %s: %s", set_id, report.as_dict())
    return report


def _from_row(row: Dict[str, Any]) -> Optional[TrackMeta]:
    return TrackMeta(
        label=row.get("label", "") or "",
        catalog_number=row.get("catalog_number", "") or "",
        year=row.get("year", "") or "",
        album=row.get("album", "") or "",
        genre=row.get("genre", "") or "",
        isrc=row.get("isrc", "") or "",
        provider=row.get("provider", "") or "",
        recording_id=row.get("mbid", "") or "",
        confidence=row.get("confidence", 0.0) or 0.0,
    )

"""What enrichment produces, and what a provider has to satisfy.

Shazam names a track. It rarely says which label released it, in what year, or
under what catalogue number — and those are exactly what a digger needs to go
looking for a record. Enrichment fills that in from a music database after the
fact.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class TrackMeta:
    """Metadata found for a track. Every field optional — partial is normal.

    `confidence` is the provider's own view of how well the record matched, on
    0-1. A search by artist and title is a fuzzy lookup, and a wrong label is
    worse than no label: it sends you hunting for a release that does not
    exist.
    """
    label: str = ""
    catalog_number: str = ""
    year: str = ""
    album: str = ""
    genre: str = ""
    isrc: str = ""
    provider: str = ""
    provider_url: str = ""
    recording_id: str = ""
    release_id: str = ""
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def empty(self) -> bool:
        return not any((self.label, self.catalog_number, self.year,
                        self.album, self.genre, self.isrc))

    def merged_over(self, existing: Dict[str, Any]) -> Dict[str, Any]:
        """Fields worth writing, given what the track already has.

        Never overwrites: what the identifier reported came from the audio,
        while this came from a name lookup that can land on the wrong record.
        Only genuinely missing fields are filled.
        """
        out: Dict[str, Any] = {}
        for field_name in ("label", "year", "album", "genre", "isrc"):
            found = getattr(self, field_name)
            if found and not (existing.get(field_name) or "").strip():
                out[field_name] = found
        if self.catalog_number:
            out["catalog_number"] = self.catalog_number
        if self.recording_id:
            out["mbid"] = self.recording_id
        return out


@runtime_checkable
class Enricher(Protocol):
    """Anything that can look up a track by name."""

    name: str

    async def lookup(self, artist: str, title: str,
                     isrc: str = "") -> Optional[TrackMeta]:
        ...

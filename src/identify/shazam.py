"""Shazam identification via shazamio.

Two things matter here.

The fingerprinter (`shazamio_core`) is a Rust extension that takes a *centred
10 second window* of whatever it is handed, converted to mono 16 kHz. Feeding
it a full segment meant decoding minutes of audio to use ten seconds of it, so
we hand it a 12 s probe extracted straight from the source instead — as bytes,
never a temp file.

And the call is I/O bound, so concurrency is bounded by a semaphore rather than
serialised behind a rate limiter.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from .base import Identifier, TrackMatch

logger = logging.getLogger(__name__)


def _section_metadata(payload: Dict[str, Any]) -> Dict[str, str]:
    """Flatten Shazam's `sections[].metadata[]` into a plain dict."""
    out: Dict[str, str] = {}
    for section in payload.get("sections") or []:
        for item in section.get("metadata") or []:
            title = item.get("title")
            text = item.get("text")
            if title and text:
                out[str(title).strip().lower()] = str(text).strip()
    return out


def parse_shazam_track(result: Dict[str, Any]) -> Optional[TrackMatch]:
    """Map a raw Shazam response onto a `TrackMatch`. Pure — easy to test."""
    track = (result or {}).get("track")
    if not isinstance(track, dict):
        return None

    title = (track.get("title") or "").strip()
    artist = (track.get("subtitle") or "").strip()
    if not title:
        return None

    meta = _section_metadata(track)
    images = track.get("images") or {}
    matches = result.get("matches") or []

    return TrackMatch(
        title=title,
        artist=artist or "Unknown",
        provider="shazam",
        url=(track.get("url") or "").strip(),
        cover_url=(images.get("coverarthq") or images.get("coverart") or "").strip(),
        album=meta.get("album", ""),
        label=meta.get("label", ""),
        year=meta.get("released", ""),
        genre=((track.get("genres") or {}).get("primary") or "").strip(),
        isrc=(track.get("isrc") or "").strip(),
        raw_matches=len({m.get("id") for m in matches if m.get("id")}),
    )


class ShazamIdentifier:
    """Concurrency-bounded Shazam client.

    `concurrency` is the single knob that decides how fast a set is analysed.
    Eight parallel probes is comfortable in practice; shazamio already retries
    429s with exponential backoff underneath, so overshooting degrades
    gracefully rather than failing.
    """

    name = "shazam"

    def __init__(self, concurrency: int = 8, language: str = "en-US",
                 endpoint_country: str = "GB") -> None:
        from shazamio import Shazam

        self._shazam = Shazam(language=language, endpoint_country=endpoint_country)
        self._sem = asyncio.Semaphore(concurrency)
        self.concurrency = concurrency

    async def identify(self, wav_bytes: bytes) -> Optional[TrackMatch]:
        async with self._sem:
            try:
                result = await self._shazam.recognize(wav_bytes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # One failed probe must never sink the analysis: an unmatched
                # window is a legitimate outcome, and the segment simply shows
                # as unidentified.
                logger.warning("Shazam probe failed: %s", exc)
                return None
        return parse_shazam_track(result or {})

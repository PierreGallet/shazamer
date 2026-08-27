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


# Failures that mean "ask again in a moment" rather than "there is no answer".
# The decode error is the telling one: under load the service stops returning
# JSON and serves something else entirely.
_TRANSIENT_SIGNS = ("decode", "json", "429", "too many", "timeout", "timed out",
                    "connection", "reset", "temporarily", "503", "502", "504",
                    "server disconnected")


def _looks_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(sign in text for sign in _TRANSIENT_SIGNS)


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

    `concurrency` is deliberately modest. Measured against the live service,
    throughput plateaus at roughly three probes per second whatever is asked
    of it: two in parallel and eight in parallel move the same number of
    probes. The extra slots buy nothing and cost something — they are what
    tips the service into refusing, which is far more expensive than waiting.
    """

    name = "shazam"

    def __init__(self, concurrency: int = 4, language: str = "en-US",
                 endpoint_country: str = "GB", max_attempts: int = 4,
                 backoff: float = 2.0) -> None:
        from shazamio import Shazam

        self._shazam = Shazam(language=language, endpoint_country=endpoint_country)
        self._sem = asyncio.Semaphore(concurrency)
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self.backoff = backoff

    async def identify(self, wav_bytes: bytes) -> Optional[TrackMatch]:
        """Identify a probe, retrying when the service refuses rather than
        answers.

        The distinction is the whole point. "No match" is a real, common and
        useful outcome — it is how a dub or an unreleased edit shows up. "The
        service would not talk to us" is not an outcome at all, and recording
        it as one manufactures gaps in the tracklist.

        Under load Shazam stops returning JSON and serves something else, which
        surfaces as a decode error. In one production run 113 of 206 probes
        came back that way and every one was filed as an unidentified segment:
        more than half the set silently blanked, with nothing to say the answer
        had never been asked for.
        """
        for attempt in range(self.max_attempts):
            async with self._sem:
                try:
                    result = await self._shazam.recognize(wav_bytes)
                    return parse_shazam_track(result or {})
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    transient = _looks_transient(exc)
                    if not transient or attempt == self.max_attempts - 1:
                        logger.warning(
                            "Shazam probe %s after %d attempt(s): %s",
                            "gave up" if transient else "failed",
                            attempt + 1, exc)
                        return None

            # Backoff outside the semaphore: holding a slot while waiting would
            # throttle the probes that are still working.
            delay = self.backoff * (2 ** attempt)
            logger.debug("Shazam looks rate-limited, retrying in %.1fs", delay)
            await asyncio.sleep(delay)

        return None

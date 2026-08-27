"""MusicBrainz lookups.

Beets was the obvious suggestion here and it is the wrong tool for this half of
the problem: beets tags *files*, and a track in the library has no file — it is
a name and a timestamp inside someone else's mix. So this talks to the API
directly. Beets becomes the right tool later, once acquisition has actually put
a file on disk worth tagging.

Two constraints shape everything below.

**One request per second, and they mean it.** MusicBrainz throttles by IP and a
burst earns a 503. Requests are serialised behind a lock with a minimum spacing
rather than fired concurrently, which makes enrichment slow and unsuited to
running inside an analysis.

**They require a real User-Agent.** A generic one is refused outright, and a
missing contact address is grounds for a block.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from .base import TrackMeta

logger = logging.getLogger(__name__)

BASE_URL = "https://musicbrainz.org/ws/2"

# Their published limit is one request per second sustained. 1.1s still drew
# 503s in practice — the limit is per IP and an office address is shared — so
# the spacing is wider than the letter of the rule. Enrichment runs in the
# background, so slower costs nothing that matters.
MIN_INTERVAL = float(os.environ.get("MUSICBRAINZ_INTERVAL", "1.5"))

CONTACT = os.environ.get("MUSICBRAINZ_CONTACT", "https://github.com/PierreGallet/shazamer")
USER_AGENT = f"Shazamer/1.0 ( {CONTACT} )"

# Below this the match is too loose to trust. A wrong label is worse than none:
# it sends you looking for a release that does not exist.
MIN_SCORE = int(os.environ.get("MUSICBRAINZ_MIN_SCORE", "88"))

# Release-group secondary types that mean "this release contains the track"
# rather than "this release is the track". A digger wants the record, not the
# mix somebody put it on.
SECONDHAND_TYPES = {"Compilation", "DJ-mix", "Live", "Mixtape/Street", "Demo",
                    "Interview", "Audiobook", "Soundtrack"}

_PUNCT = re.compile(r'["\\\\]')


def _escape(value: str) -> str:
    """Escape for a Lucene phrase query — MusicBrainz search is Lucene."""
    return _PUNCT.sub(" ", value or "").strip()


class MusicBrainzEnricher:
    """Rate-limited MusicBrainz client.

    One instance holds the pacing lock, so share it across a whole enrichment
    run rather than creating one per track.
    """

    name = "musicbrainz"

    def __init__(self, min_interval: float = MIN_INTERVAL,
                 timeout: float = 20.0) -> None:
        self.min_interval = min_interval
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def _get(self, path: str, params: Dict[str, str],
                   attempts: int = 4) -> Optional[dict]:
        """One paced request, retried while the server says it is busy.

        Retrying matters more than it looks. MusicBrainz answers a 503 with a
        JSON body and no results, so a caller that treats "could not ask" the
        same as "nothing found" quietly records a track as having no label —
        and does it more often the busier the server is. That is worse than an
        error: it looks like an answer.
        """
        import aiohttp

        for attempt in range(attempts):
            async with self._lock:
                wait = self.min_interval - (time.monotonic() - self._last_call)
                if wait > 0:
                    await asyncio.sleep(wait)

                status = 0
                payload: Optional[dict] = None
                try:
                    timeout = aiohttp.ClientTimeout(total=self.timeout)
                    async with aiohttp.ClientSession(timeout=timeout) as session:
                        async with session.get(
                            url := f"{BASE_URL}{path}",
                            params={**params, "fmt": "json"},
                            headers={"User-Agent": USER_AGENT},
                        ) as response:
                            status = response.status
                            if status == 200:
                                payload = await response.json()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("MusicBrainz request failed: %s", exc)
                finally:
                    self._last_call = time.monotonic()

            if payload is not None:
                # A 200 can still carry an error body when the server is busy.
                if isinstance(payload, dict) and payload.get("error"):
                    logger.debug("MusicBrainz busy on %s: %s", path,
                                 str(payload["error"])[:80])
                else:
                    return payload
            elif status not in (0, 503, 500, 502, 504):
                return None             # a real refusal, not congestion

            if attempt < attempts - 1:
                backoff = self.min_interval * (2 ** attempt)
                logger.debug("Retrying %s in %.1fs", path, backoff)
                await asyncio.sleep(backoff)

        logger.warning("Gave up on MusicBrainz %s after %d attempts",
                       path, attempts)
        return None

    async def lookup(self, artist: str, title: str,
                     isrc: str = "") -> Optional[TrackMeta]:
        """Find a track, preferring an ISRC when one is known.

        An ISRC identifies a specific recording, so it skips the fuzzy search
        entirely — when Shazam gave us one, that is the answer.
        """
        if isrc:
            found = await self._by_isrc(isrc)
            if found is not None:
                return found
        if not (artist and title):
            return None
        return await self._by_search(artist, title)

    async def _by_isrc(self, isrc: str) -> Optional[TrackMeta]:
        payload = await self._get(f"/isrc/{isrc}", {})
        recordings = (payload or {}).get("recordings") or []
        if not recordings:
            return None
        recording = recordings[0]
        releases = await self._releases_for(recording["id"])
        # An ISRC is exact, so nothing here is a guess.
        return self._to_meta(recording, releases, confidence=1.0, isrc=isrc)

    async def _by_search(self, artist: str, title: str) -> Optional[TrackMeta]:
        query = (f'artist:"{_escape(artist)}" AND '
                 f'recording:"{_escape(title)}"')
        payload = await self._get("/recording", {"query": query, "limit": "5"})
        recordings = (payload or {}).get("recordings") or []
        if not recordings:
            return None

        best = self._pick_recording(recordings)
        if best is None:
            logger.debug("No MusicBrainz match for %s - %s above the score "
                         "floor", artist, title)
            return None

        score = int(best.get("score") or 0)
        releases = await self._releases_for(best["id"])
        return self._to_meta(best, releases, confidence=score / 100.0)

    @staticmethod
    def _pick_recording(recordings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Choose which recording of a song to describe.

        A popular track has several: the released recording, plus a separate
        one for every DJ mix and broadcast it has appeared in. They routinely
        all score 100, so taking the first is a coin toss — and it produced
        different answers on consecutive runs of the same query, one of them
        naming a Boiler Room set as the release.

        The search response already lists each candidate's releases with their
        types, so the right one can be picked here rather than paid for in
        extra requests: prefer whichever recording appears on the most releases
        that are the record itself rather than something carrying it.
        """
        eligible = [r for r in recordings
                    if int(r.get("score") or 0) >= MIN_SCORE]
        if not eligible:
            return None

        def firsthand(recording: Dict[str, Any]) -> int:
            count = 0
            for release in recording.get("releases") or []:
                group = release.get("release-group") or {}
                secondary = set(group.get("secondary-types") or [])
                if not (secondary & SECONDHAND_TYPES):
                    count += 1
            return count

        # Ordered, so ties resolve the same way every time.
        return max(
            eligible,
            key=lambda r: (firsthand(r),
                           len(r.get("releases") or []),
                           int(r.get("score") or 0)),
        )

    async def _releases_for(self, recording_id: str) -> List[Dict[str, Any]]:
        """Releases carrying this recording, with their label and catalogue.

        A separate browse request on purpose. Asking for
        `recording/{id}?inc=releases+labels` looks like it should work and is
        the obvious thing to write — but `labels` is not a valid sub-query
        there, and MusicBrainz does not say so: it returns 200 with the
        releases silently dropped. That reads as "this recording has no
        releases" and every lookup comes back with an empty label.
        """
        payload = await self._get("/release", {
            "recording": recording_id,
            "inc": "labels+release-groups",
            "limit": "25",
        })
        return (payload or {}).get("releases") or []

    def _to_meta(self, recording: Dict[str, Any],
                 releases: List[Dict[str, Any]], confidence: float,
                 isrc: str = "") -> Optional[TrackMeta]:
        release = self._pick_release(releases)
        label_info = (release.get("label-info") or [{}])[0] if release else {}
        label = (label_info.get("label") or {}).get("name", "") or ""

        return TrackMeta(
            label=label,
            catalog_number=label_info.get("catalog-number") or "",
            year=(release.get("date") or "")[:4] if release else "",
            album=release.get("title", "") if release else "",
            isrc=isrc or (recording.get("isrcs") or [""])[0],
            provider=self.name,
            provider_url=f"https://musicbrainz.org/recording/{recording.get('id', '')}",
            recording_id=recording.get("id", "") or "",
            release_id=release.get("id", "") if release else "",
            confidence=round(confidence, 3),
        )

    @staticmethod
    def _pick_release(releases: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Choose the release a digger would actually be looking for.

        A recording can appear on dozens: the original single, then years of
        compilations and reissues. The earliest dated one carrying a label is
        the original release, which is the useful answer — the catalogue number
        of a 2019 "Best Of" helps nobody find the 2003 twelve-inch.
        """
        if not releases:
            return None

        def is_secondhand(release: Dict[str, Any]) -> bool:
            group = release.get("release-group") or {}
            secondary = set(group.get("secondary-types") or [])
            return bool(secondary & SECONDHAND_TYPES)

        # Anything that merely *carries* the track goes out first: compilations,
        # DJ mixes, live sets. A recording accumulates these for years, and the
        # catalogue number of a 2021 fabric mix helps nobody find the 12-inch it
        # was lifted from. For Overmono's "So U Kno" the difference is between
        # "fabric presents Overmono" and Poly Kicks — the actual release.
        pool = [r for r in releases if not is_secondhand(r)] or releases
        labelled = [r for r in pool
                    if (r.get("label-info") or [{}])[0].get("label")]
        pool = labelled or pool

        dated = [r for r in pool if r.get("date")]
        if dated:
            return min(dated, key=lambda r: r["date"])
        return pool[0]

"""Finding an audio excerpt of an identified record.

There is a reason to want one that has nothing to do with listening for
pleasure: a tracklist is a set of claims, and the only way to check a claim is
to hear the record next to the moment it was claimed for. A wrong match at
low confidence is indistinguishable from a right one until you do.

Shazam supplies an excerpt with the match, and that is the first choice. This
covers the rest — every track identified before that was captured, which is
the entire existing library.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

SEARCH_URL = "https://itunes.apple.com/search"
TIMEOUT = aiohttp.ClientTimeout(total=12)

# The same Apple Music excerpt Shazam points at — verified by looking a track
# up both ways and getting a byte-identical URL. No key, no account, no quota
# published; polite use is one request per lookup and the result is cached.
#
# One request at a time. Apple does not document a limit, and the way to find
# an undocumented limit is to hit it during an analysis that matters.
_gate = asyncio.Semaphore(1)


def _score(result: dict, artist: str, title: str) -> int:
    """How well a search hit matches what we asked for.

    Crude on purpose. iTunes returns covers, karaoke versions and tributes for
    almost anything, and a name check catches those far more reliably than
    trusting the ordering.
    """
    hit_artist = (result.get("artistName") or "").lower()
    hit_title = (result.get("trackName") or "").lower()
    want_artist, want_title = artist.lower(), title.lower()

    score = 0
    if want_artist and (want_artist in hit_artist or hit_artist in want_artist):
        score += 2
    if want_title and (want_title in hit_title or hit_title in want_title):
        score += 2
    # A "karaoke" or "tribute" hit that passed the name check above is worse
    # than useless: it sounds nearly right, which is the hardest kind of wrong
    # to notice when the whole point is checking by ear.
    if any(bad in hit_title or bad in hit_artist
           for bad in ("karaoke", "tribute", "made famous by", "cover version")):
        score -= 5
    return score


async def find_preview(artist: str, title: str,
                       isrc: str = "") -> Optional[str]:
    """A URL for a ~30 second excerpt, or None.

    None is an ordinary answer: plenty of records are not on Apple Music at
    all, which for a set full of dubs and white labels is most of them.
    """
    terms = " ".join(p for p in (artist, title) if p).strip()
    if not terms:
        return None

    params = {"term": terms, "entity": "song", "limit": "8"}
    url = f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"

    try:
        async with _gate:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        logger.info("iTunes search returned %s for %r",
                                    response.status, terms)
                        return None
                    # Apple serves this as text/javascript, so the content type
                    # has to be ignored or aiohttp refuses to parse it.
                    payload = await response.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.info("Could not reach iTunes for %r: %s", terms, exc)
        return None
    except Exception as exc:                       # noqa: BLE001 - see below
        # A malformed body should cost this one lookup, not the request that
        # asked for it.
        logger.warning("Unexpected iTunes response for %r: %s", terms, exc)
        return None

    results = [r for r in (payload.get("results") or []) if r.get("previewUrl")]
    if not results:
        return None

    # An exact ISRC match beats any name scoring, when both sides have one.
    if isrc:
        for result in results:
            if (result.get("isrc") or "").upper() == isrc.upper():
                return result["previewUrl"]

    best = max(results, key=lambda r: _score(r, artist, title))
    if _score(best, artist, title) < 2:
        # Nothing resembled what was asked for. Returning the top hit anyway
        # would produce a confident excerpt of the wrong record, which is
        # worse than no excerpt for a feature whose whole job is verification.
        return None
    return best["previewUrl"]

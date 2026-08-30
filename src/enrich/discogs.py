"""Sub-genre from Discogs, because "Electronic" is not something you can dig by.

The genre we show today comes from Shazam and is coarse: "Dance", "Electronic".
The axis a crate is actually browsed on is *style* — Deep House against Tech
House against Minimal against Hard Groove — and Discogs is where that lives.
It separates genre from style, the taxonomy is curated by people who buy the
records, and its coverage of electronic vinyl is far better than MusicBrainz's,
which is built around commercial releases.

Rejected in favour of this: Essentia's Discogs classifier, which predicts style
from audio alone and so would also cover white labels and promos Discogs has
never listed. A real argument, and it loses on cost — it needs
`essentia-tensorflow`, 292 MB of wheel against 13.8 MB for plain Essentia, for
one field. A human-curated style also beats a classifier on exactly this
material.

Paced and shaped after `musicbrainz.py`: one instance holds the lock, a request
that could not be made is never mistaken for a record with no style, and a
missing token degrades to nothing rather than to an exception.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BASE_URL = "https://api.discogs.com"

# Discogs allows 60 requests a minute with a token and 25 without. One second
# apart keeps us inside the authenticated allowance with room to spare; the
# unauthenticated case is handled by `MIN_INTERVAL_ANONYMOUS`.
MIN_INTERVAL = float(os.environ.get("DISCOGS_INTERVAL", "1.1"))
MIN_INTERVAL_ANONYMOUS = 2.5

TOKEN = os.environ.get("DISCOGS_TOKEN", "")
CONTACT = os.environ.get("DISCOGS_CONTACT",
                         "https://github.com/PierreGallet/shazamer")
USER_AGENT = f"Shazamer/1.0 +{CONTACT}"

# Everything in brackets, and everything after "feat". A remix credit and a
# featured vocalist both stop a Discogs release search dead, and neither is
# part of what we are asking about.
_BRACKETED = re.compile(r"[\(\[\{].*?[\)\]\}]")
_FEATURING = re.compile(r"\b(feat|ft|featuring|with)\.?\s.*$", re.IGNORECASE)
_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)


def clean(text: str) -> str:
    """A search term Discogs will actually match.

    Accents folded and punctuation dropped, but words kept whole: "cohérent"
    must become "coherent" and not "cohrent". The same lesson the Soulseek
    query learned, and for the same reason — a stripped character inside a word
    turns a search into a different search rather than a looser one.

    The transliteration table is shared with the Soulseek query rather than
    copied, because the two are answering the same question and a second table
    would drift. NFKD alone is not enough: it decomposes é into e plus an
    accent, but ø and æ are letters in their own right and come through
    untouched, so "Møller" would stay "Møller" and match nothing.
    """
    from ..acquire.slskd import _TRANSLITERATE

    text = _BRACKETED.sub(" ", text or "")
    text = _FEATURING.sub(" ", text)
    folded = "".join(_TRANSLITERATE.get(c, c) for c in text)
    folded = unicodedata.normalize("NFKD", folded)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", _NON_WORD.sub(" ", folded)).strip()


@dataclass(frozen=True)
class Style:
    """What Discogs says a record is."""

    genre: str = ""
    style: str = ""
    source: str = ""

    @property
    def empty(self) -> bool:
        return not (self.genre or self.style)


class DiscogsEnricher:
    """Rate-limited Discogs client. Share one instance across a run."""

    name = "discogs"

    def __init__(self, token: str = TOKEN, min_interval: Optional[float] = None,
                 timeout: float = 20.0) -> None:
        self.token = token
        self.min_interval = min_interval if min_interval is not None else (
            MIN_INTERVAL if token else MIN_INTERVAL_ANONYMOUS)
        self.timeout = timeout
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        if not token:
            logger.info("No DISCOGS_TOKEN; searching anonymously at %.1fs "
                        "between calls", self.min_interval)

    async def _get(self, path: str, params: Dict[str, str],
                   attempts: int = 3) -> Optional[dict]:
        """One paced request, retried while Discogs says it is busy.

        A 429 is congestion and worth waiting out; a 404 is an answer. Treating
        the two alike would either hammer a rate limit or silently record a
        record as having no style because the server was briefly loaded — and
        the second is worse than an error, because it looks like a result.
        """
        import aiohttp

        headers = {"User-Agent": USER_AGENT}
        if self.token:
            headers["Authorization"] = f"Discogs token={self.token}"

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
                        async with session.get(f"{BASE_URL}{path}",
                                               params=params,
                                               headers=headers) as response:
                            status = response.status
                            if status == 200:
                                payload = await response.json()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:        # noqa: BLE001
                    logger.debug("Discogs request failed: %s", exc)
                finally:
                    self._last_call = time.monotonic()

            if payload is not None:
                return payload
            if status not in (0, 429, 500, 502, 503, 504):
                return None                     # a refusal, not congestion

            if attempt < attempts - 1:
                backoff = self.min_interval * (2 ** attempt)
                logger.debug("Retrying Discogs %s in %.1fs", path, backoff)
                await asyncio.sleep(backoff)

        logger.warning("Gave up on Discogs %s after %d attempts", path, attempts)
        return None

    async def lookup(self, artist: str, title: str) -> Optional[Style]:
        """Genre and style for one record, or None when Discogs said nothing.

        None and `Style(genre="", style="")` mean different things and the
        caller depends on it: nothing found must not overwrite a style already
        read from the file's own tags.
        """
        artist, title = clean(artist), clean(title)
        if not title:
            return None

        payload = await self._get("/database/search", {
            "artist": artist,
            "track": title,
            "type": "release",
            "per_page": "10",
        })
        if payload is None:
            return None

        results = payload.get("results") or []
        best = self._pick(results, artist)
        if best is None:
            return None

        # `styles` is the specific one and `genres` the umbrella. Both kept:
        # "Electronic / Deep House" reads better than either alone, and a
        # release with a genre and no style still says something.
        styles = [s for s in (best.get("style") or []) if s]
        genres = [g for g in (best.get("genre") or []) if g]
        if not styles and not genres:
            return None
        return Style(genre=", ".join(genres), style=", ".join(styles),
                     source="discogs")

    @staticmethod
    def _pick(results: List[Dict[str, Any]], artist: str) -> Optional[Dict[str, Any]]:
        """The result most likely to be the record we asked about.

        Discogs ranks by its own relevance, which is often right and sometimes
        returns a compilation that merely contains the track. Preferring a
        result whose title carries the artist's name is a cheap guard against
        attaching a hundred-track compilation's genre list to one record.
        """
        if not results:
            return None
        if artist:
            wanted = artist.lower()
            for result in results:
                if wanted in str(result.get("title", "")).lower():
                    return result
        return results[0]

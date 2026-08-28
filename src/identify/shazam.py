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

from ._http import RateLimited, ShazamHTTPClient
from .base import Identifier, TrackMatch

logger = logging.getLogger(__name__)


class _RateGate:
    """One pause, shared by every probe in flight.

    Per-probe backoff is the wrong shape for a rate limit. With four probes
    running, the one that sleeps two seconds after a refusal wakes into a
    limit the other three have been feeding the whole time — so it is refused
    again, sleeps four, and so on, while the service never gets the quiet it
    is asking for. Measured on a 75-minute set: 85 of 128 probes lost, and the
    retries were part of why.

    So the pause belongs to the *service*, not to whichever probe discovered
    it. One refusal stops everyone; a wave of simultaneous refusals counts as
    one refusal, not four; and a run of successes walks the penalty back down.
    """

    def __init__(self, base: float = 10.0, ceiling: float = 300.0) -> None:
        self._base = base
        self._ceiling = ceiling
        self._penalty = base
        self._until = 0.0
        self._lock = asyncio.Lock()
        self.pauses = 0
        self.paused_for = 0.0

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    @property
    def paused(self) -> float:
        """Seconds still to wait; 0 when the service is answering."""
        return max(0.0, self._until - self._now())

    async def wait(self) -> None:
        while True:
            delay = self._until - self._now()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    async def penalise(self) -> float:
        """Record a refusal. Returns how long everyone now waits."""
        async with self._lock:
            now = self._now()
            # Already paused: this probe belongs to the same wave as whoever
            # set the pause, so it must not double the penalty again.
            if now < self._until:
                return self._until - now
            self._until = now + self._penalty
            self.pauses += 1
            self.paused_for += self._penalty
            held = self._penalty
            self._penalty = min(self._penalty * 2, self._ceiling)
            return held

    def relax(self) -> None:
        """A probe got through. Ease off, but never below the base."""
        if self._penalty > self._base:
            self._penalty = max(self._base, self._penalty / 2)


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
# Rate limiting used to hide in here as a decode error, because a 429 arrives
# as a 142-byte HTML page and shazamio reports every non-JSON body the same
# way. It has its own exception and its own handling now; these are the rest.
_TRANSIENT_SIGNS = ("decode", "json", "429", "too many", "timeout", "timed out",
                    "connection", "reset", "temporarily", "503", "502", "504",
                    "server disconnected")


def _looks_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(sign in text for sign in _TRANSIENT_SIGNS)


def _preview_url(track: Dict[str, Any]) -> str:
    """The audio excerpt Shazam offers alongside the match.

    Buried in `hub.actions` as the entry whose type is literally "uri"; the
    sibling `applemusicplay` action carries no URL at all. Absent for plenty of
    records, which is why the caller has a fallback.
    """
    for action in ((track.get("hub") or {}).get("actions") or []):
        if action.get("type") == "uri" and action.get("uri"):
            return str(action["uri"]).strip()
    return ""


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
        preview_url=_preview_url(track),
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
                 backoff: float = 2.0, probe_timeout: float = 45.0,
                 rate_limit_attempts: int = 8) -> None:
        from aiohttp_retry import ExponentialRetry
        from shazamio import Shazam

        # shazamio's own client retries twenty times with a sixty-second
        # ceiling. Layering four attempts on top of that gives a worst case of
        # eighty minutes — for one probe, of a hundred and eighteen. An
        # analysis hung on exactly that: the job ran for half an hour with the
        # CPU idle, waiting inside a library that was waiting.
        #
        # The retry policy belongs in one place. This one is short, and the
        # attempts around it handle what it gives up on.
        #
        # 429 is deliberately absent from `statuses`. Retrying a rate limit
        # here means asking again a second later — certain to fail, and part
        # of what keeps the limit alive. Refusals are handled once, above, by
        # a gate shared across every probe.
        self._shazam = Shazam(
            language=language,
            endpoint_country=endpoint_country,
            http_client=ShazamHTTPClient(
                retry_options=ExponentialRetry(
                    attempts=2, max_timeout=8,
                    statuses={500, 502, 503, 504},
                ),
            ),
        )
        self._gate = _RateGate()
        # Refusals get a longer budget than flaky requests, because waiting a
        # rate limit out is the correct answer and giving up is not. The wait
        # is shared, so eight refusals cost the *analysis* one queue of
        # pauses, not one queue per probe — every probe sleeps through the
        # same penalty and they wake together. A set that takes twenty minutes
        # longer and is complete beats one that returns quickly with a third
        # of it blank; this is a tool you leave running.
        self.rate_limit_attempts = rate_limit_attempts
        # Counted because it is the difference between "this set has dubs in
        # it" and "we never got to ask". Both look like a gap in a tracklist.
        self.lost_to_rate_limit = 0
        self._sem = asyncio.Semaphore(concurrency)
        self.concurrency = concurrency
        self.max_attempts = max_attempts
        self.backoff = backoff
        # A hard ceiling per probe. Without one, a single stalled request holds
        # a semaphore slot indefinitely and the analysis never finishes — it
        # does not fail either, which is worse, because nothing says so.
        self.probe_timeout = probe_timeout

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
        # Two budgets, because these are two different failures.
        #
        # `attempt` counts tries at getting an *answer*. `refusals` counts
        # being turned away, which is not a try at all — if a refusal spent
        # the same budget, a set analysed during a busy hour would abandon
        # probes it would have got a minute later. Only a real attempt
        # advances the loop; a refusal waits on the shared gate and asks
        # again.
        refusals = 0
        attempt = 0
        while attempt < self.max_attempts:
            # Waited on outside the semaphore: a probe serving its share of a
            # shared pause must not also hold a slot, or the pause quietly
            # becomes a concurrency cut for everyone else as well.
            await self._gate.wait()

            async with self._sem:
                try:
                    result = await asyncio.wait_for(
                        self._shazam.recognize(wav_bytes),
                        timeout=self.probe_timeout,
                    )
                    self._gate.relax()
                    return parse_shazam_track(result or {})
                except asyncio.CancelledError:
                    raise
                except RateLimited:
                    refusals += 1
                    if refusals >= self.rate_limit_attempts:
                        self.lost_to_rate_limit += 1
                        logger.warning(
                            "Shazam refused this probe %d times; recorded as "
                            "unidentified", refusals)
                        return None
                    held = await self._gate.penalise()
                    logger.info(
                        "Shazam is rate-limiting; every probe pauses %.0fs", held)
                    continue                # not an attempt at an answer
                except asyncio.TimeoutError:
                    # Treated as transient: a stalled request usually means the
                    # service is struggling, which is exactly when to back off
                    # and ask again rather than give up on the segment.
                    if attempt == self.max_attempts - 1:
                        logger.warning("Shazam probe timed out after %d attempts",
                                       attempt + 1)
                        return None
                    logger.debug("Shazam probe timed out, retrying")
                except Exception as exc:  # noqa: BLE001 - classified below
                    transient = _looks_transient(exc)
                    if not transient or attempt == self.max_attempts - 1:
                        logger.warning(
                            "Shazam probe %s after %d attempt(s): %s",
                            "gave up" if transient else "failed",
                            attempt + 1, exc)
                        return None

            # Backoff outside the semaphore: holding a slot while waiting would
            # throttle the probes that are still working.
            await asyncio.sleep(self.backoff * (2 ** attempt))
            attempt += 1

        return None

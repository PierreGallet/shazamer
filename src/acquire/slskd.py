"""Soulseek acquisition through slskd.

slskd (https://github.com/slskd/slskd) is a headless Soulseek daemon with a
REST API — it runs in Docker, holds the network connection, and exposes search
and transfer endpoints. This module drives it.

Two things to be clear about, because they shape the design rather than being
footnotes:

- Soulseek needs **your own account** and a shared folder in return. The
  network runs on reciprocity; a client that only leeches gets throttled and
  banned, so slskd must be configured with real shares.
- It carries copyrighted material. That is why `acquire.resolve` puts purchase
  links first and treats this as an opt-in path you configure yourself with
  your own credentials — it is off unless `SLSKD_URL` is set.

Candidate selection matters as much as the search: Soulseek returns dozens of
files of wildly varying provenance for a popular track, and picking the
first is how you end up with a 128 kbps rip of a YouTube upload.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import unicodedata
import uuid
from urllib.parse import quote
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

LOSSLESS = {"flac", "wav", "aiff", "aif", "alac"}
_LOSSLESS = LOSSLESS          # the older private name, still used below

# What "best" means depends on where the file is going, so it is a choice
# rather than a constant.
#
# `portable` is the default because it is right more often: FLAC does not
# import into Apple Music at all — macOS reads ALAC, not FLAC — and it is
# roughly three times the size, 40 MB against 14 for a six-minute track. A
# 320 kbps MP3 plays in every DJ application and on every phone.
#
# `lossless` is the right answer if the destination is Rekordbox, Serato or
# Traktor, all of which read FLAC natively and where the headroom matters.
FORMAT_PROFILES = {
    "portable": {"mp3": 100, "m4a": 95, "aac": 92, "flac": 70, "alac": 68,
                 "wav": 55, "aiff": 52, "aif": 52, "ogg": 60, "opus": 60,
                 "wma": 20},
    "lossless": {"flac": 100, "wav": 96, "aiff": 94, "aif": 94, "alac": 92,
                 "m4a": 60, "aac": 58, "ogg": 55, "opus": 55, "mp3": 50,
                 "wma": 20},
    # Everything Apple Music will actually import, FLAC last.
    "apple": {"alac": 100, "m4a": 96, "aac": 94, "mp3": 90, "wav": 70,
              "aiff": 68, "aif": 68, "ogg": 30, "opus": 30, "flac": 25,
              "wma": 15},
}

FORMAT_PROFILE = os.environ.get("ACQUIRE_FORMAT_PROFILE", "portable").lower()
_FORMAT_SCORE = FORMAT_PROFILES.get(FORMAT_PROFILE, FORMAT_PROFILES["portable"])

# Duration bands, in seconds. A club record runs long; a radio edit is the
# version you do not want and is usually the one named most cleanly.
SHORT_EDIT_SECONDS = 240        # below this, probably a radio edit
LONG_SET_SECONDS = 1200         # above this, probably a whole mix or album side

# Filename hints. Weak on their own — anyone can type anything — but they
# correlate well enough to break a tie between otherwise equal candidates.
_EXTENDED_HINT = re.compile(
    r"\b(extended|original\s*mix|club\s*mix|12[\s\"']*inch|maxi)\b",
    re.IGNORECASE)
_SHORT_HINT = re.compile(
    r"\b(radio\s*(edit|mix|version)|short|snippet|preview|clip)\b",
    re.IGNORECASE)

# How long a transfer may sit at the same percentage before it is abandoned.
# Soulseek queues are genuinely slow — being forty deep behind someone on ADSL
# is normal — so this is generous, and measures stalling rather than duration.
STALL_SECONDS = float(os.environ.get("SLSKD_STALL_SECONDS", "600"))

# How long a *queued* transfer may sit at the same position before it is
# abandoned. Much longer than the stall timeout, because waiting in a queue is
# not stalling — it is the normal state of a Soulseek download, and being forty
# deep behind somebody on ADSL can take an hour.
#
# This is a fix, not a knob: at 0% in a queue the percentage cannot move, so
# the stall rule was killing perfectly healthy queued transfers after ten
# minutes and reporting "the peer is probably gone" about a peer who was fine.
QUEUE_PATIENCE = float(os.environ.get("SLSKD_QUEUE_PATIENCE", "3600"))

_JUNK = re.compile(r"[_\-\.]+")


class SlskdError(RuntimeError):
    pass


def _ordinal(position: Optional[int]) -> str:
    """"3rd", "21st". Read by a person waiting, so it should read like English."""
    if position is None:
        return ""
    if 10 <= position % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(position % 10, "th")
    return f"{position}{suffix}"


@dataclass(frozen=True)
class Candidate:
    username: str
    filename: str
    size: int
    extension: str
    bitrate: Optional[int]
    sample_rate: Optional[int]
    bit_depth: Optional[int]
    length: Optional[int]
    queue_length: int
    free_slot: bool
    upload_speed: int
    score: float

    @property
    def basename(self) -> str:
        return self.filename.replace("\\", "/").rsplit("/", 1)[-1]

    @property
    def full_path(self) -> str:
        """The peer's own path to the file, under the name the API uses.

        `to_dict` has always called this `full_path` — `filename` there is the
        basename, which is what a person reads. Callers that took the dict and
        callers that took the object were therefore using different names for
        the same thing, and every one of the latter raised AttributeError.

        That broke the whole one-click acquisition path, silently, because the
        only route exercised until now went straight to slskd and never
        touched a Candidate object after ranking.
        """
        return self.filename

    @property
    def duration_label(self) -> str:
        """Length as a DJ reads it — the single most telling field here."""
        if not self.length:
            return "?"
        return f"{self.length // 60}:{self.length % 60:02d}"

    @property
    def quality_label(self) -> str:
        if self.extension in _LOSSLESS:
            bits = f"{self.bit_depth}-bit " if self.bit_depth else ""
            rate = f"{self.sample_rate / 1000:.1f} kHz" if self.sample_rate else ""
            return f"{self.extension.upper()} {bits}{rate}".strip()
        if self.bitrate:
            return f"{self.extension.upper()} {self.bitrate} kbps"
        return self.extension.upper()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "username": self.username, "filename": self.basename,
            "full_path": self.filename, "size": self.size,
            "extension": self.extension, "bitrate": self.bitrate,
            "length": self.length,
            "quality_label": self.quality_label,
            "duration_label": self.duration_label,
            "lossless": self.extension in _LOSSLESS,
            "queue_length": self.queue_length, "free_slot": self.free_slot,
            "upload_speed": self.upload_speed, "score": round(self.score, 1),
        }


def _normalise(text: str) -> str:
    return _JUNK.sub(" ", (text or "").lower()).strip()


def _length_score(length: Optional[int], basename: str) -> float:
    """Reward the version a DJ would actually play.

    The preference is absolute, not relative to any reference length, and that
    distinction matters. Shazam identifies whichever recording it matched,
    which for a lot of dance records is the radio edit — so treating the
    identified track's duration as a target would systematically reject the
    extended mix, the one thing worth having. Longer simply wins.

    A radio edit is close to useless at the decks: no intro to beatmatch into,
    no outro to mix out of. It is penalised hard rather than merely ranked
    below, because a radio edit that arrives is worse than nothing arriving —
    it looks like the job is done.

    Past twenty minutes it stops being a track: a mix, an album side, or a
    mislabelled set.

    An unknown length is neither rewarded nor punished. Plenty of peers report
    nothing, and refusing them would discard good files over a missing field.
    """
    score = 0.0

    if length:
        if length < 60:
            score -= 60             # a snippet or a broken file
        elif length < SHORT_EDIT_SECONDS:
            # Steep and scaled: 3'30 is a plausible club record, 2'00 is not.
            score -= 55 * (SHORT_EDIT_SECONDS - length) / SHORT_EDIT_SECONDS
        elif length <= LONG_SET_SECONDS:
            # Rises across the whole band, so nine minutes beats five outright
            # rather than by a rounding error.
            score += 15 + 35 * min(1.0, (length - SHORT_EDIT_SECONDS) / 360)
        else:
            score -= 30             # a whole mix, not a track

    if _EXTENDED_HINT.search(basename):
        score += 25
    if _SHORT_HINT.search(basename):
        score -= 45

    return score


def score_candidate(raw_file: Dict[str, Any], user: Dict[str, Any],
                    wanted: str) -> Optional[Candidate]:
    """Rank one search hit. Returns None for files that are not audio."""
    filename = raw_file.get("filename") or ""
    ext = (raw_file.get("extension")
           or filename.replace("\\", "/").rsplit(".", 1)[-1]).lower().lstrip(".")
    if ext not in _FORMAT_SCORE:
        return None

    bitrate = raw_file.get("bitRate")
    score = float(_FORMAT_SCORE[ext])

    # A lossy file is only as good as its bitrate.
    if ext not in _LOSSLESS and bitrate:
        score += min(45.0, bitrate / 320 * 45)
    elif ext not in _LOSSLESS:
        score -= 10  # unknown bitrate on a lossy file is a bad sign

    # Does the filename actually name the track we asked for?
    basename = _normalise(filename.replace("\\", "/").rsplit("/", 1)[-1])
    similarity = SequenceMatcher(None, _normalise(wanted), basename).ratio()
    score += similarity * 60

    # Length. For a DJ this is not a detail: the extended mix is the record and
    # the radio edit is the thing that ruins a transition. slskd reports the
    # duration, so a two-minute cut can be turned down before it is downloaded
    # rather than discovered at the decks.
    score += _length_score(raw_file.get("length"), basename)

    # Availability: a perfect file behind a 40-deep queue is worse than a very
    # good one we can start now.
    if user.get("hasFreeUploadSlot"):
        score += 15
    queue = int(user.get("queueLength") or 0)
    score -= min(25.0, queue * 1.5)
    score += min(10.0, (user.get("uploadSpeed") or 0) / 100_000)

    return Candidate(
        username=user.get("username") or "",
        filename=filename,
        size=int(raw_file.get("size") or 0),
        extension=ext,
        bitrate=bitrate,
        sample_rate=raw_file.get("sampleRate"),
        bit_depth=raw_file.get("bitDepth"),
        length=raw_file.get("length"),
        queue_length=queue,
        free_slot=bool(user.get("hasFreeUploadSlot")),
        upload_speed=int(user.get("uploadSpeed") or 0),
        score=score,
    )


class SlskdClient:
    """Thin async client over the slskd v0 API."""

    def __init__(self, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, timeout: float = 30.0) -> None:
        self.base_url = (base_url or os.environ.get("SLSKD_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("SLSKD_API_KEY", "")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _request(self, method: str, path: str, **kwargs) -> Any:
        import aiohttp

        if not self.configured:
            raise SlskdError(
                "Soulseek is not configured. Set SLSKD_URL (and SLSKD_API_KEY) "
                "to point at a running slskd instance."
            )
        url = f"{self.base_url}/api/v0{path}"
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(method, url, headers=self._headers(),
                                           **kwargs) as resp:
                    if resp.status == 401:
                        raise SlskdError("slskd rejected the API key.")
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        raise SlskdError(f"slskd {resp.status} on {path}: {body}")
                    if resp.content_type == "application/json":
                        return await resp.json()
                    return await resp.text()
        except aiohttp.ClientError as exc:
            raise SlskdError(f"Cannot reach slskd at {self.base_url}: {exc}") from exc

    async def health(self) -> bool:
        try:
            await self._request("GET", "/application")
            return True
        except SlskdError:
            return False

    # Long enough for a search to finish. slskd reports peers arriving within
    # a second or two, but it only *hands them over* once the search reaches a
    # completed state, which its own timeout puts at about twenty seconds.
    # Twelve was the previous value, so every search returned an empty list
    # and every acquisition reported "no peer is sharing this one" — measured
    # against a query that had twenty-six peers waiting behind it.
    SEARCH_WAIT = float(os.environ.get("SLSKD_SEARCH_WAIT", "45"))

    async def search(self, query: str, *, wait: Optional[float] = None,
                     poll: float = 2.0) -> List[Candidate]:
        """Run a search and return candidates, best first.

        Soulseek searches are asynchronous by nature — peers answer over
        several seconds — so we start one, wait for it to finish, then rank.

        Waiting for *completion* rather than for a while is the point: the
        response array stays empty for the whole search and fills in at the
        end, so stopping early does not return fewer results, it returns none.
        """
        wait = self.SEARCH_WAIT if wait is None else wait
        search_id = str(uuid.uuid4())
        await self._request("POST", "/searches", json={
            "id": search_id, "searchText": query,
            "fileLimit": 200, "responseLimit": 100,
        })

        elapsed = 0.0
        payload: Dict[str, Any] = {}
        while elapsed < wait:
            await asyncio.sleep(poll)
            elapsed += poll
            payload = await self._request(
                "GET", f"/searches/{search_id}?includeResponses=true")
            if "completed" in payload.get("state", "").lower():
                break

        responses = payload.get("responses") or []
        if not responses and payload.get("responseCount"):
            # Peers answered but the array is still empty, which means the
            # search has not finished. Stopping it forces completion and
            # hands over everything that arrived — so a slow search costs you
            # the stragglers rather than the whole result.
            #
            # PUT needs a body. Without one slskd answers 415 and the search
            # keeps running, which is how this looked like "no peers" rather
            # than "not finished yet".
            logger.info("Search still running after %.0fs; taking the %s "
                        "response(s) already in", wait,
                        payload.get("responseCount"))
            try:
                await self._request("PUT", f"/searches/{search_id}", json={})
                await asyncio.sleep(1.0)
                payload = await self._request(
                    "GET", f"/searches/{search_id}?includeResponses=true")
                responses = payload.get("responses") or []
            except SlskdError as exc:
                logger.warning("Could not stop search %s: %s", search_id, exc)

        candidates: List[Candidate] = []
        for response in responses:
            for raw in response.get("files") or []:
                candidate = score_candidate(raw, response, query)
                if candidate is not None:
                    candidates.append(candidate)

        # Best-effort cleanup; a stale search is harmless if this fails.
        #
        # Deliberately after the responses have been read, and tolerant of
        # failure: slskd finalises a search a moment after reporting it
        # complete, and deleting it inside that window makes it log a database
        # concurrency error. The delete still succeeds and the results are
        # already in hand — but the log line looks like a fault, so it is
        # worth saying here that it is not one.
        try:
            await self._request("DELETE", f"/searches/{search_id}")
        except SlskdError:
            pass

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

    async def transfer_state(self, username: str,
                             filename: str) -> Optional[Dict[str, Any]]:
        """Where one download has got to, or None if slskd has forgotten it.

        slskd tracks transfers per peer, so this is a lookup rather than a
        subscription. Everything the interface needs is here — state, percent,
        speed, and how far down the queue we are — and none of it was being
        read, so a download that finished looked identical to one that hung.
        """
        try:
            peers = await self._request(
                "GET", f"/transfers/downloads/{quote(username, safe='')}")
        except SlskdError:
            return None
        if not isinstance(peers, dict):
            return None

        for directory in peers.get("directories") or []:
            for entry in directory.get("files") or []:
                if entry.get("filename") != filename:
                    continue
                state = str(entry.get("state") or "")
                size = int(entry.get("size") or 0)
                done = int(entry.get("bytesTransferred") or 0)
                return {
                    "state": state,
                    # Its own field rather than derived from the state string:
                    # slskd spells states as "Completed, Succeeded" and
                    # "Completed, Errored", and the second word is the one
                    # that matters.
                    "finished": "succeeded" in state.lower(),
                    "failed": any(word in state.lower() for word in
                                  ("errored", "cancelled", "rejected",
                                   "timedout")),
                    "percent": float(entry.get("percentComplete") or 0.0),
                    "transferred": done,
                    "size": size,
                    "speed": float(entry.get("averageSpeed") or 0.0),
                    "queue_position": entry.get("placeInQueue"),
                    "remaining_seconds": entry.get("remainingTime"),
                }
        return None

    async def enqueue(self, candidate: Candidate) -> Dict[str, Any]:
        """Queue a download. Returns immediately; transfer runs in slskd."""
        await self._request(
            "POST", f"/transfers/downloads/{candidate.username}",
            json=[{"filename": candidate.filename, "size": candidate.size}],
        )
        return {"queued": True, "username": candidate.username,
                "filename": candidate.basename}

    async def transfer(self, username: str, filename: str
                       ) -> Optional[Dict[str, Any]]:
        """State of one transfer, or None once slskd has forgotten it."""
        for entry in await self.downloads():
            if entry["username"] == username and entry["full_path"] == filename:
                return entry
        return None

    async def await_transfer(self, username: str, filename: str,
                             timeout: float = 1800.0, poll: float = 5.0,
                             on_progress=None) -> Dict[str, Any]:
        """Wait for a queued download to finish.

        Soulseek transfers are not a request-response: a peer can queue you
        behind forty other people, throttle you to a trickle, or go offline
        halfway through. So this reports what it sees rather than assuming
        progress, and gives up on a stall rather than on the clock alone — an
        hour of genuine downloading is fine, ten minutes of nothing is not.
        """
        import asyncio

        started = time.monotonic()
        last_change = started
        last_signal: Tuple[float, Optional[int]] = (-1.0, None)

        while True:
            entry = await self.transfer(username, filename)
            if entry is None:
                # slskd drops completed transfers from the list after a while.
                raise SlskdError(
                    "The transfer disappeared from slskd before completing. "
                    "The peer may have gone offline."
                )

            state = (entry.get("state") or "").lower()
            percent = float(entry.get("percent") or 0)
            # How far down the peer's queue we are. Reported by slskd and,
            # until now, thrown away — which is why a queued download showed
            # "0%" for as long as it lasted with nothing to say whether it was
            # moving.
            place = entry.get("placeInQueue")
            position = int(place) if isinstance(place, (int, float)) else None
            queued = "queued" in state
            if on_progress:
                on_progress(percent, state, position)

            if "completed" in state and "succeeded" in state:
                return entry
            if any(word in state for word in
                   ("cancelled", "errored", "rejected", "timedout", "failed")):
                raise SlskdError(f"Transfer failed: {entry.get('state')}")

            # Moving up a queue is progress, exactly as much as a percentage
            # is. Measuring only the percentage meant a transfer climbing from
            # 40th to 3rd looked identical to a dead one.
            signal = (percent, position)
            patience = QUEUE_PATIENCE if queued else STALL_SECONDS
            if signal != last_signal:
                last_signal, last_change = signal, time.monotonic()
            elif time.monotonic() - last_change > patience:
                if queued:
                    raise SlskdError(
                        f"Still {_ordinal(position)} in {username}'s queue after "
                        f"{patience // 60:.0f} minutes without moving."
                        if position else
                        f"Queued by {username} for {patience // 60:.0f} minutes "
                        f"without moving.")
                raise SlskdError(
                    f"Transfer stalled at {percent:.0f}% for "
                    f"{STALL_SECONDS // 60} minutes. The peer is probably gone."
                )

            if time.monotonic() - started > timeout:
                raise SlskdError(
                    f"Transfer did not finish within {timeout / 60:.0f} minutes"
                )
            await asyncio.sleep(poll)

    async def downloads(self) -> List[Dict[str, Any]]:
        """Current transfer state, flattened for the UI."""
        raw = await self._request("GET", "/transfers/downloads")
        out: List[Dict[str, Any]] = []
        for user in raw or []:
            for directory in user.get("directories") or []:
                for f in directory.get("files") or []:
                    out.append({
                        "username": user.get("username", ""),
                        "full_path": f.get("filename") or "",
                        "filename": (f.get("filename") or "")
                                    .replace("\\", "/").rsplit("/", 1)[-1],
                        "state": f.get("state", ""),
                        "percent": round(f.get("percentComplete") or 0, 1),
                        "size": f.get("size", 0),
                        "speed": f.get("averageSpeed", 0),
                        # Where slskd wrote it. Absent until the transfer ends.
                        "local_path": f.get("localPath") or "",
                    })
        return out


# Everything a peer's filename is unlikely to spell the way we were given it.
# Peers match a plain substring against their own filenames, so each of these
# left in the query is a chance to match nothing at all.
_BRACKETED = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_FEATURING = re.compile(r"\b(feat|ft|featuring|with)\b.*", re.IGNORECASE)
# Everything that is not a letter, a digit or a space.
_NON_WORD = re.compile(r"[^0-9A-Za-z ]+")

# Letters that NFKD will not decompose, because they are letters in their own
# right rather than a base plus an accent. Without these, stripping what is
# left of them deletes the letter instead of replacing it: "Møme" becomes
# "M me" and matches nothing. An accent removed has to leave a word behind.
_TRANSLITERATE = {
    "ø": "o", "Ø": "O", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ß": "ss", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "TH", "ħ": "h", "Ħ": "H", "ı": "i", "ŋ": "n", "Ŋ": "N",
    # Not letters, but they hold words together and dropping them fuses two
    # into one: "hip-hop" must not become "hiphop".
    "-": " ", "–": " ", "—": " ", "/": " ", "\\": " ", "_": " ",
}


def search_query(artist: str, title: str) -> str:
    """Build the search string Soulseek responds best to.

    Peers index by filename and match a plain substring, so anything in the
    query that a filename might spell differently costs the whole result
    rather than narrowing it. What Shazam hands over is full of exactly that:
    "(Extended Mix)", "[Axwell Mix]", "(feat. Georgi Kay)", ampersands between
    artists — none of which a given uploader is obliged to have written.

    Measured against the live network, on tracks from real sets:

        Todd Terry & Sound Design — Bounce to the Beat (Chris Stussy Remix)
            with the suffix     9 results
            without           166

        Ivan Gough & Feenixpawl — In My Mind (feat. Georgi Kay) [Axwell Mix]
            with               60
            without           193

    Dropping the mix name does not mean settling for the wrong version:
    ranking still prefers extended mixes on length and penalises radio edits,
    and it can only do that over candidates the search actually returned.

    Whitespace is collapsed last: stripping punctuation from a name like
    "Fred again.." leaves a run of spaces, and slskd forwards the query
    verbatim.
    """
    text = f"{artist} {title}"
    text = _BRACKETED.sub(" ", text)
    text = _FEATURING.sub(" ", text)
    # Then every remaining character that is not a letter, a digit or a space.
    # Apostrophes, ampersands, accents, slashes, quotes — each one is a way
    # for the query to differ from a filename that holds the very track we
    # want. Names are folded to ASCII first, so "Irène" finds "Irene".
    folded = "".join(_TRANSLITERATE.get(c, c) for c in text)
    folded = unicodedata.normalize("NFKD", folded)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    cleaned = re.sub(r"\s+", " ", _NON_WORD.sub(" ", folded)).strip()
    # Never return nothing: a title that is entirely bracketed would otherwise
    # search for the empty string, which matches everything.
    return cleaned or re.sub(r"\s+", " ", _JUNK.sub(" ", f"{artist} {title}")).strip()

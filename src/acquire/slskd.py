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
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Quality score by container/bitrate. Deliberately steep: a lossless file is
# worth waiting in a queue for, a 128 kbps file is barely worth downloading.
_FORMAT_SCORE = {"flac": 100, "wav": 96, "aiff": 94, "aif": 94, "alac": 92,
                 "m4a": 60, "ogg": 55, "opus": 55, "mp3": 50, "wma": 20}

_LOSSLESS = {"flac", "wav", "aiff", "aif", "alac"}

# How long a transfer may sit at the same percentage before it is abandoned.
# Soulseek queues are genuinely slow — being forty deep behind someone on ADSL
# is normal — so this is generous, and measures stalling rather than duration.
STALL_SECONDS = float(os.environ.get("SLSKD_STALL_SECONDS", "600"))

_JUNK = re.compile(r"[_\-\.]+")


class SlskdError(RuntimeError):
    pass


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
            "quality_label": self.quality_label, "lossless": self.extension in _LOSSLESS,
            "queue_length": self.queue_length, "free_slot": self.free_slot,
            "upload_speed": self.upload_speed, "score": round(self.score, 1),
        }


def _normalise(text: str) -> str:
    return _JUNK.sub(" ", (text or "").lower()).strip()


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

    async def search(self, query: str, *, wait: float = 12.0,
                     poll: float = 1.5) -> List[Candidate]:
        """Run a search and return candidates, best first.

        Soulseek searches are asynchronous by nature — peers answer over
        several seconds — so we start one, let responses accumulate, then rank.
        """
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
            if payload.get("state", "").lower().startswith("completed"):
                break

        candidates: List[Candidate] = []
        for response in payload.get("responses") or []:
            for raw in response.get("files") or []:
                candidate = score_candidate(raw, response, query)
                if candidate is not None:
                    candidates.append(candidate)

        # Best-effort cleanup; a stale search is harmless if this fails.
        try:
            await self._request("DELETE", f"/searches/{search_id}")
        except SlskdError:
            pass

        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates

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
        last_seen = -1.0

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
            if on_progress:
                on_progress(percent, state)

            if "completed" in state and "succeeded" in state:
                return entry
            if any(word in state for word in
                   ("cancelled", "errored", "rejected", "timedout", "failed")):
                raise SlskdError(f"Transfer failed: {entry.get('state')}")

            if percent > last_seen:
                last_seen, last_change = percent, time.monotonic()
            elif time.monotonic() - last_change > STALL_SECONDS:
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


def search_query(artist: str, title: str) -> str:
    """Build the search string Soulseek responds best to.

    Peers index by filename, so punctuation and mix suffixes hurt more than
    they help — "artist title" finds what "Artist - Title (Original Mix)"
    misses.

    Whitespace is collapsed at the end: stripping punctuation from a name like
    "Fred again.." leaves a run of spaces, and slskd forwards the query
    verbatim to peers whose matching is a plain substring test.
    """
    stripped = _JUNK.sub(" ", f"{artist} {title}")
    return re.sub(r"\s+", " ", stripped).strip()

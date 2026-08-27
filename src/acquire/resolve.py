"""Where to get a track, in order of preference.

The ladder puts stores first. That is not a disclaimer bolted on — it reflects
what a digger actually wants: a purchased FLAC comes with correct tags, a
catalogue number, and it supports the label you just discovered. Soulseek sits
below it as an opt-in path, and only appears at all when you have configured
your own slskd instance.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, List
from urllib.parse import quote_plus


@dataclass(frozen=True)
class Source:
    kind: str          # "store" | "stream" | "p2p"
    name: str
    url: str
    quality: str
    note: str = ""
    actionable: bool = False   # True when the app can fetch it directly

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def resolve(artist: str, title: str, *, isrc: str = "",
            soulseek_configured: bool = False) -> List[Source]:
    """Build the acquisition ladder for one track."""
    query = quote_plus(f"{artist} {title}".strip())
    sources: List[Source] = []

    if artist or title:
        sources.append(Source(
            kind="store", name="Bandcamp",
            url=f"https://bandcamp.com/search?q={query}&item_type=t",
            quality="Often WAV / FLAC",
            note="Best margin for the artist",
        ))
        sources.append(Source(
            kind="store", name="Beatport",
            url=f"https://www.beatport.com/search?q={query}",
            quality="WAV / AIFF / MP3 320",
            note="Catalogue numbers and release dates",
        ))
        sources.append(Source(
            kind="store", name="Discogs",
            url=f"https://www.discogs.com/search/?q={query}&type=release",
            quality="Physical / marketplace",
            note="For vinyl-only and unreleased pressings",
        ))
        sources.append(Source(
            kind="stream", name="SoundCloud",
            url=f"https://soundcloud.com/search/sounds?q={query}",
            quality="Original file when the uploader allows it",
            note="yt-dlp takes the `download` format first",
        ))
        sources.append(Source(
            kind="stream", name="YouTube",
            url=f"https://www.youtube.com/results?search_query={query}",
            quality="Opus ~160 kbps ceiling",
            note="Do not transcode this to 320 — it only grows the file",
        ))

    if soulseek_configured:
        sources.append(Source(
            kind="p2p", name="Soulseek",
            url="", quality="FLAC / MP3 320 when shared",
            note="Requires your own account and shared folder",
            actionable=True,
        ))

    return sources


def soulseek_configured() -> bool:
    return bool(os.environ.get("SLSKD_URL"))

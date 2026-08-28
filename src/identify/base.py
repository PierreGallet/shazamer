"""The identification contract, and the title normalisation everything shares.

Adding a provider (AcoustID, AudD, a local fingerprint index) means writing one
class that satisfies `Identifier`. Nothing above this layer knows which one is
in use.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class TrackMatch:
    """One identified track, provider-agnostic."""
    title: str
    artist: str
    provider: str
    url: str = ""
    cover_url: str = ""
    album: str = ""
    label: str = ""
    year: str = ""
    genre: str = ""
    isrc: str = ""
    # A ~30 second excerpt of the record itself, for checking by ear that the
    # thing named is the thing playing. Shazam hands one over with the match;
    # it is the same Apple Music preview the iTunes API returns, so a track
    # identified before this existed can still get one looked up.
    preview_url: str = ""
    raw_matches: int = 0

    @property
    def key(self) -> str:
        return normalize_key(self.artist, self.title)

    @property
    def display(self) -> str:
        return f"{self.artist} — {self.title}"


@runtime_checkable
class Identifier(Protocol):
    """Anything that can name a track from a few seconds of audio."""

    name: str

    async def identify(self, wav_bytes: bytes) -> Optional[TrackMatch]:
        ...


# Suffixes that name the same recording. Merging them is what stops a set from
# listing "Track", "Track (Original Mix)" and "Track - Extended" as three finds.
_NOISE_SUFFIXES = re.compile(
    r"\s*[\(\[\-—]\s*"
    r"(original mix|original|extended mix|extended version|extended|radio edit|"
    r"radio mix|radio version|club mix|club edit|album version|single version|"
    r"official (music )?video|official audio|lyric video|hd|hq|remaster(ed)?"
    r"(\s*\d{4})?|free download|full version)"
    r"\s*[\)\]]?\s*$",
    re.IGNORECASE,
)

_FEAT = re.compile(r"\s*[\(\[]?\s*(feat\.?|ft\.?|featuring|with)\s+[^\)\]]*[\)\]]?",
                   re.IGNORECASE)
_PUNCT = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


def normalize_title(value: str) -> str:
    """Strip the decorations that make the same track look like several."""
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()

    # Applied repeatedly: "Track (Original Mix) [Remastered]" needs two passes.
    for _ in range(3):
        stripped = _NOISE_SUFFIXES.sub("", text).strip()
        if stripped == text:
            break
        text = stripped

    text = _FEAT.sub(" ", text)
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def normalize_artist(value: str) -> str:
    """Normalise an artist credit down to its primary name.

    Shazam credits collaborations inconsistently — "A & B", "A, B", "A x B" —
    so we key on the first credited artist and keep the full string for display.
    """
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold()
    text = _FEAT.sub(" ", text)
    text = re.split(r"\s*(?:,|&|\bx\b|\bvs\.?\b|\band\b)\s*", text)[0]
    text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def normalize_key(artist: str, title: str) -> str:
    """The identity used for voting, merging and library lookups."""
    return f"{normalize_artist(artist)}::{normalize_title(title)}"

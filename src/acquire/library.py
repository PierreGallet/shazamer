"""What happens to a file once Soulseek has actually delivered it.

Queueing a download was where this stopped before: slskd dropped the file in
its own directory and you went looking for it yourself. The steps between
"a peer sent bytes" and "this is a record in my collection" are the ones that
were missing.

Three of them, in order of how much they matter:

**Verify.** Soulseek filenames lie. A file called "Artist - Track.flac" is
whatever the uploader decided to call it — mislabelled rips, wrong versions and
the occasional entirely different song are routine. So the download is
fingerprinted with the same identifier used to find it, and a file that does
not come back as the track we asked for is rejected rather than filed.

**Tag.** A verified file gets the metadata already established — artist,
title, label, catalogue number, year — so it arrives in a DJ library correct
rather than as whatever the uploader typed.

**Keep, briefly.** The server is not storage. Files are served to the browser
and swept on the same schedule as set audio.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class VerificationFailed(RuntimeError):
    """The delivered file is not the track that was asked for."""


@dataclass(frozen=True)
class AcquiredFile:
    path: Path
    verified: bool
    verified_as: str = ""
    note: str = ""


def safe_filename(artist: str, title: str, extension: str) -> str:
    """A filename that survives every filesystem and reads correctly."""
    stem = f"{artist} - {title}".strip(" -") or "track"
    stem = _UNSAFE.sub("_", stem)[:150].strip()
    extension = (extension or "").lstrip(".").lower() or "bin"
    return f"{stem}.{extension}"


async def verify(path: Path, identifier, expected_key: str,
                 sample_at: float = 45.0) -> tuple[bool, str]:
    """Fingerprint the file and check it is the track we went looking for.

    A window from partway in, not the opening: intros are quiet, sometimes
    silent, and a fingerprint of silence identifies nothing. If the file is too
    short for that the middle is used instead.

    Returns (matched, what_it_actually_is). A file that cannot be read at all
    counts as unverified rather than wrong — the difference matters, because
    one is a bad download and the other might be an unusual codec.
    """
    from src.core import audio as audio_io

    try:
        duration = await audio_io.probe_duration(str(path))
        start = sample_at if duration > sample_at + 20 else max(0.0, duration / 2)
        wav = await audio_io.extract_probe(str(path), start, 12.0)
    except Exception as exc:
        logger.warning("Could not read %s for verification: %s", path.name, exc)
        return False, ""

    match = await identifier.identify(wav)
    if match is None:
        return False, ""
    return match.key == expected_key, match.display


def tag(path: Path, meta: Dict[str, Any]) -> bool:
    """Write metadata into the file. False when the format cannot carry it.

    Best-effort by design: a correctly named file with no tags is still a
    usable record, so a tagging failure must not discard the download.
    """
    try:
        import mutagen
        from mutagen.easyid3 import EasyID3
        from mutagen.id3 import ID3NoHeaderError
    except ImportError:
        logger.warning("mutagen is not installed; leaving %s untagged", path.name)
        return False

    fields = {
        "artist": meta.get("artist", ""),
        "title": meta.get("title", ""),
        "album": meta.get("album", "") or meta.get("label", ""),
        "date": str(meta.get("year", "") or ""),
        "genre": meta.get("genre", "") or "",
    }
    fields = {k: v for k, v in fields.items() if v}

    try:
        if path.suffix.lower() == ".mp3":
            try:
                audio = EasyID3(str(path))
            except ID3NoHeaderError:
                audio = mutagen.File(str(path), easy=True)
                if audio is None:
                    return False
                audio.add_tags()
        else:
            audio = mutagen.File(str(path), easy=True)
            if audio is None:
                return False

        for key, value in fields.items():
            try:
                audio[key] = value
            except (KeyError, ValueError):
                continue                # this format has no such field
        audio.save()
        return True
    except Exception as exc:
        logger.warning("Could not tag %s: %s", path.name, exc)
        return False


async def collect(source: Path, destination_dir: Path, artist: str, title: str,
                  identifier=None, expected_key: str = "",
                  meta: Optional[Dict[str, Any]] = None,
                  require_verification: bool = True) -> AcquiredFile:
    """Verify, name, tag and file a downloaded track.

    `require_verification` decides what happens to a file whose fingerprint
    does not match. Rejecting is the default: a wrong file filed under the
    right name is worse than no file, because it is discovered at the decks.
    """
    if not source.exists():
        raise FileNotFoundError(f"slskd reported a file that is not there: {source}")

    verified, actually = False, ""
    if identifier is not None and expected_key:
        verified, actually = await verify(source, identifier, expected_key)
        if not verified and require_verification:
            raise VerificationFailed(
                f"The file is {actually or 'unrecognisable'}, not "
                f"{artist} — {title}. Soulseek filenames are whatever the "
                f"uploader typed, so this one was wrong."
            )

    destination_dir.mkdir(parents=True, exist_ok=True)
    target = destination_dir / safe_filename(artist, title, source.suffix)
    if target.exists():
        target = destination_dir / safe_filename(
            artist, f"{title} ({source.stem[:12]})", source.suffix)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, shutil.copy2, str(source), str(target))

    if meta:
        await loop.run_in_executor(None, tag, target,
                                   {**meta, "artist": artist, "title": title})

    return AcquiredFile(
        path=target, verified=verified, verified_as=actually,
        note="" if verified else "not fingerprint-verified",
    )

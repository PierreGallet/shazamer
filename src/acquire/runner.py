"""Getting one track: search, download, verify, file.

Everything between clicking "Get" and having a tagged file. Runs as a job
because a Soulseek transfer is not a request-response — a peer can queue you
behind forty people and take an hour, or vanish at 60%.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from .library import VerificationFailed, collect
from .slskd import Candidate, SlskdClient, SlskdError, search_query

logger = logging.getLogger(__name__)

# Where slskd writes completed transfers, as this container sees it. The two
# containers must agree on this path or the file is downloaded and then lost.
SLSKD_DOWNLOADS = Path(os.environ.get("SLSKD_DOWNLOADS_DIR", "/downloads"))

# Rejecting a mismatched file is the default: a wrong record filed under the
# right name is worse than no record, because it is found out at the decks.
# Off. The fingerprint labels a download; it does not decide whether you may
# have it. You asked for the file and Soulseek delivered it — refusing to
# hand it over on the strength of twelve seconds of audio is this program
# overruling its user about their own download.
REQUIRE_VERIFICATION = os.environ.get("ACQUIRE_REQUIRE_VERIFICATION",
                                      "false").lower() == "true"


async def rank_candidates(artist: str, title: str, limit: int = 5,
                          client: Optional[SlskdClient] = None
                          ) -> Dict[str, Any]:
    """Search Soulseek and return the best few, best first.

    Separate from fetching so the choice can be shown before anything is
    downloaded. That matters more here than in most places: filenames on
    Soulseek are whatever the uploader typed, and the difference between the
    extended mix and the radio edit is invisible until someone looks.
    """
    client = client or SlskdClient()

    if not client.configured:
        raise SlskdError("Soulseek is not configured on this server.")

    candidates = await client.search(search_query(artist, title))
    return {
        "query": search_query(artist, title),
        "candidates": [c.to_dict() for c in candidates[:limit]],
        "total": len(candidates),
    }


def _locate(entry: Dict[str, Any]) -> Optional[Path]:
    """Find the file slskd just finished writing.

    Three attempts, because slskd's own view of where it put something is not
    ours and its filing is not flat.

    It reports `localPath` from inside its container — `/downloads/music/x.mp3`
    — which is mounted elsewhere here. And it recreates the *peer's* directory
    structure underneath, so a file lands in `music/` or `not in current
    sets/` rather than at the top. Looking only at the root found nothing and
    reported a transfer that had in fact succeeded as a failure.
    """
    reported = Path(entry.get("local_path") or "")
    if reported.is_absolute() and reported.exists():
        return reported

    wanted = Path(entry.get("full_path", "").replace("\\", "/")).name
    if not wanted:
        return None

    # slskd's path, remapped onto our mount: everything after its own
    # downloads root is the part that is the same on both sides.
    if reported.parts:
        for i, part in enumerate(reported.parts):
            if part in ("downloads", "download"):
                candidate = SLSKD_DOWNLOADS.joinpath(*reported.parts[i + 1:])
                if candidate.exists():
                    return candidate
                break

    # Failing that, look for the name. Newest first: slskd appends a suffix
    # when a name is already taken, so a second fetch of the same record
    # leaves two files and the one just written is the one meant.
    matches = sorted(
        (p for p in SLSKD_DOWNLOADS.rglob("*") if p.is_file()
         and p.name == wanted),
        key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


async def acquire_track(library, destination: Path, track_key: str,
                        artist: str, title: str,
                        download_id: int,
                        meta: Optional[Dict[str, Any]] = None,
                        client: Optional[SlskdClient] = None,
                        identifier=None,
                        chosen: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Fetch one track and return the download row.

    Every failure is written to the row rather than raised at the caller: the
    person who clicked the button is not watching a stack trace, and "the peer
    went offline at 60%" is a genuinely different outcome from "nobody has it".
    """
    client = client or SlskdClient()

    # Declared out here so the failure handlers below can drain them. Defining
    # them inside the try would leave the handlers referring to a name that
    # does not exist when the failure came first.
    progress_writes: List[Any] = []

    async def settle_progress() -> None:
        """Let every queued progress write finish before the verdict.

        Untracked, the last one landed *after* the outcome and overwrote it: a
        failed download displayed "failed" beside "Downloading... 100%", with
        the actual reason gone.
        """
        import asyncio
        if progress_writes:
            await asyncio.gather(*progress_writes, return_exceptions=True)
            progress_writes.clear()

    async def note(status: str, message: str, **extra) -> None:
        await library.update_download(download_id, status=status,
                                      message=message, **extra)

    try:
        if not client.configured:
            await note("failed", "Soulseek is not configured on this server.")
            return await library.get_download(download_id, user_id=None)

        if chosen:
            # A candidate the user picked from the list. Trusted as their
            # choice, still verified by fingerprint afterwards like any other.
            best = Candidate(
                username=chosen["username"], filename=chosen["full_path"],
                size=int(chosen.get("size") or 0),
                extension=(chosen.get("extension") or "").lower(),
                bitrate=chosen.get("bitrate"), sample_rate=None,
                bit_depth=None, length=chosen.get("length"),
                queue_length=int(chosen.get("queue_length") or 0),
                free_slot=bool(chosen.get("free_slot")),
                upload_speed=int(chosen.get("upload_speed") or 0),
                score=float(chosen.get("score") or 0),
            )
        else:
            await note("queued", "Searching Soulseek...")
            candidates = await client.search(search_query(artist, title))
            if not candidates:
                await note("failed",
                           "No peer is sharing this one right now. The pool "
                           "changes constantly — worth retrying.")
                return await library.get_download(download_id, user_id=None)
            best = candidates[0]
        logger.info("Best candidate for %s - %s: %s from %s",
                    artist, title, best.quality_label, best.username)
        # Who it is coming from, not only what. A Soulseek transfer depends
        # entirely on one stranger's machine staying online and their queue
        # moving, so the peer is the part that explains why it is fast, slow
        # or stuck — and it is already on the row, just never said out loud.
        await note("downloading",
                   f"Downloading {best.quality_label} from {best.username}...",
                   username=best.username, remote_path=best.full_path,
                   quality=best.quality_label, size=best.size)

        await client.enqueue(best)

        def on_progress(percent: float, state: str) -> None:
            # Fire-and-forget, because awaiting a database write inside the
            # poll loop would slow the loop for no benefit — but *tracked*,
            # and drained before anything final is written.
            #
            # Untracked, the last of these landed after the outcome and
            # overwrote it: a download that failed displayed "failed" next to
            # "Downloading... 100%", with the actual reason gone. The status
            # said one thing and the message said another, and neither said
            # what went wrong.
            import asyncio
            progress_writes.append(asyncio.create_task(
                library.update_download(
                    download_id, progress=percent,
                    message=f"Downloading from {best.username}... "
                            f"{percent:.0f}%")
            ))


        entry = await client.await_transfer(best.username, best.full_path,
                                            on_progress=on_progress)

        await settle_progress()

        source = _locate(entry)
        if source is None:
            wanted = Path(entry.get("full_path", "").replace("\\", "/")).name
            await note("failed",
                       f"slskd finished the transfer but {wanted!r} is not "
                       f"under {SLSKD_DOWNLOADS}. Check that "
                       f"SLSKD_DOWNLOADS_DIR matches the volume slskd writes "
                       f"to.")
            return await library.get_download(download_id, user_id=None)

        await note("verifying", "Checking it is the right track...")
        acquired = await collect(
            source, destination, artist, title,
            identifier=identifier, expected_key=track_key, meta=meta,
            require_verification=REQUIRE_VERIFICATION,
        )

        # Ready either way. The fingerprint's verdict rides along in the
        # message, because "this sounds like a mix of three other records" is
        # worth knowing before you load it — and worth nothing if it stops you
        # having the file you asked for.
        if acquired.verified:
            message = "Ready"
        elif acquired.verified_as:
            message = f"Ready — but this sounds like {acquired.verified_as}"
        else:
            message = "Ready — could not confirm what this is"

        await note("ready", message,
                   local_path=str(acquired.path),
                   verified=1 if acquired.verified else 0, progress=100)
        logger.info("Acquired %s - %s -> %s", artist, title, acquired.path.name)

    except VerificationFailed as exc:
        await settle_progress()
        await note("failed", str(exc))
    except SlskdError as exc:
        await settle_progress()
        await note("failed", str(exc))
    except Exception as exc:
        # Drained here too, not only on the happy path: a failure at 99% is
        # exactly when a late progress write lands on top of the reason.
        await settle_progress()
        logger.exception("Acquisition of %s - %s failed", artist, title)
        await note("failed", f"Unexpected failure: {exc}")

    return await library.get_download(download_id, user_id=None)

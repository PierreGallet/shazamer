"""Getting one track: search, download, verify, file.

Everything between clicking "Get" and having a tagged file. Runs as a job
because a Soulseek transfer is not a request-response — a peer can queue you
behind forty people and take an hour, or vanish at 60%.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .library import VerificationFailed, collect
from .slskd import Candidate, SlskdClient, SlskdError, search_query

logger = logging.getLogger(__name__)

# Where slskd writes completed transfers, as this container sees it. The two
# containers must agree on this path or the file is downloaded and then lost.
SLSKD_DOWNLOADS = Path(os.environ.get("SLSKD_DOWNLOADS_DIR", "/downloads"))

# Rejecting a mismatched file is the default: a wrong record filed under the
# right name is worse than no record, because it is found out at the decks.
REQUIRE_VERIFICATION = os.environ.get("ACQUIRE_REQUIRE_VERIFICATION",
                                      "true").lower() != "false"


async def acquire_track(library, destination: Path, track_key: str,
                        artist: str, title: str,
                        download_id: int,
                        meta: Optional[Dict[str, Any]] = None,
                        client: Optional[SlskdClient] = None,
                        identifier=None) -> Dict[str, Any]:
    """Fetch one track and return the download row.

    Every failure is written to the row rather than raised at the caller: the
    person who clicked the button is not watching a stack trace, and "the peer
    went offline at 60%" is a genuinely different outcome from "nobody has it".
    """
    client = client or SlskdClient()

    async def note(status: str, message: str, **extra) -> None:
        await library.update_download(download_id, status=status,
                                      message=message, **extra)

    try:
        if not client.configured:
            await note("failed", "Soulseek is not configured on this server.")
            return await library.get_download(download_id)

        await note("queued", "Searching Soulseek...")
        candidates = await client.search(search_query(artist, title))
        if not candidates:
            await note("failed", "No peer is sharing this one right now. The "
                                 "pool changes constantly — worth retrying.")
            return await library.get_download(download_id)

        best = candidates[0]
        logger.info("Best candidate for %s - %s: %s from %s",
                    artist, title, best.quality_label, best.username)
        await note("downloading", f"Downloading {best.quality_label}...",
                   username=best.username, remote_path=best.full_path,
                   quality=best.quality_label, size=best.size)

        await client.enqueue(best)

        def on_progress(percent: float, state: str) -> None:
            # Fire-and-forget: progress is a nicety, and awaiting a DB write
            # inside the poll loop would slow the loop for no benefit.
            import asyncio
            asyncio.create_task(
                library.update_download(download_id, progress=percent,
                                        message=f"Downloading... {percent:.0f}%")
            )

        entry = await client.await_transfer(best.username, best.full_path,
                                            on_progress=on_progress)

        source = Path(entry.get("local_path") or "")
        if not source.is_absolute() or not source.exists():
            # slskd reports its own view of the path; map it into ours.
            source = SLSKD_DOWNLOADS / Path(entry["full_path"].replace("\\\\", "/")).name
        if not source.exists():
            await note("failed",
                       f"slskd finished the transfer but the file is not at "
                       f"{source}. Check that SLSKD_DOWNLOADS_DIR matches the "
                       f"volume slskd writes to.")
            return await library.get_download(download_id)

        await note("verifying", "Checking it is the right track...")
        acquired = await collect(
            source, destination, artist, title,
            identifier=identifier, expected_key=track_key, meta=meta,
            require_verification=REQUIRE_VERIFICATION,
        )

        await note("ready",
                   "Ready" if acquired.verified else "Ready (unverified)",
                   local_path=str(acquired.path),
                   verified=1 if acquired.verified else 0, progress=100)
        logger.info("Acquired %s - %s -> %s", artist, title, acquired.path.name)

    except VerificationFailed as exc:
        await note("failed", str(exc))
    except SlskdError as exc:
        await note("failed", str(exc))
    except Exception as exc:
        logger.exception("Acquisition of %s - %s failed", artist, title)
        await note("failed", f"Unexpected failure: {exc}")

    return await library.get_download(download_id)

"""Checking followed channels on a schedule.

Following a channel was a bookmark until now: you had to open the page and
press a button, which is the thing following was supposed to replace. With a
queue in place this becomes what it was meant to be — the app notices a new
mix and analyses it before you ask.

Two restraints, both about not surprising anyone. A channel with a back
catalogue is not a reason to start forty analyses, so the first check of a
watch only records what is there rather than analysing it; new uploads after
that are what get picked up. And each round enqueues at most a handful.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# Per round, across every watch. An hour of analysis each, so a burst of them
# would block everything else in the queue for most of a day.
MAX_PER_ROUND = int(os.environ.get("WATCH_MAX_PER_ROUND", "3"))

# How many entries to look at per channel. Enough to catch a busy week.
LOOKBACK = int(os.environ.get("WATCH_LOOKBACK", "20"))


async def check_watches(library, enqueue, *, max_per_round: int = MAX_PER_ROUND
                        ) -> Dict[str, Any]:
    """Look for new uploads on every followed channel and queue them.

    `enqueue` is passed in rather than imported so this can be tested without
    a queue, and so the caller decides what "analyse it" means.
    """
    from src.sources import download as dl

    # Every account's, deliberately: this runs on a schedule with no
    # session behind it. Passing None is how that is said out loud.
    watches = await library.list_watches(user_id=None)
    if not watches:
        return {"watches": 0, "found": 0, "queued": 0}

    logger.info("Checking %d watch(es) for new uploads", len(watches))
    found_total = 0
    queued: List[Dict[str, str]] = []

    for watch in watches:
        try:
            entries = await dl.list_channel(watch["url"], limit=LOOKBACK)
        except Exception as exc:
            # One dead channel must not stop the others. A URL can rot, go
            # private, or simply be down for the afternoon.
            logger.warning("Could not check %s: %s", watch["title"], exc)
            continue

        seen = set(await library.watch_seen_ids(watch["id"]))
        fresh = [e for e in entries if e["id"] and e["id"] not in seen]

        # Record everything before deciding what to analyse, so a failure
        # further down does not make the same entries "new" again next round.
        await library.mark_watch_checked(
            watch["id"], list(seen | {e["id"] for e in entries if e["id"]}))

        if not seen:
            # First look at this channel: its back catalogue is not news.
            logger.info("Recorded %d existing upload(s) on %s without "
                        "analysing them", len(entries), watch["title"])
            continue

        found_total += len(fresh)
        for entry in fresh:
            if len(queued) >= max_per_round:
                logger.info("Reached the per-round cap; the rest will be "
                            "picked up next time")
                break
            if await enqueue(entry["url"]):
                queued.append({"watch": watch["title"], "title": entry["title"]})

    for item in queued:
        logger.info("Queued %r from %s", item["title"][:60], item["watch"])

    return {"watches": len(watches), "found": found_total, "queued": len(queued)}

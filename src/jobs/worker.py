"""arq worker: the process that actually analyses sets.

Run with `arq src.jobs.worker.WorkerSettings`. It shares the application image
and the state volume with the API, so the two agree on where a task's progress
lives without talking to each other.

The job functions are thin: they import the same `run_analysis` /
`run_url_analysis` the API used to call directly, so there is one
implementation of an analysis and the queue only decides *where* it runs.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from arq import cron

from .queue import JOB_TIMEOUT, MAX_TRIES, QUEUE_NAME, _redis_settings

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s")
logger = logging.getLogger(__name__)


async def analyze_upload_job(ctx: Dict[str, Any], task_id: str, path: str,
                             title: str) -> None:
    from src import web

    logger.info("Analysing upload %s (%s)", task_id, title)
    await web.run_analysis(task_id, web.Path(path), title)


async def analyze_url_job(ctx: Dict[str, Any], task_id: str, url: str,
                          set_id: Optional[str] = None) -> None:
    from src import web

    logger.info("Analysing %s for task %s", url, task_id)
    await web.run_url_analysis(task_id, url, set_id=set_id)


async def enrich_set_job(ctx: Dict[str, Any], set_id: str) -> Dict[str, int]:
    """Fill in labels and catalogue numbers for a set that has been analysed.

    Its own job rather than a stage of the analysis: lookups are paced at about
    one per second, so this adds minutes a listener should not wait through,
    and a provider being down should not fail an analysis that already
    succeeded.
    """
    from src import web
    from src.enrich.musicbrainz import MusicBrainzEnricher
    from src.enrich.runner import enrich_set

    report = await enrich_set(web.library, MusicBrainzEnricher(), set_id)
    return report.as_dict()


async def acquire_track_job(ctx: Dict[str, Any], download_id: int,
                            track_key: str, artist: str, title: str,
                            meta: Optional[Dict[str, Any]] = None,
                            chosen: Optional[Dict[str, Any]] = None) -> None:
    """Fetch one track from Soulseek.

    A job because a Soulseek transfer can take an hour: a peer may queue you
    behind forty other people, and holding an HTTP request open for that is
    not a design.
    """
    from src import web
    from src.acquire.runner import acquire_track
    from src.identify.shazam import ShazamIdentifier

    await acquire_track(
        web.library, web.DOWNLOAD_DIR, track_key, artist, title,
        download_id=download_id, meta=meta, chosen=chosen,
        # The same fingerprinter that found the track checks what arrived.
        identifier=ShazamIdentifier(concurrency=1),
    )


async def check_watches_job(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """Look for new uploads on followed channels, on a schedule.

    Following was a bookmark before this: you had to open the page and press a
    button, which is the thing following was supposed to save you.
    """
    import uuid as _uuid

    from src import web
    from src.jobs.watches import check_watches

    async def enqueue(url: str) -> bool:
        task_id = str(_uuid.uuid4())
        task = web.tasks.create(task_id, filename="From a followed channel",
                                source_url=url)
        await web.dispatch(task, "analyze_url_job", task_id, url, None)
        return True

    return await check_watches(web.library, enqueue)


async def startup(ctx: Dict[str, Any]) -> None:
    """Reclaim whatever the previous worker was doing when it died.

    arq will retry an interrupted job on its own, but not soon: it holds an
    "in progress" lock for the length of `job_timeout`, which for a
    multi-hour analysis means the work sits untouched for hours. That is worse
    than useless — the whole point of the queue is that a deploy stops costing
    an analysis.

    So the lock is released here and the job re-queued immediately.

    This assumes **one worker replica**, which the stack enforces: a worker
    starting up means nothing else is running, so no live job can be stolen.
    Raising the replica count without revisiting this would let a starting
    worker snatch a job another one is part-way through.
    """
    from src import web

    interrupted = web.tasks.mark_interrupted(requeued=True)
    if interrupted:
        logger.info("Marked %d task(s) interrupted by the previous worker",
                    interrupted)

    stale = web.tasks.interrupted_jobs()
    if not stale:
        return

    redis = ctx["redis"]
    for snapshot in stale:
        task_id = snapshot["task_id"]
        spec = snapshot["job"]
        try:
            await redis.delete(f"arq:in-progress:{task_id}")
            await redis.enqueue_job(spec["function"], *spec["args"],
                                    _job_id=task_id, _queue_name=QUEUE_NAME)
            logger.info("Reclaimed interrupted analysis %s", task_id)
        except Exception as exc:
            logger.error("Could not reclaim %s: %s", task_id, exc)


class WorkerSettings:
    functions = [analyze_upload_job, analyze_url_job, enrich_set_job,
                 acquire_track_job, check_watches_job]
    # Every six hours, offset off the hour so it does not land with everything
    # else on the machine. `unique` means one worker runs it even if there were
    # ever several.
    cron_jobs = [
        cron(check_watches_job, hour={2, 8, 14, 20}, minute=17, unique=True),
    ]
    on_startup = startup
    queue_name = QUEUE_NAME
    job_timeout = JOB_TIMEOUT
    max_tries = MAX_TRIES
    # One analysis at a time per worker. Each already saturates several cores
    # through ffmpeg and the feature thread; running two would slow both and
    # put the container back in the territory where its health probe starves.
    max_jobs = 1
    # Keep finished jobs briefly so a result can be inspected after the fact.
    keep_result = 3600
    # Without this arq ignores every abort request, and it defaults to False.
    # The UI has always shown a Cancel button on a running analysis; for any
    # analysis actually running in the worker — which is all of them — it
    # returned "Task is not running" and the work carried on. An hour of
    # probing you have decided you do not want is expensive in the one budget
    # that matters here, which is how often Shazam will still talk to us.
    allow_abort_jobs = True

    # An attribute, not a method: arq reads this off the class and expects a
    # RedisSettings instance. A staticmethod here fails at connection time with
    # an AttributeError about `.host`, which points nowhere useful.
    redis_settings = _redis_settings()

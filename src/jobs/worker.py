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
    functions = [analyze_upload_job, analyze_url_job]
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

    # An attribute, not a method: arq reads this off the class and expects a
    # RedisSettings instance. A staticmethod here fails at connection time with
    # an AttributeError about `.host`, which points nowhere useful.
    redis_settings = _redis_settings()

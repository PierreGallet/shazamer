"""Job queue: run analyses outside the web process.

Analyses used to run as `asyncio.create_task` inside the API. That was fine
until it wasn't: every deploy killed whatever was in flight, and a 69-minute
set is long enough to be near-certain to meet one. Three were lost in a single
afternoon.

Two things change here.

**Durability.** A job lives in Redis until a worker finishes it. Kill the
worker mid-analysis and the job is picked up again — by the replacement
container, or by the same one after a restart. Deploys stop being destructive.

**Isolation.** Analysis is CPU-bound by design and the API is not. Splitting
them means a long decode can no longer starve the health probe, which is how
Swarm came to kill a container that was working perfectly.

Redis is used only as the broker. Task state stays on the shared volume both
containers already mount, so there is one source of truth and no second copy
to keep in sync. When REDIS_URL is unset — local development, tests — the
queue degrades to running in-process, which behaves identically apart from the
durability it exists to provide.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")

# A three-hour set is a legitimate job. The timeout exists to catch a wedged
# worker, not to bound normal work, so it sits well above the worst real case.
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT_SECONDS", "14400"))

# One attempt beyond the first. A job that died because its container was
# replaced deserves another go; one that died because the URL is dead does not
# deserve five.
MAX_TRIES = int(os.environ.get("JOB_MAX_TRIES", "2"))

QUEUE_NAME = "shazamer:analysis"


def enabled() -> bool:
    return bool(REDIS_URL)


def _redis_settings():
    from arq.connections import RedisSettings

    if not REDIS_URL:
        # The worker reads this at class-definition time, so raising here would
        # make the module unimportable without a queue — breaking tests and
        # tooling that only want to inspect it. A worker genuinely started
        # without a queue instead fails at connect time, which says the same
        # thing at the moment it actually matters.
        logger.warning("REDIS_URL is not set; falling back to a local default. "
                       "The API runs analyses in-process without a queue, but "
                       "a worker has no other way to receive work.")
        return RedisSettings.from_dsn("redis://localhost:6379")
    return RedisSettings.from_dsn(REDIS_URL)


async def enqueue(function: str, *args: Any, job_id: Optional[str] = None) -> bool:
    """Hand a job to the queue. Returns False when no queue is configured.

    `job_id` is the task id, which makes enqueueing idempotent: asking twice
    for the same analysis does not run it twice.
    """
    if not enabled():
        return False

    from arq import create_pool

    try:
        pool = await create_pool(_redis_settings())
    except Exception as exc:
        # A queue that cannot be reached must not silently swallow the request;
        # the caller falls back to running in-process.
        logger.error("Cannot reach the job queue: %s", exc)
        return False

    try:
        job = await pool.enqueue_job(function, *args, _job_id=job_id,
                                     _queue_name=QUEUE_NAME)
        if job is None:
            logger.info("Job %s is already queued or running", job_id)
        return True
    finally:
        await pool.aclose()


async def abort(job_id: str) -> bool:
    """Ask the queue to stop a job. False when there is no queue."""
    if not enabled():
        return False

    from arq import create_pool
    from arq.jobs import Job

    try:
        pool = await create_pool(_redis_settings())
    except Exception as exc:
        logger.error("Cannot reach the job queue to abort %s: %s", job_id, exc)
        return False
    try:
        return await Job(job_id, pool, _queue_name=QUEUE_NAME).abort(timeout=5)
    except Exception as exc:
        logger.warning("Could not abort %s: %s", job_id, exc)
        return False
    finally:
        await pool.aclose()

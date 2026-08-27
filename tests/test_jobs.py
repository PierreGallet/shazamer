"""The job queue: dispatch, fallback, and surviving an interruption.

None of this needs a live Redis. What is worth testing is the decision-making
around the queue — when work is handed off, what happens when the queue is
absent or unreachable, and whether an interrupted analysis carries enough
information to be picked up again. Whether arq can talk to Redis is arq's
business, and was verified by hand against a real instance.
"""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src.jobs import queue as jobs
from src.tasks import TaskManager

pytestmark = pytest.mark.anyio


def test_the_queue_is_off_unless_configured(monkeypatch):
    monkeypatch.setattr(jobs, "REDIS_URL", "")
    assert jobs.enabled() is False


async def test_enqueue_declines_without_a_queue(monkeypatch):
    monkeypatch.setattr(jobs, "REDIS_URL", "")
    assert await jobs.enqueue("analyze_url_job", "t1", "http://x") is False


async def test_a_task_records_how_to_restart_itself(tmp_path):
    """An interrupted job is only reclaimable if something remembers it."""
    manager = TaskManager(tmp_path)
    task = manager.create("t1", filename="Set")
    task.job = {"function": "analyze_url_job", "args": ["t1", "http://x", None]}
    manager.update(task, status="processing", progress=40)

    reloaded = TaskManager(tmp_path)
    stale = reloaded.interrupted_jobs()
    assert len(stale) == 1
    assert stale[0]["job"]["function"] == "analyze_url_job"
    assert stale[0]["job"]["args"][1] == "http://x"


async def test_tasks_without_a_job_spec_are_not_reclaimed(tmp_path):
    """Work the API ran in-process cannot be handed to a queue."""
    manager = TaskManager(tmp_path)
    task = manager.create("t1", filename="Set")
    manager.update(task, status="processing", progress=40)

    assert TaskManager(tmp_path).interrupted_jobs() == []


async def test_interruption_reads_as_a_pause_when_it_will_be_retried(tmp_path):
    """With a queue the work is not lost, so calling it an error is a lie.

    It also has a consequence: the frontend stops watching a task it believes
    has failed, so a run about to resume would look dead.
    """
    manager = TaskManager(tmp_path)
    task = manager.create("t1", filename="Set")
    manager.update(task, status="processing", progress=40)

    assert TaskManager(tmp_path).mark_interrupted(requeued=True) == 1
    resumed = TaskManager(tmp_path).get("t1")
    assert resumed.status == "pending"
    assert resumed.error is None
    assert "picking up again" in resumed.message


async def test_interruption_reads_as_an_error_without_a_queue(tmp_path):
    manager = TaskManager(tmp_path)
    task = manager.create("t1", filename="Set")
    manager.update(task, status="processing", progress=40)

    assert TaskManager(tmp_path).mark_interrupted(requeued=False) == 1
    stopped = TaskManager(tmp_path).get("t1")
    assert stopped.status == "error"
    assert "restarted" in stopped.error


async def test_progress_from_another_process_is_visible(tmp_path):
    """The worker writes to the shared directory; the API must read it.

    Preferring the in-memory copy made every queued analysis look frozen at
    "pending 0%", because memory held what was true at creation and nothing
    since.
    """
    api = TaskManager(tmp_path)
    api.create("t1", filename="Set")

    worker = TaskManager(tmp_path)
    worker_task = worker.get("t1")
    worker.update(worker_task, status="processing", progress=63,
                  message="Identifying... 40/95 probes")

    seen = api.get("t1")
    assert seen.progress == 63
    assert "40/95" in seen.message


async def test_watch_yields_changes_and_stops_when_finished(tmp_path):
    import asyncio

    manager = TaskManager(tmp_path)
    task = manager.create("t1", filename="Set")

    async def advance():
        writer = TaskManager(tmp_path)
        for pct in (25, 60):
            await asyncio.sleep(0.05)
            writer.update(writer.get("t1"), status="processing", progress=pct)
        await asyncio.sleep(0.05)
        writer.finish(writer.get("t1"), status="completed", message="Done",
                      set_id="s1")

    mover = asyncio.create_task(advance())
    seen = [s["progress"] async for s in manager.watch("t1", poll=0.02)]
    await mover

    assert seen[-1] == 100, "watch did not see the finish"
    assert 25 in seen and 60 in seen, f"missed intermediate progress: {seen}"


async def test_watch_gives_up_on_a_task_that_never_appears(tmp_path):
    manager = TaskManager(tmp_path)
    seen = [s async for s in manager.watch("never-written", poll=0.01)]
    assert seen == []


async def test_dispatch_falls_back_when_the_queue_is_unreachable(client,
                                                                 monkeypatch):
    """A Redis outage must degrade the app, not break it."""
    monkeypatch.setattr(client.web.jobs, "enabled", lambda: True)
    monkeypatch.setattr(client.web.jobs, "enqueue",
                        AsyncMock(return_value=False))

    ran = {}

    async def fake_analysis(task_id, path, title, **kwargs):
        ran["task_id"] = task_id

    monkeypatch.setattr(client.web, "run_analysis", fake_analysis)

    task = client.web.tasks.create("t1", filename="Set")
    await client.web.dispatch(task, "analyze_upload_job", "t1",
                              str(Path("x.mp3")), "Set")
    assert task._handle is not None, "no in-process fallback was started"
    await task._handle
    assert ran["task_id"] == "t1"


async def test_dispatch_prefers_the_queue_when_it_answers(client, monkeypatch):
    monkeypatch.setattr(client.web.jobs, "enabled", lambda: True)
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(client.web.jobs, "enqueue", enqueue)

    task = client.web.tasks.create("t1", filename="Set")
    await client.web.dispatch(task, "analyze_url_job", "t1", "http://x", None)

    assert enqueue.await_count == 1
    assert task._handle is None, "work was also started in-process"
    assert task.job["function"] == "analyze_url_job"

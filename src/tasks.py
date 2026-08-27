"""In-flight analysis tasks: state, progress fan-out, and persistence.

Replaces the old module-level dict, which grew without bound and lost every
in-flight task on restart. Three changes matter:

- **Bounded.** Completed tasks are evicted once the cap is reached; the library
  is the durable record, a task is just the progress of one run.
- **Pushed, not polled.** Each subscriber gets a queue, so the API can stream
  Server-Sent Events instead of the frontend asking "are we there yet" once a
  second.
- **Cancellable.** A task holds its asyncio task so a user can stop a long
  analysis, and so shutdown does not strand subprocesses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger(__name__)

NON_TERMINAL = {"pending", "downloading", "processing"}
_VOLATILE = {"_task", "_queues", "filepath"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Task:
    def __init__(self, task_id: str, filename: str = "", source_url: str = "") -> None:
        self.id = task_id
        self.status = "pending"
        self.progress = 0
        self.stage = "pending"
        self.message = "Queued..."
        self.filename = filename
        self.source_url = source_url
        self.error: Optional[str] = None
        self.set_id: Optional[str] = None
        self.result: Optional[Dict[str, Any]] = None
        self.created_at = _now()
        self.finished_at: Optional[str] = None
        self.filepath: Optional[str] = None
        self.quality: str = ""
        # How to run this task again: the queue function and its arguments.
        # Stored with the task so an interrupted job can be reclaimed without
        # the worker having to reconstruct what it was doing.
        self.job: Optional[Dict[str, Any]] = None
        self._queues: List[asyncio.Queue] = []
        self._handle: Optional[asyncio.Task] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "task_id": self.id, "status": self.status, "progress": self.progress,
            "stage": self.stage, "message": self.message, "filename": self.filename,
            "source_url": self.source_url, "error": self.error, "set_id": self.set_id,
            "quality": self.quality, "created_at": self.created_at,
            "finished_at": self.finished_at, "job": self.job,
        }

    @property
    def terminal(self) -> bool:
        return self.status in ("completed", "error", "cancelled")


class TaskManager:
    def __init__(self, directory: Path, max_tasks: int = 200) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_tasks = max_tasks
        self._tasks: "OrderedDict[str, Task]" = OrderedDict()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def create(self, task_id: str, filename: str = "", source_url: str = "") -> Task:
        task = Task(task_id, filename=filename, source_url=source_url)
        self._tasks[task_id] = task
        self._evict()
        self.persist(task)
        return task

    def active(self) -> List[Task]:
        """Tasks still running, newest first.

        The header needs this to show a way back to an analysis you navigated
        away from. It reads from memory rather than disk on purpose: only this
        process can actually still be running them, and a task left on disk by
        a previous process is by definition no longer in flight.
        """
        return sorted(
            (t for t in self._tasks.values() if not t.terminal),
            key=lambda t: t.created_at,
            reverse=True,
        )

    def get(self, task_id: str) -> Optional[Task]:
        """Return a task, preferring whichever copy is actually authoritative.

        In-memory is right only for work this process is running. Everything
        else is executed by the worker container, which writes its progress to
        the shared file — so for those, memory holds whatever was true at
        creation and nothing since. Returning it made every queued analysis
        look stuck at "pending 0%".
        """
        task = self._tasks.get(task_id)
        if task is not None and task._handle is not None:
            return task                 # we are running it; memory is the truth

        stored = self._load(task_id)
        if stored is None:
            return task                 # never persisted yet, memory is all we have
        if task is not None:
            # Keep the same object so subscribers and the handle survive.
            task.__dict__.update(
                {k: v for k, v in stored.__dict__.items()
                 if not k.startswith("_")}
            )
            return task
        self._tasks[task_id] = stored
        return stored

    def _evict(self) -> None:
        """Drop the oldest terminal tasks once over the cap."""
        while len(self._tasks) > self.max_tasks:
            for tid, task in list(self._tasks.items()):
                if task.terminal:
                    del self._tasks[tid]
                    break
            else:
                # Everything still running — refuse to evict live work.
                return

    def attach(self, task: Task, handle: asyncio.Task) -> None:
        task._handle = handle

    async def cancel(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or task.terminal or task._handle is None:
            return False
        task._handle.cancel()
        return True

    # ── Progress fan-out ─────────────────────────────────────────────────

    def subscribe(self, task: Task) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        task._queues.append(queue)
        return queue

    def unsubscribe(self, task: Task, queue: asyncio.Queue) -> None:
        try:
            task._queues.remove(queue)
        except ValueError:
            pass

    def update(self, task: Task, *, stage: Optional[str] = None,
               progress: Optional[int] = None, message: Optional[str] = None,
               status: Optional[str] = None, **fields) -> None:
        if stage is not None:
            task.stage = stage
        if progress is not None:
            task.progress = max(0, min(100, int(progress)))
        if message is not None:
            task.message = message
        if status is not None:
            task.status = status
        for key, value in fields.items():
            setattr(task, key, value)

        snapshot = task.snapshot()

        # Persisted on every update, not only at phase transitions. The worker
        # and the API run in separate containers sharing this directory, so the
        # file *is* the channel between them — an update kept in memory is
        # invisible to whoever is streaming it. Each write is a few hundred
        # bytes replaced atomically, a couple of hundred times per analysis.
        self.persist(task)

        for queue in list(task._queues):
            try:
                queue.put_nowait(snapshot)
            except asyncio.QueueFull:
                # A stalled reader must not stall the analysis. It will pick up
                # the current state from its next successful message.
                pass

    def finish(self, task: Task, *, status: str, message: str,
               error: Optional[str] = None, set_id: Optional[str] = None) -> None:
        task.finished_at = _now()
        self.update(task, status=status, message=message,
                    progress=100 if status == "completed" else task.progress,
                    stage=status, error=error, set_id=set_id)
        self.persist(task)
        for queue in list(task._queues):
            try:
                queue.put_nowait(None)  # sentinel: close the stream
            except asyncio.QueueFull:
                pass

    # ── Persistence ──────────────────────────────────────────────────────

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def persist(self, task: Task) -> None:
        path = self._path(task.id)
        tmp = path.with_suffix(".json.tmp")
        try:
            with open(tmp, "w") as fh:
                json.dump(task.snapshot(), fh, default=str)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Could not persist task %s: %s", task.id, exc)

    def _load(self, task_id: str) -> Optional[Task]:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        task = Task(task_id, filename=data.get("filename", ""),
                    source_url=data.get("source_url", ""))
        task.status = data.get("status", "unknown")
        task.progress = data.get("progress", 0)
        task.stage = data.get("stage", "")
        task.message = data.get("message", "")
        task.error = data.get("error")
        task.set_id = data.get("set_id")
        task.created_at = data.get("created_at", _now())
        task.finished_at = data.get("finished_at")
        task.job = data.get("job")
        return task

    async def watch(self, task_id: str, poll: float = 0.5
                    ) -> "AsyncIterator[Dict[str, Any]]":
        """Yield a task's state each time it changes on disk.

        Used when the task belongs to another process — the worker container —
        where there is no in-memory queue to subscribe to. The file is the
        shared channel, so this watches the file.

        Server-side polling of a small local file is cheap and the client still
        gets a pushed stream; the alternative, a second Redis connection per
        open tab purely to relay progress, buys nothing here.

        Ends once the task reaches a terminal state.
        """
        last: Optional[str] = None
        missing_for = 0.0
        while True:
            snapshot = self._read(task_id)
            if snapshot is None:
                # A task enqueued a moment ago may not have been written yet.
                missing_for += poll
                if missing_for > 10:
                    return
                await asyncio.sleep(poll)
                continue
            missing_for = 0.0

            encoded = json.dumps(snapshot, sort_keys=True)
            if encoded != last:
                last = encoded
                yield snapshot
            if snapshot.get("status") in ("completed", "error", "cancelled"):
                return
            await asyncio.sleep(poll)

    def interrupted_jobs(self) -> List[Dict[str, Any]]:
        """Task snapshots that were mid-flight and know how to restart.

        Used by the worker to reclaim work after a crash. Tasks with no job
        spec were run in-process by the API and cannot be handed to a queue.
        """
        return [s for s in self.active_on_disk() if s.get("job")]

    def active_on_disk(self) -> List[Dict[str, Any]]:
        """Non-terminal tasks according to the shared directory.

        The in-memory view only knows about work this process started, which
        since the queue exists is usually none of it.
        """
        out: List[Dict[str, Any]] = []
        for path in self.dir.glob("*.json"):
            try:
                with open(path) as fh:
                    snapshot = json.load(fh)
            except (OSError, json.JSONDecodeError):
                continue
            if snapshot.get("status") in NON_TERMINAL:
                out.append(snapshot)
        out.sort(key=lambda s: s.get("created_at", ""), reverse=True)
        return out

    def _read(self, task_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(task_id)
        try:
            with open(path) as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None

    def mark_interrupted(self, requeued: bool = False) -> int:
        """Flag tasks left mid-flight by a restart.

        Without this the frontend sees a task that never advances and reports
        "connection lost", pointing the user at their network instead of at the
        redeploy or OOM that actually happened.

        `requeued` changes what the interruption *means*. With a job queue the
        work is not lost — the queue hands it back and it starts again — so
        calling it an error would be false, and the UI would stop watching a
        run that is about to resume. Without a queue the work really is gone
        and the user has to start it themselves.
        """
        count = 0
        for path in self.dir.glob("*.json"):
            try:
                with open(path) as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("status") in NON_TERMINAL:
                if requeued:
                    data.update({
                        "status": "pending", "stage": "pending", "progress": 0,
                        "message": "Interrupted — picking up again...",
                        "error": None,
                    })
                else:
                    data.update({
                        "status": "error", "stage": "error", "progress": 0,
                        "message": "Analysis interrupted",
                        "error": "The server restarted while this set was being "
                                 "analysed. Start it again — nothing was lost.",
                    })
                try:
                    with open(path, "w") as fh:
                        json.dump(data, fh, default=str)
                    count += 1
                except OSError:
                    pass
        return count

    def sweep(self, max_age_days: int = 30) -> int:
        cutoff = datetime.now(timezone.utc).timestamp() - max_age_days * 86400
        removed = 0
        for path in self.dir.glob("*.json"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed

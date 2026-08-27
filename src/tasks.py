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
from typing import Any, Dict, List, Optional

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
        self._queues: List[asyncio.Queue] = []
        self._handle: Optional[asyncio.Task] = None

    def snapshot(self) -> Dict[str, Any]:
        return {
            "task_id": self.id, "status": self.status, "progress": self.progress,
            "stage": self.stage, "message": self.message, "filename": self.filename,
            "source_url": self.source_url, "error": self.error, "set_id": self.set_id,
            "quality": self.quality, "created_at": self.created_at,
            "finished_at": self.finished_at,
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

    def get(self, task_id: str) -> Optional[Task]:
        task = self._tasks.get(task_id)
        if task is not None:
            return task
        return self._load(task_id)

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
        return task

    def mark_interrupted(self) -> int:
        """Flag tasks left mid-flight by a restart.

        Without this the frontend sees a task that never advances and reports
        "connection lost", which points the user at their network instead of
        at the redeploy or OOM that actually happened.
        """
        count = 0
        for path in self.dir.glob("*.json"):
            try:
                with open(path) as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                continue
            if data.get("status") in NON_TERMINAL:
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

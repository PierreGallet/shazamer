"""Persistent task store backed by per-task JSON files.

Survives uvicorn restarts so the frontend sees a clean 'interrupted' error
instead of 'Connection lost' when the process is killed mid-analysis (OOM,
redeploy, etc.).
"""
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

NON_TERMINAL_STATUSES = {"pending", "downloading", "processing"}
_VOLATILE_KEYS = {"filepath", "_analyzer"}


class TaskStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, task_id: str) -> Path:
        return self.dir / f"{task_id}.json"

    def save(self, task_id: str, task: dict) -> None:
        path = self._path(task_id)
        tmp = path.with_suffix(".json.tmp")
        serializable = {k: v for k, v in task.items() if k not in _VOLATILE_KEYS}
        try:
            with open(tmp, "w") as f:
                json.dump(serializable, f, default=str)
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("Failed to persist task %s: %s", task_id, exc)

    def load(self, task_id: str) -> Optional[dict]:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def mark_interrupted(self) -> int:
        """Mark any persisted task in a non-terminal state as interrupted.

        Called at server startup. Returns the number of tasks marked.
        """
        count = 0
        for path in self.dir.glob("*.json"):
            try:
                with open(path) as f:
                    task = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            if task.get("status") in NON_TERMINAL_STATUSES:
                task["status"] = "error"
                task["progress"] = 0
                task["message"] = "Analysis interrupted"
                task["error"] = (
                    "The analysis was interrupted because the server "
                    "restarted (likely an out-of-memory on a very long "
                    "audio). Please retry with a shorter file."
                )
                try:
                    with open(path, "w") as f:
                        json.dump(task, f, default=str)
                    count += 1
                except OSError:
                    pass
        return count

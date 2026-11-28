"""Task records and where they live.

A record is a JSON object::

    {"task_id", "tenant", "tool", "status", "status_message"?,
     "created_at", "updated_at", "expires_at",      # epoch seconds
     "result"? | "error"?, "cancel_requested", "runner", "heartbeat_at"}

``status`` is one of the extension's: working, input_required, completed,
failed, cancelled. ``result`` (a CallToolResult) is user content: stores keep
it out of logs, and files are written owner-only.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Protocol, runtime_checkable

TERMINAL = frozenset({"completed", "failed", "cancelled"})
TASK_ID_RE = re.compile(r"[0-9a-f]{32}")
TASKS_DIRNAME = "_tasks"


@runtime_checkable
class TaskStore(Protocol):
    def create(self, record: dict) -> None:
        """Store a new record; durable when this returns."""
        ...

    def get(self, task_id: str) -> dict | None:
        ...

    def update(self, task_id: str, fields: dict) -> dict | None:
        """Merge *fields* into the record; ``None`` when there is none. A
        terminal record never changes status again."""
        ...

    def delete(self, task_id: str) -> None:
        ...

    def purge(self, now: float) -> int:
        """Delete every record expired at *now*; how many went."""
        ...


def merge(record: dict, fields: dict) -> dict:
    """*record* with *fields*, except that a finished task stays finished."""
    if record["status"] in TERMINAL and "status" in fields:
        fields = {k: v for k, v in fields.items()
                  if k not in ("status", "status_message", "result", "error")}
    return {**record, **fields}


class LocalTaskStore:
    """One JSON file per task under ``{feedback_dir}/_tasks/``, written
    atomically and readable by the server's user only."""

    def __init__(self, feedback_dir: str | os.PathLike[str]):
        self.root = Path(feedback_dir) / TASKS_DIRNAME
        self._lock = threading.Lock()

    def _path(self, task_id: str) -> Path | None:
        return self.root / f"{task_id}.json" if TASK_ID_RE.fullmatch(task_id or "") else None

    def _write(self, path: Path, record: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _read(self, path: Path) -> dict | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def create(self, record: dict) -> None:
        with self._lock:
            self._write(self._path(record["task_id"]), record)

    def get(self, task_id: str) -> dict | None:
        path = self._path(task_id)
        return self._read(path) if path is not None else None

    def update(self, task_id: str, fields: dict) -> dict | None:
        path = self._path(task_id)
        if path is None:
            return None
        with self._lock:
            record = self._read(path)
            if record is None:
                return None
            record = merge(record, fields)
            self._write(path, record)
            return record

    def delete(self, task_id: str) -> None:
        path = self._path(task_id)
        if path is not None:
            with self._lock:
                path.unlink(missing_ok=True)

    def purge(self, now: float) -> int:
        if not self.root.is_dir():
            return 0
        gone = 0
        with self._lock:
            for path in self.root.glob("*.json"):
                record = self._read(path)
                if record is None or record.get("expires_at", 0) <= now:
                    path.unlink(missing_ok=True)
                    gone += 1
        return gone


def new_record(task_id: str, tenant, tool: str, ttl_s: int, runner: str,
               now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {"task_id": task_id, "tenant": tenant, "tool": tool, "status": "working",
            "created_at": now, "updated_at": now, "expires_at": now + ttl_s,
            "cancel_requested": False, "runner": runner, "heartbeat_at": now}

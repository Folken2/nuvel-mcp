"""In-process task workers.

A task runs on a bounded thread pool, in a copy of the caller's context (so
the tool sees the same principal). While it runs, a heartbeat refreshes
``heartbeat_at`` and names this process as its ``runner``; a task whose runner
stopped beating (the process restarted or died) is reported as failed by
:meth:`TaskRunner.settle`, never left working.

Cancellation is cooperative: a tool that may run long checks
:func:`cancelled` and raises :class:`TaskCancelled`; a task cancelled before
it started never runs.
"""

from __future__ import annotations

import contextvars
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from nuvel_mcp.protocol.errors import INTERNAL_ERROR, McpError
from nuvel_mcp.tasks.store import TERMINAL, TaskStore

WORKERS_ENV = "NUVEL_MCP_TASK_WORKERS"
TTL_ENV = "NUVEL_MCP_TASK_TTL"
DEFAULT_WORKERS = 4
DEFAULT_TTL_S = 24 * 3600
HEARTBEAT_S = 10.0
INTERRUPTED = {"code": INTERNAL_ERROR, "message": "The task was interrupted: its server stopped"}

_cancel: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "nuvel_mcp_task_cancel", default=None)


class TaskCancelled(Exception):
    """Raised by a tool that saw its task's cancellation."""


def cancelled() -> bool:
    """True inside a task whose cancellation was requested."""
    event = _cancel.get()
    return event is not None and event.is_set()


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def _env_int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not low <= int(raw) <= high:
        raise ValueError(f"{name} must be a whole number from {low} to {high}")
    return int(raw)


def settings_from_env() -> tuple[int, int]:
    """(workers, ttl seconds) from the environment; raises ``ValueError``."""
    return (_env_int(WORKERS_ENV, DEFAULT_WORKERS, 1, 64),
            _env_int(TTL_ENV, DEFAULT_TTL_S, 60, 7 * 24 * 3600))


class TaskRunner:
    def __init__(self, store_getter: Callable[[], TaskStore], workers: int = DEFAULT_WORKERS,
                 heartbeat_s: float = HEARTBEAT_S):
        self.store = store_getter
        self.run_id = uuid.uuid4().hex  # this process, as a task's runner
        self.heartbeat_s = heartbeat_s
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="mcp-task")
        self._live: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._beat: threading.Thread | None = None

    def is_live(self, task_id: str) -> bool:
        with self._lock:
            return task_id in self._live

    def submit(self, task_id: str, work: Callable[[], dict],
               on_done: Callable[[str], None] | None = None) -> None:
        """Run *work* (returns a CallToolResult, raises McpError) as the task."""
        event = threading.Event()
        with self._lock:
            self._live[task_id] = event
            if self._beat is None:
                self._beat = threading.Thread(target=self._heartbeat, name="mcp-task-heartbeat",
                                              daemon=True)
                self._beat.start()
        context = contextvars.copy_context()
        self._pool.submit(context.run, self._run, task_id, event, work, on_done)

    def _run(self, task_id, event, work, on_done) -> None:
        store = self.store()
        token = _cancel.set(event)
        try:
            if event.is_set() or (store.get(task_id) or {}).get("cancel_requested"):
                fields = {"status": "cancelled"}
            else:
                try:
                    fields = {"status": "completed", "result": work()}
                except TaskCancelled:
                    fields = {"status": "cancelled"}
                except McpError as exc:
                    error = {"code": exc.code, "message": exc.message}
                    if exc.data is not None:
                        error["data"] = exc.data
                    fields = {"status": "failed", "error": error}
                except Exception as exc:  # noqa: BLE001 - a task never kills its worker
                    _log(f"task: {type(exc).__name__} in a task")  # never the result or args
                    fields = {"status": "failed",
                              "error": {"code": INTERNAL_ERROR, "message": "Internal error"}}
            fields["updated_at"] = time.time()
            store.update(task_id, fields)
        finally:
            _cancel.reset(token)
            with self._lock:
                self._live.pop(task_id, None)
            if on_done is not None:
                on_done(task_id)

    def cancel(self, task_id: str) -> None:
        with self._lock:
            event = self._live.get(task_id)
        if event is not None:
            event.set()

    def _heartbeat(self) -> None:
        while True:
            time.sleep(self.heartbeat_s)
            with self._lock:
                live = list(self._live)
            now = time.time()
            for task_id in live:
                try:
                    self.store().update(task_id, {"heartbeat_at": now})
                except Exception as exc:  # noqa: BLE001
                    _log(f"task: heartbeat not stored: {type(exc).__name__}")

    def settle(self, record: dict, now: float | None = None) -> dict:
        """*record*, or failed when its runner is gone: a task working in no live
        process (this one not running it, or another one silent for three
        heartbeats) can't finish."""
        if record["status"] in TERMINAL:
            return record
        now = time.time() if now is None else now
        if record.get("runner") == self.run_id:
            orphaned = not self.is_live(record["task_id"])
        else:
            orphaned = now - record.get("heartbeat_at", 0) > 3 * self.heartbeat_s
        if not orphaned:
            return record
        return self.store().update(record["task_id"], {
            "status": "failed", "error": INTERRUPTED, "updated_at": now}) or record

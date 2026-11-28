"""The Tasks extension (``io.modelcontextprotocol/tasks``, SEP-2663).

A ``tools/call`` of a task tool, from a client that declared the extension on
that request, is answered with ``resultType: "task"`` and a task id instead of
the tool's result; the tool runs on :class:`~nuvel_mcp.tasks.runner.TaskRunner`
and the client polls ``tasks/get`` (or listens for ``notifications/tasks``).
A client that didn't declare it gets the ordinary result: no tool here needs
to be a task.

Every task method needs the extension declared (``-32021``), and a task is
visible only to the tenant that created it: an unknown id, an expired one and
another tenant's all get the same ``-32602``. Modern (2026-07-28) requests
only; the 2025-11-25 tasks were a different, experimental design.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from nuvel_mcp.auth import current_principal
from nuvel_mcp.protocol import envelope
from nuvel_mcp.protocol.errors import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    McpError,
)
from nuvel_mcp.tasks.runner import TaskRunner
from nuvel_mcp.tasks.store import new_record

IDENTIFIER = "io.modelcontextprotocol/tasks"
POLL_INTERVAL_MS = 1000
PURGE_EVERY_S = 60.0
_NOT_FOUND = "Task not found"


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def required() -> McpError:
    return McpError(MISSING_REQUIRED_CLIENT_CAPABILITY, "Missing required client capability",
                    {"requiredCapabilities": {"extensions": {IDENTIFIER: {}}}})


def declared(context: envelope.RequestContext | None) -> bool:
    return context is not None and context.has_extension(IDENTIFIER)


def detailed(record: dict) -> dict:
    """A record as the wire's DetailedTask."""
    task = {
        "taskId": record["task_id"],
        "status": record["status"],
        "createdAt": _iso(record["created_at"]),
        "lastUpdatedAt": _iso(record["updated_at"]),
        "ttlMs": int(round((record["expires_at"] - record["created_at"]) * 1000)),
        "pollIntervalMs": POLL_INTERVAL_MS,
    }
    if record.get("status_message"):
        task["statusMessage"] = record["status_message"]
    if record["status"] == "completed":
        task["result"] = record["result"]
    elif record["status"] == "failed":
        task["error"] = record["error"]
    elif record["status"] == "input_required":
        task["inputRequests"] = record.get("input_requests", {})
    return task


class TasksExtension:
    identifier = IDENTIFIER

    def __init__(self, store_getter, runner: TaskRunner, ttl_s: int):
        self.store = store_getter
        self.runner = runner
        self.ttl_s = ttl_s
        self._last_purge = 0.0

    def settings(self) -> dict:
        return {}

    def methods(self) -> dict:
        return {"tasks/get": self.handle_get, "tasks/update": self.handle_update,
                "tasks/cancel": self.handle_cancel}

    # --- creation --------------------------------------------------------------------

    def _maybe_purge(self, now: float) -> None:
        if now - self._last_purge >= PURGE_EVERY_S:
            self._last_purge = now
            self.store().purge(now)

    def create(self, tool: str, work, on_done=None) -> dict:
        """Start *work* as a task; the CreateTaskResult once the record is stored."""
        now = time.time()
        self._maybe_purge(now)
        record = new_record(uuid.uuid4().hex, current_principal.get(), tool, self.ttl_s,
                            self.runner.run_id, now)
        self.store().create(record)  # durable before the client hears of it
        self.runner.submit(record["task_id"], work, on_done)
        return {"resultType": "task", **detailed(record)}

    # --- methods ---------------------------------------------------------------------

    def _own(self, params: dict) -> dict:
        """The caller's task named in *params*, settled; else -32602."""
        context = envelope.current_request.get()
        if context is None:  # a legacy session: there are no tasks there
            raise McpError(METHOD_NOT_FOUND, "Method not found")
        if not declared(context):
            raise required()
        task_id = params.get("taskId")
        record = self.store().get(task_id) if isinstance(task_id, str) else None
        if record is None or record.get("tenant") != current_principal.get():
            raise McpError(INVALID_PARAMS, _NOT_FOUND)
        if record["expires_at"] <= time.time():
            self.store().delete(record["task_id"])
            raise McpError(INVALID_PARAMS, _NOT_FOUND)
        return self.runner.settle(record)

    def get(self, task_id: object, tenant) -> dict | None:
        """*tenant*'s task as a DetailedTask, or None (for subscriptions,
        which poll outside any request)."""
        record = self.store().get(task_id) if isinstance(task_id, str) else None
        if (record is None or record.get("tenant") != tenant
                or record["expires_at"] <= time.time()):
            return None
        return detailed(self.runner.settle(record))

    def handle_get(self, params: dict) -> dict:
        return detailed(self._own(params))

    def handle_update(self, params: dict) -> dict:
        # No task here asks for input, so no response is ever outstanding: the
        # extension says to ignore those, after checking the task is the caller's.
        self._own(params)
        if not isinstance(params.get("inputResponses"), dict):
            raise McpError(INVALID_PARAMS, "inputResponses must be an object")
        return {}

    def handle_cancel(self, params: dict) -> dict:
        record = self._own(params)
        if record["status"] not in ("completed", "failed", "cancelled"):
            self.store().update(record["task_id"], {"cancel_requested": True,
                                                    "updated_at": time.time()})
            self.runner.cancel(record["task_id"])
        return {}

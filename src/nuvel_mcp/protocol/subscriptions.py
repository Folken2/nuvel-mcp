"""``subscriptions/listen`` (2026-07-28): long-lived change notifications.

A client opens a stream with a filter of the notification types it wants. The
server answers first with ``notifications/subscriptions/acknowledged`` naming
the subset it honors, then sends only those notifications, each carrying the
subscription id (the JSON-RPC id of the listen request) in ``_meta``. A
server-side end sends the listen request's own result; a client ends a stream
by closing it (HTTP) or with ``notifications/cancelled`` (stdio).

Changes are found by polling, not pushed: :class:`Broker` asks two probes
every ``interval`` seconds (a fingerprint of the resource list, and one per
subscribed resource URI) and compares each with what that subscription last
saw, starting from the moment it opened. Each process watches what it serves
itself, so replicas need no shared feed. Transport-neutral: the transports
drain :attr:`Subscription.outbox`. Standard library only.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Hashable

from nuvel_mcp.protocol.envelope import PREFIX
from nuvel_mcp.protocol.errors import INVALID_PARAMS, McpError

METHOD = "subscriptions/listen"
SUBSCRIPTION_ID = PREFIX + "subscriptionId"
ACKNOWLEDGED = "notifications/subscriptions/acknowledged"
INTERVAL_ENV = "NUVEL_MCP_WATCH_INTERVAL"
DEFAULT_INTERVAL = 2.0
MAX_URIS = 256
# Outbox sentinel: the server is ending the subscription (send its result).
CLOSED = object()
_BOOLS = ("toolsListChanged", "promptsListChanged", "resourcesListChanged")


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def interval_from_env() -> float:
    """``NUVEL_MCP_WATCH_INTERVAL`` seconds (default 2); raises ``ValueError``."""
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    if not raw:
        return DEFAULT_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{INTERVAL_ENV} must be a number of seconds") from None
    if not 0.05 <= value <= 3600:
        raise ValueError(f"{INTERVAL_ENV} must be between 0.05 and 3600 seconds")
    return value


@dataclass(frozen=True)
class Filter:
    tools: bool = False
    prompts: bool = False
    resources_list: bool = False
    uris: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()  # the Tasks extension's addition

    def wire(self) -> dict:
        out: dict = {}
        if self.tools:
            out["toolsListChanged"] = True
        if self.prompts:
            out["promptsListChanged"] = True
        if self.resources_list:
            out["resourcesListChanged"] = True
        if self.uris:
            out["resourceSubscriptions"] = list(self.uris)
        if self.task_ids:
            out["taskIds"] = list(self.task_ids)
        return out


def read_filter(params: dict) -> Filter:
    """The requested filter; ``INVALID_PARAMS`` naming the field at fault."""
    value = params.get("notifications", {})
    if not isinstance(value, dict):
        raise McpError(INVALID_PARAMS, "notifications must be an object")
    for key in _BOOLS:
        if key in value and not isinstance(value[key], bool):
            raise McpError(INVALID_PARAMS, f"notifications.{key} must be a boolean")
    uris = value.get("resourceSubscriptions", [])
    if not isinstance(uris, list) or not all(isinstance(u, str) for u in uris):
        raise McpError(INVALID_PARAMS, "notifications.resourceSubscriptions must be a list of strings")
    task_ids = value.get("taskIds", [])
    if not isinstance(task_ids, list) or not all(isinstance(t, str) for t in task_ids):
        raise McpError(INVALID_PARAMS, "notifications.taskIds must be a list of strings")
    return Filter(task_ids=tuple(dict.fromkeys(task_ids)), tools=value.get("toolsListChanged", False),
                  prompts=value.get("promptsListChanged", False),
                  resources_list=value.get("resourcesListChanged", False),
                  uris=tuple(dict.fromkeys(uris)))


def _meta(sub_id) -> dict:
    return {SUBSCRIPTION_ID: sub_id}


def acknowledged(sub_id, honored: Filter) -> dict:
    return {"jsonrpc": "2.0", "method": ACKNOWLEDGED,
            "params": {"_meta": _meta(sub_id), "notifications": honored.wire()}}


def notification(sub_id, event: tuple) -> dict:
    """The wire form of a broker event: ``("list",)``, ``("updated", uri)`` or
    ``("task", task_id, detailed_task)``."""
    if event[0] == "task":
        return {"jsonrpc": "2.0", "method": "notifications/tasks",
                "params": {**event[2], "_meta": _meta(sub_id)}}
    if event[0] == "list":
        return {"jsonrpc": "2.0", "method": "notifications/resources/list_changed",
                "params": {"_meta": _meta(sub_id)}}
    return {"jsonrpc": "2.0", "method": "notifications/resources/updated",
            "params": {"_meta": _meta(sub_id), "uri": event[1]}}


def closing_result(sub_id, server_info: dict | None = None) -> dict:
    meta = _meta(sub_id)
    if server_info is not None:
        meta[PREFIX + "serverInfo"] = server_info
    return {"jsonrpc": "2.0", "id": sub_id, "result": {"resultType": "complete", "_meta": meta}}


@dataclass(eq=False)
class Subscription:
    id: object
    honored: Filter
    outbox: queue.Queue = field(default_factory=queue.Queue)
    seen_list: Hashable = None
    seen: dict = field(default_factory=dict)
    tenant: object = None  # whose tasks it may hear about
    seen_tasks: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Listen:
    """What the dispatcher returns for ``subscriptions/listen``: an open
    subscription and its acknowledgment, for the transport to stream."""

    subscription: Subscription
    ack: dict


def _task_mark(task: dict | None):
    """What makes a task's state new: its status and when it last changed."""
    return None if task is None else (task.get("status"), task.get("lastUpdatedAt"))


class Broker:
    """Open subscriptions and the poller that feeds them.

    ``list_probe()`` fingerprints the resource list; ``uri_probe(uri)`` one
    resource (``None`` when it is not there). A probe that raises reads as
    ``None``.
    """

    def __init__(self, list_probe: Callable[[], Hashable],
                 uri_probe: Callable[[str], Hashable], interval: float = DEFAULT_INTERVAL,
                 task_probe: Callable[[str, object], dict | None] | None = None):
        self.list_probe = list_probe
        self.uri_probe = uri_probe
        # task_probe(task_id, tenant) -> the DetailedTask, or None.
        self.task_probe = task_probe or (lambda task_id, tenant: None)
        self.interval = interval
        self._subs: list[Subscription] = []
        self._lock = threading.Lock()
        # One poll at a time: two would both see a change and report it twice.
        self._poll_lock = threading.Lock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def _safe(self, probe, *args):
        try:
            return probe(*args)
        except Exception as exc:  # noqa: BLE001 - a probe never breaks the poller
            _log(f"subscriptions: probe failed: {type(exc).__name__}")
            return None

    def open(self, sub_id, honored: Filter, tenant=None) -> Subscription:
        """Start a subscription; what it sees now is its baseline."""
        sub = Subscription(sub_id, honored, tenant=tenant)
        if honored.resources_list:
            sub.seen_list = self._safe(self.list_probe)
        sub.seen = {uri: self._safe(self.uri_probe, uri) for uri in honored.uris}
        sub.seen_tasks = {t: _task_mark(self._safe(self.task_probe, t, tenant))
                          for t in honored.task_ids}
        with self._lock:
            self._subs.append(sub)
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="mcp-subscriptions",
                                                daemon=True)
                self._thread.start()
        self._wake.set()
        return sub

    def close(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def close_all(self) -> None:
        """End every subscription from the server side (shutdown)."""
        with self._lock:
            subs, self._subs = self._subs, []
        for sub in subs:
            sub.outbox.put(CLOSED)

    def poke(self) -> None:
        """Poll now rather than at the next interval (a task just finished)."""
        self._wake.set()

    def __len__(self) -> int:
        with self._lock:
            return len(self._subs)

    def poll_once(self) -> None:
        with self._poll_lock:
            self._poll()

    def _poll(self) -> None:
        with self._lock:
            subs = list(self._subs)
        listing = None
        cache: dict = {}
        for sub in subs:
            if sub.honored.resources_list:
                if listing is None:
                    listing = (self._safe(self.list_probe),)
                if listing[0] != sub.seen_list:
                    sub.seen_list = listing[0]
                    sub.outbox.put(("list",))
            for uri in sub.honored.uris:
                if uri not in cache:
                    cache[uri] = self._safe(self.uri_probe, uri)
                if cache[uri] != sub.seen.get(uri):
                    sub.seen[uri] = cache[uri]
                    sub.outbox.put(("updated", uri))
            for task_id in sub.honored.task_ids:
                task = self._safe(self.task_probe, task_id, sub.tenant)
                mark = _task_mark(task)
                if task is not None and mark != sub.seen_tasks.get(task_id):
                    sub.seen_tasks[task_id] = mark
                    sub.outbox.put(("task", task_id, task))

    def _run(self) -> None:
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            if len(self):
                self.poll_once()

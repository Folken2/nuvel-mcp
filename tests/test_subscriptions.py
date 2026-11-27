"""subscriptions/listen: the filter, the broker, and the stdio and HTTP streams."""

from __future__ import annotations

import http.client
import json
import os
import queue
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import pytest

from _mcp_http_helpers import TIMEOUT, isolate_env, make_provider, service_token, start
from nuvel_mcp.protocol import subscriptions as subs
from nuvel_mcp.protocol.errors import McpError
from nuvel_mcp.server import SkillsMCPServer
from nuvel_mcp.skills_loader import SkillsLoader
from test_mcp_http_modern import headers_for
from test_protocol_modern import modern

INVOICE = "skill://docs/pdf-processing/templates/invoice.md"
SUB_ID = "io.modelcontextprotocol/subscriptionId"


def _write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data)


def _index(root: Path, names: list[str]) -> None:
    (root / "index.json").write_text(json.dumps({"themes": {"docs": [
        {"name": n, "description": n} for n in names]}}))


def _touch(path: Path, data: str) -> None:
    """Rewrite with a new size, so the change is seen whatever the clock's grain."""
    path.write_text(data)
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))


@pytest.fixture
def hub(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    root = tmp_path / "hub" / "skills"
    for name in ("pdf-processing", "other"):
        _write(root / "docs" / name / "SKILL.md",
               f"---\nname: {name}\ndescription: The {name} skill\n---\n\nBody.\n")
    _write(root / "docs" / "pdf-processing" / "templates" / "invoice.md", "# Invoice\n")
    _index(root, ["pdf-processing", "other"])
    return root


def listen(params=None, msg_id=7, caps=None):
    return modern(subs.METHOD, {"notifications": params or {}}, msg_id=msg_id, caps=caps)


# --- the filter and the broker -------------------------------------------------------

@pytest.mark.parametrize("value, field", [
    ([], "notifications must be an object"),
    ({"toolsListChanged": "yes"}, "toolsListChanged"),
    ({"resourceSubscriptions": "skill://a/b"}, "resourceSubscriptions"),
    ({"resourceSubscriptions": [1]}, "resourceSubscriptions"),
])
def test_a_malformed_filter_names_its_field(value, field):
    with pytest.raises(McpError) as exc:
        subs.read_filter({"notifications": value})
    assert exc.value.code == -32602 and field in exc.value.message


def test_the_broker_reports_changes_against_each_subscriptions_own_baseline():
    state = {"list": ("a",), "u": "d1"}
    broker = subs.Broker(lambda: state["list"], lambda uri: state.get(uri), interval=3600)
    early = broker.open(1, subs.Filter(resources_list=True, uris=("u",)))
    state["u"] = "d2"
    late = broker.open(2, subs.Filter(uris=("u",)))  # opened after the change
    broker.poll_once()
    assert early.outbox.get_nowait() == ("updated", "u")
    assert late.outbox.empty()
    state["list"] = ("a", "b")
    broker.poll_once()
    assert early.outbox.get_nowait() == ("list",) and late.outbox.empty()
    broker.close(early)
    state["u"] = "d3"
    broker.poll_once()
    assert early.outbox.empty() and late.outbox.get_nowait() == ("updated", "u")


def test_close_all_ends_every_subscription_from_the_server_side():
    broker = subs.Broker(lambda: None, lambda uri: None, interval=3600)
    sub = broker.open(1, subs.Filter())
    broker.close_all()
    assert sub.outbox.get_nowait() is subs.CLOSED and len(broker) == 0


def test_a_failing_probe_reads_as_missing():
    def boom(uri):
        raise OSError("disk")
    broker = subs.Broker(lambda: None, boom, interval=3600)
    assert broker.open(1, subs.Filter(uris=("u",))).seen == {"u": None}


@pytest.mark.parametrize("value", ["0", "x", "-1"])
def test_a_bad_watch_interval_stops_the_server(monkeypatch, value):
    monkeypatch.setenv(subs.INTERVAL_ENV, value)
    with pytest.raises(ValueError, match=subs.INTERVAL_ENV):
        subs.interval_from_env()


# --- the dispatcher ------------------------------------------------------------------

def test_the_ack_honors_only_what_the_server_can_report(hub):
    server = SkillsMCPServer(SkillsLoader.from_base(hub))
    opened = server.dispatch(listen({"toolsListChanged": True, "promptsListChanged": True,
                                     "resourcesListChanged": True,
                                     "resourceSubscriptions": [INVOICE, "file:///etc/passwd",
                                                               INVOICE]}))
    assert isinstance(opened, subs.Listen)
    assert opened.ack == {"jsonrpc": "2.0", "method": subs.ACKNOWLEDGED, "params": {
        "_meta": {SUB_ID: 7},
        "notifications": {"resourcesListChanged": True, "resourceSubscriptions": [INVOICE]}}}
    server.subscriptions.close(opened.subscription)


def test_listen_is_modern_only_and_validated(hub):
    server = SkillsMCPServer(SkillsLoader.from_base(hub))
    legacy = {"jsonrpc": "2.0", "id": 1, "method": subs.METHOD, "params": {}}
    assert server.dispatch(legacy)["error"]["code"] == -32601
    bad = server.dispatch(listen({"resourcesListChanged": 1}))
    assert bad["error"]["code"] == -32602


def test_without_a_hub_nothing_is_promised(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    opened = SkillsMCPServer(None).dispatch(listen({"resourcesListChanged": True}))
    assert opened.ack["params"]["notifications"] == {}


# --- stdio ---------------------------------------------------------------------------

class _Stdio:
    def __init__(self, hub: Path):
        env = {**os.environ, subs.INTERVAL_ENV: "0.1"}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "nuvel_mcp", "serve", "--skills-dir", str(hub)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=env)
        self.lines: queue.Queue = queue.Queue()
        import threading
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(json.loads(line))

    def send(self, message):
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()

    def next(self, timeout=TIMEOUT):
        return self.lines.get(timeout=timeout)

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(TIMEOUT)


def test_stdio_streams_updates_until_cancelled(hub):
    io = _Stdio(hub)
    try:
        io.send(listen({"resourcesListChanged": True, "resourceSubscriptions": [INVOICE]}))
        ack = io.next()
        assert ack["method"] == subs.ACKNOWLEDGED and ack["params"]["_meta"][SUB_ID] == 7
        time.sleep(0.3)
        _touch(hub / "docs" / "pdf-processing" / "templates" / "invoice.md", "# Invoice v2\n")
        assert io.next() == {"jsonrpc": "2.0", "method": "notifications/resources/updated",
                             "params": {"_meta": {SUB_ID: 7}, "uri": INVOICE}}
        _index(hub, ["pdf-processing"])
        assert io.next()["method"] == "notifications/resources/list_changed"
        # Other requests keep working while the stream is open.
        io.send(modern("tools/list", msg_id=8))
        assert io.next()["id"] == 8
        io.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                 "params": {"requestId": 7}})
        time.sleep(0.2)
        _touch(hub / "docs" / "pdf-processing" / "templates" / "invoice.md", "# Invoice v3!\n")
        time.sleep(0.5)
        io.send(modern("tools/list", msg_id=9))
        assert io.next()["id"] == 9  # nothing for the cancelled subscription came first
    finally:
        io.close()


def test_stdio_refuses_a_second_listen_with_the_same_id(hub):
    io = _Stdio(hub)
    try:
        io.send(listen())
        assert io.next()["method"] == subs.ACKNOWLEDGED
        io.send(listen())
        assert io.next()["error"]["code"] == -32600
    finally:
        io.close()


# --- HTTP ----------------------------------------------------------------------------

class _Stream:
    """One listen POST, read event by event."""

    def __init__(self, httpd, message, **headers):
        url = urllib.parse.urlsplit(httpd.url)
        self.conn = http.client.HTTPConnection(url.hostname, url.port, timeout=TIMEOUT)
        self.conn.request("POST", "/mcp", body=json.dumps(message),
                          headers=headers_for(message, **headers))
        self.response = self.conn.getresponse()

    def next(self):
        data = []
        while True:
            line = self.response.fp.readline()
            if not line:
                return None  # closed
            line = line.decode().rstrip("\n")
            if line.startswith("data: "):
                data.append(line[6:])
            elif line == "" and data:
                return json.loads("".join(data))

    def close(self):
        self.response.close()  # the response holds the socket open otherwise
        self.conn.close()


@pytest.fixture
def httpd(hub):
    server, thread = start(hub, no_auth=True)
    server.mcp.subscriptions.interval = 0.05
    yield server
    server.stop()
    thread.join(TIMEOUT)


def test_http_streams_updates_and_ends_with_the_listen_result(httpd, hub):
    stream = _Stream(httpd, listen({"resourceSubscriptions": [INVOICE]}))
    try:
        assert stream.response.status == 200
        assert stream.response.getheader("Content-Type") == "text/event-stream"
        assert stream.response.getheader("Mcp-Session-Id") is None
        assert stream.next()["method"] == subs.ACKNOWLEDGED
        _touch(hub / "docs" / "pdf-processing" / "templates" / "invoice.md", "# Invoice v2\n")
        assert stream.next()["params"] == {"_meta": {SUB_ID: 7}, "uri": INVOICE}
        httpd.mcp.subscriptions.close_all()  # what stop() does
        closing = stream.next()
        assert closing["id"] == 7 and closing["result"]["resultType"] == "complete"
        assert closing["result"]["_meta"][SUB_ID] == 7
        assert stream.next() is None
    finally:
        stream.close()


def test_closing_the_stream_cancels_the_subscription(httpd):
    stream = _Stream(httpd, listen({"resourcesListChanged": True}))
    assert stream.next()["method"] == subs.ACKNOWLEDGED
    assert len(httpd.mcp.subscriptions) == 1
    stream.close()
    deadline = time.monotonic() + TIMEOUT
    while len(httpd.mcp.subscriptions) and time.monotonic() < deadline:
        time.sleep(0.05)  # the handler notices on its next keep-alive write
    assert len(httpd.mcp.subscriptions) == 0


def test_listen_needs_an_event_stream(httpd):
    stream = _Stream(httpd, listen(), Accept="application/json")
    try:
        assert stream.response.status == 400
        assert json.loads(stream.response.read())["error"]["code"] == -32600
        assert len(httpd.mcp.subscriptions) == 0
    finally:
        stream.close()


def test_a_clients_newest_stream_displaces_its_oldest(hub, tmp_path):
    provider = make_provider(tmp_path / "oauth" / "store.json")
    server, thread = start(hub, provider, max_streams_per_principal=1)
    try:
        token = service_token(server)
        auth = {"Authorization": f"Bearer {token}"}
        first = _Stream(server, listen(msg_id=1), **auth)
        assert first.next()["method"] == subs.ACKNOWLEDGED
        second = _Stream(server, listen(msg_id=2), **auth)
        assert second.next()["method"] == subs.ACKNOWLEDGED
        assert first.next() is None  # displaced: closed without a result
        second.close()
        first.close()
    finally:
        server.stop()
        thread.join(TIMEOUT)

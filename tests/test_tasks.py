"""The Tasks extension: task tools, tasks/get, tasks/update, tasks/cancel,
notifications/tasks, the stores, and what happens to a task its server lost."""

from __future__ import annotations

import json
import stat
import threading
import time

import pytest

from _mcp_http_helpers import TIMEOUT, isolate_env, make_provider, service_token, start
from nuvel_mcp.auth import current_principal
from nuvel_mcp.protocol import subscriptions as subs
from nuvel_mcp.protocol.errors import McpError
from nuvel_mcp.server import SkillsMCPServer
from nuvel_mcp.skills_loader import SkillsLoader
from nuvel_mcp.tasks import runner as task_runner
from nuvel_mcp.tasks.store import LocalTaskStore, new_record
from test_mcp_http_modern import post_modern
from test_protocol_modern import modern

TASKS = "io.modelcontextprotocol/tasks"
CAPS = {"extensions": {TASKS: {}}}
REQUIRED = {"code": -32021, "message": "Missing required client capability",
            "data": {"requiredCapabilities": {"extensions": {TASKS: {}}}}}
NOT_FOUND = {"code": -32602, "message": "Task not found"}


def _skill(name, extra=""):
    return f"---\nname: {name}\ndescription: The {name} skill\n{extra}---\n\nBody.\n"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    root = tmp_path / "hub" / "skills"
    for name, text in {"good": _skill("good"), "ambiguous": _skill("ambiguous", "beta: yes\n"),
                       "unnamed": "---\ndescription: no name\n---\n"}.items():
        (root / "docs" / name).mkdir(parents=True)
        (root / "docs" / name / "SKILL.md").write_text(text)
    (root / "index.json").write_text(json.dumps({"themes": {"docs": [
        {"name": n, "description": n} for n in ("good", "ambiguous", "unnamed")]}}))
    return root


@pytest.fixture
def server(hub):
    return SkillsMCPServer(SkillsLoader.from_base(hub))


def call(server, method, params=None, caps=CAPS, msg_id=1):
    return server.dispatch(modern(method, params, caps=caps, msg_id=msg_id))


def wait(server, task_id, until=("completed", "failed", "cancelled")):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        task = call(server, "tasks/get", {"taskId": task_id})["result"]
        if task["status"] in until:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task still {task['status']}")


def add_tool(server, name, fn):
    server._tools[name] = fn
    server.task_tools.add(name)


AUDIT = {"total": 3, "published": 1, "skills": [
    {"theme": "docs", "name": "good", "published": True},
    {"theme": "docs", "name": "ambiguous", "published": False,
     "reason": "its frontmatter is refused (line 4: a value that YAML parsers read differently; "
               "quote it)"},
    {"theme": "docs", "name": "unnamed", "published": False,
     "reason": "its frontmatter name differs from the index name"}]}


# --- negotiation -----------------------------------------------------------------------

def test_discover_declares_tasks_and_initialize_does_not(server):
    assert TASKS in call(server, "server/discover")["result"]["capabilities"]["extensions"]
    init = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-11-25", "capabilities": {}}})
    assert TASKS not in init["result"]["capabilities"]["extensions"]


def test_without_the_extension_a_task_tool_answers_directly(server):
    result = call(server, "tools/call", {"name": "audit_hub", "arguments": {}}, caps={})["result"]
    assert result["resultType"] == "complete"
    assert json.loads(result["content"][0]["text"]) == AUDIT


def test_with_it_the_call_becomes_a_task_with_the_same_result(server):
    created = call(server, "tools/call", {"name": "audit_hub", "arguments": {}})["result"]
    assert created["resultType"] == "task" and len(created["taskId"]) == 32
    assert created["ttlMs"] == 24 * 3600 * 1000 and created["pollIntervalMs"] == 1000
    assert server._task_store().get(created["taskId"]) is not None  # durable before the answer
    done = wait(server, created["taskId"])
    assert done["status"] == "completed" and done["resultType"] == "complete"
    assert json.loads(done["result"]["content"][0]["text"]) == AUDIT
    assert done["createdAt"].endswith("Z") and "error" not in done


def test_other_tools_never_become_tasks(server):
    result = call(server, "tools/call", {"name": "list_themes", "arguments": {}})["result"]
    assert result["resultType"] == "complete"


# --- tasks/get, update, cancel -----------------------------------------------------------

@pytest.mark.parametrize("method", ["tasks/get", "tasks/update", "tasks/cancel"])
def test_task_methods_need_the_extension(server, method):
    assert call(server, method, {"taskId": "0" * 32}, caps={})["error"] == REQUIRED


@pytest.mark.parametrize("method", ["tasks/get", "tasks/update", "tasks/cancel"])
def test_task_methods_do_not_exist_on_legacy_sessions(server, method):
    legacy = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {"taskId": "0" * 32}}
    assert server.dispatch(legacy)["error"]["code"] == -32601


@pytest.mark.parametrize("task_id", ["0" * 32, "nope", None, "../../etc"])
def test_an_unknown_task_is_not_found(server, task_id):
    assert call(server, "tasks/get", {"taskId": task_id})["error"] == NOT_FOUND


def test_another_tenants_task_is_not_found(server):
    reset = current_principal.set("tenant-a")
    try:
        task_id = call(server, "tools/call", {"name": "audit_hub", "arguments": {}})["result"]["taskId"]
        wait(server, task_id)
    finally:
        current_principal.reset(reset)
    reset = current_principal.set("tenant-b")
    try:
        for method in ("tasks/get", "tasks/update", "tasks/cancel"):
            params = {"taskId": task_id, "inputResponses": {}}
            assert call(server, method, params)["error"] == NOT_FOUND
    finally:
        current_principal.reset(reset)


def test_update_acknowledges_and_changes_nothing(server):
    task_id = call(server, "tools/call", {"name": "audit_hub", "arguments": {}})["result"]["taskId"]
    before = wait(server, task_id)
    ack = call(server, "tasks/update", {"taskId": task_id, "inputResponses": {"k": {}}})
    assert ack["result"] == {"resultType": "complete", "_meta": ack["result"]["_meta"]}
    assert call(server, "tasks/get", {"taskId": task_id})["result"]["status"] == before["status"]


def test_cancel_stops_a_tool_that_checks(server):
    started = threading.Event()

    def slow(args):
        started.set()
        while not task_runner.cancelled():
            time.sleep(0.01)
        raise task_runner.TaskCancelled()

    add_tool(server, "slow", slow)
    task_id = call(server, "tools/call", {"name": "slow", "arguments": {}})["result"]["taskId"]
    assert started.wait(TIMEOUT)
    assert call(server, "tasks/get", {"taskId": task_id})["result"]["status"] == "working"
    assert call(server, "tasks/cancel", {"taskId": task_id})["result"]["resultType"] == "complete"
    assert wait(server, task_id)["status"] == "cancelled"


def test_a_task_cancelled_before_it_starts_never_runs(hub, monkeypatch):
    monkeypatch.setenv(task_runner.WORKERS_ENV, "1")
    server = SkillsMCPServer(SkillsLoader.from_base(hub))
    gate, ran = threading.Event(), []
    add_tool(server, "block", lambda args: gate.wait(TIMEOUT) and {})
    add_tool(server, "later", lambda args: ran.append(1) or {})
    first = call(server, "tools/call", {"name": "block", "arguments": {}})["result"]["taskId"]
    second = call(server, "tools/call", {"name": "later", "arguments": {}})["result"]["taskId"]
    call(server, "tasks/cancel", {"taskId": second})
    gate.set()
    assert wait(server, first)["status"] == "completed"
    assert wait(server, second)["status"] == "cancelled" and ran == []


def test_cancelling_a_finished_task_leaves_it_finished(server):
    task_id = call(server, "tools/call", {"name": "audit_hub", "arguments": {}})["result"]["taskId"]
    wait(server, task_id)
    call(server, "tasks/cancel", {"taskId": task_id})
    assert call(server, "tasks/get", {"taskId": task_id})["result"]["status"] == "completed"


def test_a_protocol_error_fails_the_task_and_a_tool_error_completes_it(server):
    def broken(args):
        raise McpError(-32602, "Argument 'x' must be a string")

    add_tool(server, "broken", broken)
    failed = wait(server, call(server, "tools/call", {"name": "broken", "arguments": {}})
                  ["result"]["taskId"])
    assert failed["status"] == "failed"
    assert failed["error"] == {"code": -32602, "message": "Argument 'x' must be a string"}
    server.task_tools.add("record_run_trace")
    rejected = wait(server, call(server, "tools/call", {"name": "record_run_trace",
                                                        "arguments": {"trace": {}}})["result"]["taskId"])
    assert rejected["status"] == "completed" and rejected["result"]["isError"] is True


def test_an_unexpected_exception_fails_the_task_as_a_direct_call_would(server):
    def leaky(args):
        raise RuntimeError("detail")

    add_tool(server, "leaky", leaky)
    server.enable_remote_mode()  # remote callers get fixed text, as for direct calls
    server._allowed_tools = server._allowed_tools | {"leaky"}
    task = wait(server, call(server, "tools/call", {"name": "leaky", "arguments": {}})
                ["result"]["taskId"])
    assert task["error"] == {"code": -32603, "message": "Internal error"}


def test_results_are_never_logged(server, capsys):
    add_tool(server, "say", lambda args: {"said": "a private sentence"})
    wait(server, call(server, "tools/call", {"name": "say", "arguments": {}})["result"]["taskId"])
    assert "a private sentence" not in capsys.readouterr().err


# --- lifetime --------------------------------------------------------------------------

def test_an_expired_task_is_gone(server):
    task_id = call(server, "tools/call", {"name": "audit_hub", "arguments": {}})["result"]["taskId"]
    wait(server, task_id)
    server._task_store().update(task_id, {"expires_at": time.time() - 1})
    assert call(server, "tasks/get", {"taskId": task_id})["error"] == NOT_FOUND
    assert server._task_store().get(task_id) is None


def test_a_task_whose_server_stopped_reads_as_failed(server):
    store = server._task_store()
    lost = new_record("a" * 32, None, "audit_hub", 3600, runner="another-process",
                      now=time.time() - 120)  # its runner last beat two minutes ago
    store.create(lost)
    task = call(server, "tasks/get", {"taskId": "a" * 32})["result"]
    assert task["status"] == "failed" and task["error"] == task_runner.INTERRUPTED
    # Ours, but no worker runs it (this process restarted under the same id).
    store.create(new_record("b" * 32, None, "audit_hub", 3600, runner=server.tasks.runner.run_id))
    assert call(server, "tasks/get", {"taskId": "b" * 32})["result"]["status"] == "failed"


def test_a_completed_task_survives_a_restart(hub):
    first = SkillsMCPServer(SkillsLoader.from_base(hub))
    task_id = call(first, "tools/call", {"name": "audit_hub", "arguments": {}})["result"]["taskId"]
    wait(first, task_id)
    again = SkillsMCPServer(SkillsLoader.from_base(hub))  # same feedback dir, new process id
    assert json.loads(wait(again, task_id)["result"]["content"][0]["text"]) == AUDIT


def test_task_files_are_private_and_purged(tmp_path):
    store = LocalTaskStore(tmp_path)
    store.create(new_record("c" * 32, "t", "x", 60, "r", now=1000.0))
    path = tmp_path / "_tasks" / f"{'c' * 32}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert store.purge(1059.0) == 0 and store.purge(1061.0) == 1 and not path.exists()


def test_a_finished_record_never_changes_status(tmp_path):
    store = LocalTaskStore(tmp_path)
    store.create({**new_record("d" * 32, None, "x", 60, "r"), "status": "completed",
                  "result": {"ok": 1}})
    store.update("d" * 32, {"status": "failed", "error": {}, "heartbeat_at": 5})
    record = store.get("d" * 32)
    assert record["status"] == "completed" and "error" not in record and record["heartbeat_at"] == 5


@pytest.mark.parametrize("name, value", [(task_runner.TTL_ENV, "10"),
                                         (task_runner.WORKERS_ENV, "0"),
                                         (task_runner.WORKERS_ENV, "many")])
def test_bad_task_settings_stop_the_server(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=name):
        task_runner.settings_from_env()


# --- notifications/tasks ------------------------------------------------------------------

def test_listen_for_a_task_needs_the_extension(server):
    listen = modern(subs.METHOD, {"notifications": {"taskIds": ["0" * 32]}}, msg_id=5, caps={})
    assert server.dispatch(listen)["error"] == REQUIRED


def test_a_listener_hears_its_own_task_finish(server):
    gate = threading.Event()
    add_tool(server, "block", lambda args: gate.wait(TIMEOUT) and {"done": True})
    mine = call(server, "tools/call", {"name": "block", "arguments": {}})["result"]["taskId"]
    opened = server.dispatch(modern(subs.METHOD, {"notifications": {
        "taskIds": [mine, "f" * 32]}}, msg_id=5, caps=CAPS))
    assert opened.ack["params"]["notifications"] == {"taskIds": [mine]}  # not someone else's
    gate.set()
    event = opened.subscription.outbox.get(timeout=TIMEOUT)
    note = subs.notification(5, event)
    assert note["method"] == "notifications/tasks"
    assert note["params"]["taskId"] == mine and note["params"]["status"] == "completed"
    assert note["params"]["_meta"] == {"io.modelcontextprotocol/subscriptionId": 5}
    server.subscriptions.close(opened.subscription)


# --- HTTP ----------------------------------------------------------------------------------

def test_over_http_tasks_route_by_mcp_name_and_stay_with_their_tenant(hub, tmp_path):
    provider = make_provider(tmp_path / "oauth" / "store.json")
    httpd, thread = start(hub, provider)
    try:
        alice, bob = (f"Bearer {service_token(httpd)}" for _ in range(2))
        status, _, body = post_modern(httpd, modern("tools/call", {
            "name": "audit_hub", "arguments": {}}, caps=CAPS), Authorization=alice)
        task_id = body["result"]["taskId"]
        get = modern("tasks/get", {"taskId": task_id}, caps=CAPS)
        status, _, body = post_modern(httpd, get, Authorization=alice)
        assert status == 400 and body["error"]["code"] == -32020  # no Mcp-Name
        deadline = time.monotonic() + TIMEOUT
        while True:
            status, _, body = post_modern(httpd, get, Authorization=alice, **{"Mcp-Name": task_id})
            if body["result"]["status"] == "completed" or time.monotonic() > deadline:
                break
        assert status == 200 and body["result"]["status"] == "completed"
        status, _, body = post_modern(httpd, get, Authorization=bob, **{"Mcp-Name": task_id})
        assert status == 400 and body["error"] == NOT_FOUND
    finally:
        httpd.stop()
        thread.join(TIMEOUT)


# --- Postgres ----------------------------------------------------------------------------

def test_the_postgres_store(pg_db):
    from nuvel_mcp.db.stores import PostgresTaskStore

    store = PostgresTaskStore(pg_db)
    store.create(new_record("e" * 32, "t", "x", 60, "r", now=1000.0))
    assert store.get("e" * 32)["tenant"] == "t" and store.get("nope") is None
    store.update("e" * 32, {"status": "completed", "result": {"content": []}})
    assert store.update("e" * 32, {"status": "failed"})["status"] == "completed"
    assert store.purge(1059.0) == 0 and store.purge(1061.0) == 1 and store.get("e" * 32) is None


def test_tasks_on_postgres_end_to_end(hub, pg_db):
    from nuvel_mcp.db.stores import PostgresTaskStore

    server = SkillsMCPServer(SkillsLoader.from_base(hub), task_store=PostgresTaskStore(pg_db))
    task_id = call(server, "tools/call", {"name": "audit_hub", "arguments": {}})["result"]["taskId"]
    assert json.loads(wait(server, task_id)["result"]["content"][0]["text"]) == AUDIT

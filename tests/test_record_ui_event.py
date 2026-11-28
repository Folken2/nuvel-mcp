"""The record_ui_event tool: validation without echo, tenant stamping, storage,
dedupe, relay, and the 'ui' block of check_skill_health."""

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nuvel_mcp import server as server_mod
from nuvel_mcp import ui_events
from nuvel_mcp.auth import current_principal
from nuvel_mcp.server import FEEDBACK_TOOLS, HTTP_DEFAULT_TOOLS, SkillsMCPServer
from nuvel_mcp.ui_events import LocalUiEventStore, validate_ui_event

FIXTURE = Path(__file__).parent / "fixtures" / "ui_event_example.json"
DAY_FILE = Path("_ui_events") / "chief-of-staff.dashboard" / "2026-11-20.jsonl"
OTHER_ID = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def example():
    return json.loads(FIXTURE.read_text())


@pytest.fixture(autouse=True)
def _no_relay(monkeypatch):
    monkeypatch.delenv("NUVEL_FEEDBACK_URL", raising=False)


def call(s, event):
    res = s.handle_tools_call({"name": "record_ui_event", "arguments": {"event": event}})
    return res, json.loads(res["content"][0]["text"])


def health(s, **args):
    res = s.handle_tools_call({"name": "check_skill_health", "arguments": args})
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


def test_listed_and_exposed_over_http(tmp_path):
    assert "record_ui_event" in FEEDBACK_TOOLS and "record_ui_event" in HTTP_DEFAULT_TOOLS
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    tool = {t["name"]: t for t in s.handle_tools_list({})["tools"]}["record_ui_event"]
    assert "user content" in tool["description"] and "duplicate" in tool["description"]
    assert tool["inputSchema"]["required"] == ["event"]
    versions = tool["inputSchema"]["properties"]["event"]["oneOf"]
    assert [v["properties"]["schema"]["const"] for v in versions] == [
        "nuvel.ui_event/1", "nuvel.ui_event/2"]
    for event_schema in versions:
        assert event_schema["additionalProperties"] is False
        assert "first_action_s" not in event_schema["required"]


def test_records_valid_event(tmp_path, example):
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    res, body = call(s, example)
    assert res["isError"] is False
    assert body == {"status": "recorded", "run_id": example["run_id"]}
    line = json.loads((tmp_path / DAY_FILE).read_text())
    assert line == {**example, "started_at": "2026-11-20T08:15:00Z"}
    assert not (tmp_path / "_runs").exists()  # never mixed with run traces


def test_first_action_s_is_optional(tmp_path, example):
    del example["first_action_s"]
    _, body = call(SkillsMCPServer(None, feedback_dir=tmp_path), example)
    assert body["status"] == "recorded"


def test_duplicate_run_id_returns_duplicate(tmp_path, example):
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    call(s, example)
    res, body = call(s, {**example, "events": {"view.today": 9}})
    assert res["isError"] is False
    assert body == {"status": "duplicate", "run_id": example["run_id"]}
    assert len((tmp_path / DAY_FILE).read_text().splitlines()) == 1


@pytest.mark.parametrize("change, path, secret", [
    # Unknown top-level field.
    (lambda e: e.update(note="Ana at acme asked about pricing"), "/note", "acme"),
    (lambda e: e.update(page_title="q3-acme-deal"), "/page_title", "q3-acme-deal"),
    # A free-text-looking event key: reported as '*', never echoed.
    (lambda e: e["events"].update({"Reply to Ana about the Acme contract": 1}), "/events/*", "Acme"),
    (lambda e: e["events"].update({"ana@acme.com": 1}), "/events/*", "ana@acme.com"),
    # Bad UUIDs.
    (lambda e: e.update(run_id="ana@acme.com"), "/run_id", "ana@acme.com"),
    (lambda e: e.update(install_id="not-a-uuid-acme"), "/install_id", "not-a-uuid-acme"),
    # Closed enums and bounds.
    (lambda e: e.update(surface="acme-portal"), "/surface", "acme-portal"),
    (lambda e: e["events"].update({"task.close": 10001}), "/events/task.close", "10001"),
    (lambda e: e.update(first_action_s=86401), "/first_action_s", "86401"),
    (lambda e: e["plugin"].update(extra="acme"), "/plugin/extra", "acme"),
    # tenant is the server's to set.
    (lambda e: e.update(tenant="acme-evil"), "/tenant", "acme-evil"),
])
def test_rejected_without_echo(tmp_path, example, change, path, secret):
    change(example)
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    res, body = call(s, example)
    assert res["isError"] is True
    assert body == {"status": "rejected", "errors": [path]}
    assert secret not in json.dumps(res)
    assert not (tmp_path / "_ui_events").exists()


def test_too_many_event_keys_rejected(tmp_path, example):
    example["events"] = {f"view.k{i}": 1 for i in range(51)}
    res, body = call(SkillsMCPServer(None, feedback_dir=tmp_path), example)
    assert res["isError"] is True and body == {"status": "rejected", "errors": ["/events"]}
    example["events"] = {f"view.k{i}": 1 for i in range(50)}
    assert validate_ui_event(example) == []


def test_missing_event_rejected(tmp_path):
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    res = s.handle_tools_call({"name": "record_ui_event", "arguments": {}})
    assert res["isError"] is True
    assert json.loads(res["content"][0]["text"]) == {"status": "rejected", "errors": ["/"]}


def test_tenant_stamped_from_principal(tmp_path, example):
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    token = current_principal.set("acme")
    try:
        call(s, example)
    finally:
        current_principal.reset(token)
    assert json.loads((tmp_path / DAY_FILE).read_text())["tenant"] == "acme"


def test_relay_sends_the_stored_record(tmp_path, example, monkeypatch):
    monkeypatch.setenv("NUVEL_FEEDBACK_URL", "https://relay.example/feedback")
    sent = []
    monkeypatch.setattr(server_mod.urllib.request, "urlopen",
                        lambda request, timeout=None: sent.append(request) or io.BytesIO(b"{}"))
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    call(s, example)
    call(s, example)  # the duplicate is not relayed
    (request,) = sent
    assert json.loads(request.data) == {
        "type": "ui_event", "record": json.loads((tmp_path / DAY_FILE).read_text())}


def test_storage_failure_is_internal_error_without_detail(tmp_path, example):
    class Broken:
        def append(self, event):
            raise OSError("/srv/secret/path is full")

        def query(self, *a):
            return []

    s = SkillsMCPServer(None, feedback_dir=tmp_path, ui_event_store=Broken())
    with pytest.raises(server_mod.McpError) as exc:
        call(s, example)
    assert "secret" not in str(exc.value)


# --- check_skill_health 'ui' block ---------------------------------------------------


def _now(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_health_sums_dashboard_visits_and_events(tmp_path, example):
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    call(s, {**example, "started_at": _now()})
    call(s, {**example, "run_id": OTHER_ID, "started_at": _now(days=2),
             "events": {"view.today": 1, "view.decisions": 2}})
    call(s, {**example, "run_id": "22222222-2222-4222-8222-222222222222",
             "started_at": _now(days=31)})  # outside the window
    body = health(s, skill_name="dashboard", plugin="chief-of-staff")
    assert body["ui"] == {"last_30_days": {
        "visits": 2,
        "events": {"draft.save_edited": 1, "task.close": 1, "view.decisions": 2, "view.today": 4},
    }}
    assert body["runs"]["total"] == 0  # the dashboard skill's run traces, separately


def test_health_ui_block_only_for_surfaces_with_plugin(tmp_path, example):
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    call(s, {**example, "started_at": _now()})
    assert "ui" not in health(s, skill_name="radar", plugin="chief-of-staff")
    assert "ui" not in health(s, skill_name="dashboard")
    empty = health(s, skill_name="dashboard", plugin="other-plugin")["ui"]
    assert empty == {"last_30_days": {"visits": 0, "events": {}}}


def test_remote_health_is_scoped_to_the_caller(tmp_path, example):
    s = SkillsMCPServer(None, feedback_dir=tmp_path, health_runs_only=True)
    for tenant, run_id in (("acme", example["run_id"]), ("beta", OTHER_ID)):
        token = current_principal.set(tenant)
        try:
            call(s, {**example, "run_id": run_id, "started_at": _now()})
        finally:
            current_principal.reset(token)
    token = current_principal.set("acme")
    try:
        body = health(s, skill_name="dashboard", plugin="chief-of-staff")
    finally:
        current_principal.reset(token)
    assert set(body) == {"skill", "plugin", "runs", "ui"}
    assert body["ui"]["last_30_days"]["visits"] == 1
    assert body["ui"]["last_30_days"]["events"]["view.today"] == 3


def test_health_ignores_malformed_custom_store_records(tmp_path):
    class Store:
        def append(self, event):
            return True

        def query(self, plugin, surface, since, tenant):
            return [{"events": {"view.today": 2, "x": True, "y": "3"}}, {"events": None}, {}]

    s = SkillsMCPServer(None, feedback_dir=tmp_path, ui_event_store=Store())
    assert health(s, skill_name="dashboard", plugin="chief-of-staff")["ui"] == {
        "last_30_days": {"visits": 3, "events": {"view.today": 2}}}


def test_local_store_files_by_surface(tmp_path, example):
    store = LocalUiEventStore(tmp_path)
    record = {**example, "started_at": "2026-11-20T08:15:00Z"}
    assert store.append(record) is True and store.append(record) is False
    since = datetime(2026, 11, 20, tzinfo=timezone.utc)
    assert store.query("chief-of-staff", "dashboard", since, None) == [record]
    assert store.query("chief-of-staff", "radar", since, None) == []
    assert (tmp_path / DAY_FILE).is_file()


# --- nuvel.ui_event/2: the server's own views --------------------------------------

def _browser_visit(example, **changes):
    event = {**example, "schema": "nuvel.ui_event/2", "surface": "skill-browser",
             "plugin": {"name": "nuvel-mcp", "version": "0.7.0"},
             "events": {"search": 2, "open_skill": 1}}
    event.update(changes)
    return event


def test_version_1_events_stay_valid_as_version_1(example):
    assert ui_events.validate_ui_event(example) == []
    # A v1 event cannot name a surface only v2 knows.
    assert ui_events.validate_ui_event({**example, "surface": "skill-browser"}) == ["/surface"]


def test_a_skill_browser_visit_is_recorded(tmp_path, example):
    s = SkillsMCPServer(None, feedback_dir=tmp_path)
    res, body = call(s, _browser_visit(example))
    assert res["isError"] is False and body["status"] == "recorded"
    assert (tmp_path / "_ui_events" / "nuvel-mcp.skill-browser").is_dir()


@pytest.mark.parametrize("changes, path", [
    ({"plugin": {"name": "chief-of-staff", "version": "0.7.0"}}, "/plugin/name"),
    ({"events": {"search": 1, "view.today": 1}}, "/events/*"),
    ({"schema": "nuvel.ui_event/3"}, "/schema"),
])
def test_the_server_owns_its_surfaces_catalogue(example, changes, path):
    assert path in ui_events.validate_ui_event(_browser_visit(example, **changes))


def test_surfaces_span_every_version():
    assert ui_events.surfaces() == {"dashboard", "skill-browser"}

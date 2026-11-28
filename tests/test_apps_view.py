"""MCP Apps: the skill browser view, its resource, and its tool.

The protocol checks run everywhere. The browser check runs the view in a real
Chromium through ``tests/apps_host.js`` (a minimal Apps host) when Node and
Playwright are installed, and skips otherwise. Set
``NUVEL_MCP_SCREENSHOT_DIR`` to keep its screenshots.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from html.parser import HTMLParser
from importlib import resources
from pathlib import Path

import pytest

from _mcp_http_helpers import TIMEOUT, isolate_env, make_hub, start
from nuvel_mcp.extensions import apps
from nuvel_mcp.server import SkillsMCPServer
from nuvel_mcp.skills_loader import SkillsLoader
from test_mcp_http_modern import post_modern
from test_protocol_modern import modern

VIEW = "ui://nuvel-skills/skill-browser"


@pytest.fixture
def hub(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    return make_hub(tmp_path / "hub")


@pytest.fixture
def server(hub):
    return SkillsMCPServer(SkillsLoader.from_base(hub))


def result(server, method, params=None):
    response = server.dispatch(modern(method, params))
    assert "error" not in response, response
    return response["result"]


def test_discover_declares_apps(server):
    caps = result(server, "server/discover")["capabilities"]
    assert caps["extensions"]["io.modelcontextprotocol/ui"] == {}


def test_browse_skills_links_its_view_and_keeps_a_text_fallback(server):
    tools = {t["name"]: t for t in result(server, "tools/list")["tools"]}
    assert tools["browse_skills"]["_meta"] == {"ui": {"resourceUri": VIEW}}
    called = result(server, "tools/call", {"name": "browse_skills", "arguments": {"query": "roll"}})
    assert called["structuredContent"]["skills"][0]["name"] == "rollback"
    assert json.loads(called["content"][0]["text"]) == called["structuredContent"]


def test_browse_skills_without_a_query_lists_the_catalog(server):
    data = result(server, "tools/call", {"name": "browse_skills", "arguments": {}})
    assert data["structuredContent"]["total"] == 3 and data["structuredContent"]["query"] == ""


def test_the_view_is_an_mcp_app_resource(server):
    read = result(server, "resources/read", {"uri": VIEW})
    content = read["contents"][0]
    assert content["mimeType"] == "text/html;profile=mcp-app"
    assert content["text"].startswith("<!DOCTYPE html>")
    assert content["_meta"] == {"ui": {"prefersBorder": True}}  # no csp: the strict default
    assert read["ttlMs"] > 0


def test_an_unknown_view_is_not_found(server):
    response = server.dispatch(modern("resources/read", {"uri": "ui://nuvel-skills/nope"}))
    assert response["error"] == {"code": -32602, "message": "Resource not found"}


def test_views_stay_out_of_resources_list(server):
    uris = [r["uri"] for r in result(server, "resources/list")["resources"]]
    assert not [u for u in uris if u.startswith("ui://")]


class _Audit(HTMLParser):
    """What a view file loads or runs from outside itself."""

    def __init__(self):
        super().__init__()
        self.external: list[str] = []
        self.handlers: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name in ("src", "href") and value and not value.startswith(("#", "data:")):
                self.external.append(f"{tag}[{name}]")
            if name.startswith("on"):
                self.handlers.append(f"{tag}[{name}]")


@pytest.mark.parametrize("view", apps.VIEWS.values(), ids=lambda v: v.name)
def test_views_are_self_contained(view):
    html = resources.files("nuvel_mcp").joinpath("apps", view.filename).read_text()
    audit = _Audit()
    audit.feed(html)
    assert audit.external == [] and audit.handlers == []
    # Data reaches the page as text only.
    assert "innerHTML" not in html and "outerHTML" not in html and "insertAdjacentHTML" not in html
    for network in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "import("):
        assert network not in html


def _browser_ready() -> bool:
    if shutil.which("node") is None:
        return False
    probe = subprocess.run(["node", "-e", "require('playwright')"], capture_output=True)
    return probe.returncode == 0


@pytest.mark.skipif(not _browser_ready(), reason="needs Node and Playwright")
def test_the_view_in_a_browser(hub, tmp_path):
    httpd, thread = start(hub, no_auth=True)
    shots = Path(os.environ.get("NUVEL_MCP_SCREENSHOT_DIR") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    view_file = resources.files("nuvel_mcp").joinpath("apps", apps.SKILL_BROWSER.filename)
    try:
        run = subprocess.run(
            ["node", str(Path(__file__).with_name("apps_host.js")), httpd.url + "/mcp",
             str(view_file), str(shots)],
            capture_output=True, text=True, timeout=120)
        health = None
        if run.returncode == 0:
            message = modern("tools/call", {"name": "check_skill_health", "arguments": {
                "plugin": "nuvel-mcp", "skill_name": "skill-browser"}})
            status, _, body = post_modern(httpd, message)
            health = json.loads(body["result"]["content"][0]["text"])
    finally:
        httpd.stop()
        thread.join(TIMEOUT)
    assert run.returncode == 0, run.stderr
    report = json.loads(run.stdout)
    for label in ("desktop", "phone"):
        seen = report[label]
        assert seen["firstResults"] == ["rollback"]          # from the tool-result
        assert seen["searchResults"] == ["onboarding"]       # the view's own tools/call
        assert "Employee onboarding checklist" in seen["skillBody"]
        assert seen["background"] == "rgb(16, 16, 20)"       # the host's theme variable
        assert seen["overflowX"] is False
        log = seen["log"]
        assert log["violations"] == []                       # nothing broke the default CSP
        assert log["methods"][:2] == ["ui/initialize", "ui/notifications/initialized"]
        assert "tools/call" in log["methods"] and log["sizes"]
        assert log["messages"] == [{"role": "user",
                                    "content": {"type": "text", "text": "Use the onboarding skill."}}]
        # Teardown: the visit went out before the view answered.
        assert log["tornDown"] and log["toolCalls"][-1] == "record_ui_event"
    # One visit per page (desktop, phone), each: one search, one skill opened, used once.
    assert health["ui"]["last_30_days"] == {
        "visits": 2, "events": {"open_skill": 2, "search": 2, "use_skill": 2}}


def test_the_view_records_visits_only_where_record_ui_event_is_offered(hub):
    loader = SkillsLoader.from_base(hub)
    on = result(SkillsMCPServer(loader), "tools/call", {"name": "browse_skills", "arguments": {}})
    assert on["structuredContent"]["visits"] == {
        "record": True, "schema": "nuvel.ui_event/2",
        "plugin": {"name": "nuvel-mcp", "version": on["_meta"][
            "io.modelcontextprotocol/serverInfo"]["version"]}}
    off = SkillsMCPServer(loader, tools=frozenset({"browse_skills", "get_skill"}))
    called = result(off, "tools/call", {"name": "browse_skills", "arguments": {}})
    assert called["structuredContent"]["visits"] == {"record": False}

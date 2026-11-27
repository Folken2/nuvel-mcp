"""The 2026-07-28 request path of the dispatcher: envelope, discover, results.

Driven through ``SkillsMCPServer.dispatch``, the same entry point stdio and
HTTP use.
"""

from __future__ import annotations

import pytest

from _mcp_http_helpers import isolate_env, make_hub
from nuvel_mcp.protocol import versions
from nuvel_mcp.server import SERVER_INFO, SkillsMCPServer
from nuvel_mcp.skills_loader import SkillsLoader

VERSION = "2026-07-28"
SERVER_INFO_KEY = "io.modelcontextprotocol/serverInfo"


def modern(method, params=None, msg_id=1, caps=None, version=VERSION, client=None):
    meta = {"io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientCapabilities": {} if caps is None else caps}
    if client is not None:
        meta["io.modelcontextprotocol/clientInfo"] = client
    message = {"jsonrpc": "2.0", "method": method, "params": {**(params or {}), "_meta": meta}}
    if msg_id is not None:
        message["id"] = msg_id
    return message


@pytest.fixture
def server(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    return SkillsMCPServer(SkillsLoader.from_base(make_hub(tmp_path / "hub")))


def test_discover_names_modern_versions_capabilities_and_identity(server):
    result = server.dispatch(modern("server/discover"))["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"] == list(versions.MODERN)
    assert result["capabilities"] == {
        "resources": {"listChanged": True, "subscribe": True}, "tools": {},
        "extensions": {"io.modelcontextprotocol/skills": {"directoryRead": True},
                       "io.modelcontextprotocol/ui": {}}}
    assert result["_meta"][SERVER_INFO_KEY]["name"] == SERVER_INFO["name"]
    assert result["ttlMs"] > 0 and result["cacheScope"] == "private"


def test_tools_list_is_stamped_cacheable_and_sorted(server):
    result = server.dispatch(modern("tools/list"))["result"]
    names = [tool["name"] for tool in result["tools"]]
    assert names == sorted(names) and "search_skills" in names
    assert (result["resultType"], result["cacheScope"]) == ("complete", "private")
    assert isinstance(result["ttlMs"], int)


def test_tools_call_is_complete_and_not_cacheable(server):
    response = server.dispatch(modern("tools/call", {"name": "list_themes", "arguments": {}}))
    result = response["result"]
    assert result["resultType"] == "complete" and result["isError"] is False
    assert "ttlMs" not in result and "cacheScope" not in result


def test_resources_read_is_cacheable(server):
    uri = "skill://hr/onboarding"
    result = server.dispatch(modern("resources/read", {"uri": uri}))["result"]
    assert result["contents"][0]["uri"] == uri
    assert result["ttlMs"] > 0 and result["cacheScope"] == "private"


@pytest.mark.parametrize("meta, needle", [
    ({"io.modelcontextprotocol/protocolVersion": VERSION}, "clientCapabilities"),
    ({"io.modelcontextprotocol/protocolVersion": 20260728,
      "io.modelcontextprotocol/clientCapabilities": {}}, "protocolVersion must be a string"),
    ({"io.modelcontextprotocol/protocolVersion": VERSION,
      "io.modelcontextprotocol/clientCapabilities": []}, "must be an object"),
])
def test_a_malformed_envelope_is_invalid_params(server, meta, needle):
    message = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {"_meta": meta}}
    error = server.dispatch(message)["error"]
    assert error["code"] == -32602 and needle in error["message"]


def test_an_unsupported_version_lists_what_is_served(server):
    error = server.dispatch(modern("tools/list", version="1900-01-01"))["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": list(versions.MODERN), "requested": "1900-01-01"}


def test_an_unsupported_version_is_not_echoed_unless_version_shaped(server):
    error = server.dispatch(modern("tools/list", version="<script>alert(1)</script>"))["error"]
    assert error["code"] == -32022 and error["data"]["requested"] == ""


@pytest.mark.parametrize("method", ["ping", "logging/setLevel", "resources/subscribe", "nope/x"])
def test_removed_and_unknown_methods_are_not_found_on_the_modern_path(server, method):
    assert server.dispatch(modern(method))["error"]["code"] == -32601


def test_a_modern_notification_gets_no_answer(server):
    assert server.dispatch(modern("tools/list", msg_id=None)) is None


@pytest.mark.parametrize("requested, answered", [
    ("2025-11-25", "2025-11-25"),
    ("2025-06-18", "2025-06-18"),
    ("2024-11-05", "2024-11-05"),
    ("2026-07-28", "2025-11-25"),  # not a handshake version
    ("1999-01-01", "2025-11-25"),
    (None, "2025-11-25"),
])
def test_initialize_negotiates_a_legacy_version(server, requested, answered):
    params = {"capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
    if requested is not None:
        params["protocolVersion"] = requested
    result = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": params})["result"]
    assert result["protocolVersion"] == answered
    assert result["capabilities"]["resources"] == {} and result["capabilities"]["tools"] == {}


def test_initialize_stays_legacy_with_an_envelope_on_it(server):
    message = modern("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}})
    assert server.dispatch(message)["result"]["protocolVersion"] == "2025-11-25"


def test_legacy_results_keep_their_shape(server):
    result = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
    assert "resultType" not in result and "ttlMs" not in result and "_meta" not in result


class _Usage:
    def __init__(self):
        self.events = []

    def record(self, event):
        self.events.append(event)


def test_usage_reads_the_client_app_from_each_modern_request(server):
    server.usage = _Usage()
    server.dispatch(modern("tools/list", client={"name": "claude-ai", "version": "1.2"}))
    event = server.usage.events[-1]
    assert (event["client_name"], event["client_version"], event["protocol_version"]) == (
        "claude-ai", "1.2", VERSION)
    assert event["method"] == "tools/list" and event["outcome"] == "ok"


def test_usage_records_discover_by_name(server):
    server.usage = _Usage()
    server.dispatch(modern("server/discover"))
    assert server.usage.events[-1]["method"] == "server/discover"

"""Streamable HTTP for 2026-07-28: no session, headers checked against the body.

Real servers on 127.0.0.1:0, driven with http.client.
"""

from __future__ import annotations

import base64
import http.client
import json
import urllib.parse

import pytest

from _mcp_http_helpers import INIT, TIMEOUT, isolate_env, make_hub, make_provider, service_token, start
from nuvel_mcp.protocol.headers import check, decode_value
from nuvel_mcp.protocol.errors import McpError
from test_protocol_modern import VERSION, modern


@pytest.fixture
def hub(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    return make_hub(tmp_path / "hub")


@pytest.fixture
def httpd(hub):
    server, thread = start(hub, no_auth=True)
    yield server
    server.stop()
    thread.join(TIMEOUT)


def headers_for(message, **overrides):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": VERSION, "Mcp-Method": message["method"]}
    params = message.get("params") or {}
    if message["method"] in ("tools/call", "prompts/get"):
        headers["Mcp-Name"] = params.get("name")
    if message["method"] == "resources/read":
        headers["Mcp-Name"] = params.get("uri")
    headers.update(overrides)
    return {k: v for k, v in headers.items() if v is not None}


def post(httpd, body, headers, path="/mcp"):
    url = urllib.parse.urlsplit(httpd.url)
    conn = http.client.HTTPConnection(url.hostname, url.port, timeout=TIMEOUT)
    try:
        conn.request("POST", path, body=json.dumps(body), headers=headers)
        response = conn.getresponse()
        raw = response.read()
        return response.status, dict(response.getheaders()), raw
    finally:
        conn.close()


def post_modern(httpd, message, **overrides):
    status, headers, raw = post(httpd, message, headers_for(message, **overrides))
    return status, headers, (json.loads(raw) if raw and headers.get("Content-Type", "")
                             .startswith("application/json") else raw)


def test_a_modern_request_needs_no_session(httpd):
    status, headers, body = post_modern(httpd, modern("tools/list"))
    assert status == 200
    assert "Mcp-Session-Id" not in headers
    assert body["result"]["resultType"] == "complete"
    # --no-auth on loopback: nothing sits between, anyone may cache.
    assert body["result"]["cacheScope"] == "public"


def test_discover_over_http(httpd):
    status, _, body = post_modern(httpd, modern("server/discover"))
    assert status == 200 and body["result"]["supportedVersions"] == [VERSION]


def test_tools_call_with_its_name_header(httpd):
    message = modern("tools/call", {"name": "list_themes", "arguments": {}})
    status, _, body = post_modern(httpd, message)
    assert status == 200 and body["result"]["isError"] is False


def test_a_base64_name_header_is_decoded(httpd):
    message = modern("resources/read", {"uri": "skill://hr/onboarding"})
    encoded = "=?base64?" + base64.b64encode(b"skill://hr/onboarding").decode() + "?="
    status, _, body = post_modern(httpd, message, **{"Mcp-Name": encoded})
    assert status == 200 and body["result"]["contents"][0]["uri"] == "skill://hr/onboarding"


@pytest.mark.parametrize("overrides, header", [
    ({"MCP-Protocol-Version": None}, "MCP-Protocol-Version"),
    ({"MCP-Protocol-Version": "2025-11-25"}, "MCP-Protocol-Version"),
    ({"Mcp-Method": None}, "Mcp-Method"),
    ({"Mcp-Method": "tools/list"}, "Mcp-Method"),
    ({"Mcp-Name": None}, "Mcp-Name"),
    ({"Mcp-Name": "get_skill"}, "Mcp-Name"),
    ({"Mcp-Name": "=?base64?!!notb64?="}, "Mcp-Name"),
])
def test_headers_that_disagree_with_the_body_are_refused(httpd, overrides, header):
    message = modern("tools/call", {"name": "list_themes", "arguments": {}})
    status, _, body = post_modern(httpd, message, **overrides)
    assert status == 400
    assert body["error"]["code"] == -32020 and header in body["error"]["message"]
    assert body["id"] == 1


def test_an_unknown_method_is_404_with_a_json_rpc_body(httpd):
    status, _, body = post_modern(httpd, modern("nope/nothing"))
    assert status == 404 and body["error"]["code"] == -32601


def test_an_unsupported_version_is_400_naming_the_served_ones(httpd):
    message = modern("tools/list", version="2099-01-01")
    status, _, body = post_modern(httpd, message, **{"MCP-Protocol-Version": "2099-01-01"})
    assert status == 400
    assert body["error"]["code"] == -32022 and body["error"]["data"]["supported"] == [VERSION]


def test_a_missing_envelope_key_is_400(httpd):
    message = modern("tools/list")
    del message["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
    status, _, body = post_modern(httpd, message)
    assert status == 400 and body["error"]["code"] == -32602


def test_a_modern_batch_is_refused(httpd):
    batch = [modern("tools/list", msg_id=1), modern("tools/list", msg_id=2)]
    status, _, raw = post(httpd, batch, headers_for(batch[0]))
    assert status == 400 and json.loads(raw)["error"]["code"] == -32600


def test_a_modern_version_header_without_an_envelope_is_a_mismatch(httpd):
    message = {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}}
    status, _, raw = post(httpd, message, headers_for(message))
    assert status == 400 and json.loads(raw)["error"]["code"] == -32020


def test_sse_is_answered_as_one_event_when_preferred(httpd):
    status, headers, raw = post_modern(httpd, modern("tools/list"),
                                       Accept="text/event-stream, application/json;q=0.5")
    assert status == 200 and headers["Content-Type"] == "text/event-stream"
    assert headers.get("X-Accel-Buffering") == "no"
    data = [line[6:] for line in raw.decode().splitlines() if line.startswith("data: ")]
    assert json.loads("".join(data))["result"]["resultType"] == "complete"


def test_a_legacy_session_still_works_and_negotiates(httpd):
    init = {**INIT, "params": {**INIT["params"], "protocolVersion": "2025-11-25"}}
    status, headers, raw = post(httpd, init, {"Content-Type": "application/json",
                                              "Accept": "application/json, text/event-stream"})
    assert status == 200 and headers.get("Mcp-Session-Id")
    assert json.loads(raw)["result"]["protocolVersion"] == "2025-11-25"


def test_with_auth_lists_are_private_and_the_token_is_required(hub, tmp_path):
    provider = make_provider(tmp_path / "oauth" / "store.json")
    server, thread = start(hub, provider)
    try:
        message = modern("tools/list")
        status, _, _ = post_modern(server, message)
        assert status == 401
        token = service_token(server)
        status, _, body = post_modern(server, message, Authorization=f"Bearer {token}")
        assert status == 200 and body["result"]["cacheScope"] == "private"
    finally:
        server.stop()
        thread.join(TIMEOUT)


# --- header values ---------------------------------------------------------------------

@pytest.mark.parametrize("value, decoded", [
    ("us-west1", "us-west1"),
    ("=?base64?SGVsbG8sIOS4lueVjA==?=", "Hello, 世界"),
    ("=?base64?IHBhZGRlZCA=?=", " padded "),
    ("=?base64?PT9iYXNlNjQ/bGl0ZXJhbD89?=", "=?base64?literal?="),
    ("=?base64?not base64?=", None),
    ("café", None),
    (None, None),
])
def test_decode_value(value, decoded):
    assert decode_value(value) == decoded


def test_a_repeated_header_is_a_mismatch():
    message = modern("tools/list")
    values = {"mcp-protocol-version": [VERSION, VERSION], "mcp-method": ["tools/list"]}
    with pytest.raises(McpError, match="more than once"):
        check(message, VERSION, lambda name: values.get(name.lower()))

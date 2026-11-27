"""Multi round-trip requests: input_required, the retry, and the sealed state."""

from __future__ import annotations

import base64
import json

import pytest

from _mcp_http_helpers import isolate_env
from nuvel_mcp.auth import current_principal
from nuvel_mcp.protocol import envelope, mrtr
from nuvel_mcp.protocol.errors import McpError
from nuvel_mcp.server import SkillsMCPServer
from test_protocol_modern import modern

ELICITATION = {"elicitation": {"form": {}}}
ASK = {"name": {"method": "elicitation/create", "params": {
    "mode": "form", "message": "Your name?",
    "requestedSchema": {"type": "object", "properties": {"name": {"type": "string"}}}}}}


def _ask_tool(args):
    """Asks for a name, then greets it with the state it sealed."""
    context = envelope.current_request.get()
    if context is None or "name" not in context.input_responses:
        raise mrtr.InputRequired(ASK, state={"step": 1})
    return {"hello": context.input_responses["name"]["content"]["name"], "state": context.state}


@pytest.fixture
def server(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    monkeypatch.delenv(mrtr.KEY_ENV, raising=False)
    mcp = SkillsMCPServer(None)
    mcp._tools["ask"] = _ask_tool
    return mcp


def _call(arguments=None, msg_id=1, caps=ELICITATION, **extra):
    return modern("tools/call", {"name": "ask", "arguments": arguments or {}, **extra},
                  msg_id=msg_id, caps=caps)


def _first_round(server, arguments=None):
    return server.dispatch(_call(arguments))["result"]


def test_input_required_carries_the_requests_and_a_sealed_state(server):
    result = _first_round(server)
    assert result["resultType"] == "input_required"
    assert result["inputRequests"] == ASK
    assert result["requestState"].startswith("v1.")
    assert "ttlMs" not in result  # only a complete result is cacheable


def test_the_retry_gets_the_answers_and_the_state(server):
    state = _first_round(server)["requestState"]
    answers = {"name": {"action": "accept", "content": {"name": "Ada"}}}
    response = server.dispatch(_call(msg_id=2, inputResponses=answers, requestState=state))
    result = response["result"]
    assert result["resultType"] == "complete"
    assert json.loads(result["content"][0]["text"]) == {"hello": "Ada", "state": {"step": 1}}


def test_an_undeclared_capability_is_refused_with_what_is_missing(server):
    error = server.dispatch(_call(caps={}))["error"]
    assert error["code"] == -32021
    assert error["data"] == {"requiredCapabilities": {"elicitation": {}}}


def test_url_elicitation_needs_the_url_capability(server):
    context = envelope.RequestContext(version="2026-07-28", capabilities=ELICITATION)
    request = {"u": {"method": "elicitation/create", "params": {"mode": "url", "url": "https://x"}}}
    with pytest.raises(McpError) as exc:
        mrtr.check_capabilities(context, request)
    assert exc.value.data == {"requiredCapabilities": {"elicitation": {"url": {}}}}


def _retry_error(server, state, arguments=None):
    answers = {"name": {"action": "accept", "content": {"name": "Ada"}}}
    return server.dispatch(_call(arguments, msg_id=2, inputResponses=answers,
                                 requestState=state))["error"]


def test_a_tampered_state_is_refused(server):
    version, body, mac = _first_round(server)["requestState"].split(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["s"] = {"step": 99}
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    error = _retry_error(server, f"{version}.{forged}.{mac}")
    assert error == {"code": -32602, "message": "Invalid or expired requestState"}


def test_a_state_is_bound_to_its_call(server):
    state = _first_round(server, {"topic": "a"})["requestState"]
    assert _retry_error(server, state, {"topic": "b"})["code"] == -32602


def test_a_state_is_bound_to_its_principal(server):
    reset = current_principal.set("tenant-a")
    try:
        state = _first_round(server)["requestState"]
    finally:
        current_principal.reset(reset)
    reset = current_principal.set("tenant-b")
    try:
        assert _retry_error(server, state)["code"] == -32602
    finally:
        current_principal.reset(reset)


@pytest.mark.parametrize("state", ["", "v1.", "v2.abc.def", "x" * 20000, 42, "v1.!!.??"])
def test_garbage_state_is_refused_with_the_same_text(server, state):
    assert _retry_error(server, state) == {"code": -32602,
                                           "message": "Invalid or expired requestState"}


def test_a_legacy_session_cannot_be_asked_for_input(server):
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "ask", "arguments": {}}}
    error = server.dispatch(message)["error"]
    assert error["code"] == -32600 and "2026-07-28" in error["message"]


def test_input_required_is_refused_outside_the_three_methods(server):
    server._methods["tools/list"] = lambda params: (_ for _ in ()).throw(
        mrtr.InputRequired(ASK))
    assert server.dispatch(modern("tools/list", caps=ELICITATION))["error"] == {
        "code": -32603, "message": "Internal error"}


@pytest.mark.parametrize("needed", [
    mrtr.InputRequired({"x": {"method": "sampling/nope"}}),
    mrtr.InputRequired(ASK, state={"big": "y" * 5000}),
    mrtr.InputRequired(ASK, state={"not": {"json", "able"}}),
])
def test_a_request_that_cannot_be_sent_is_an_internal_error(server, needed):
    def tool(args):
        raise needed
    server._tools["ask"] = tool
    assert server.dispatch(_call())["error"] == {"code": -32603, "message": "Internal error"}


# --- the sealer ----------------------------------------------------------------------

def _seal(sealer, now=1000.0, **binding):
    return sealer.seal(principal=binding.get("principal"), method="tools/call",
                       params={"name": "t"}, state={"k": 1}, ttl=60, now=now)


def test_a_state_expires():
    sealer = mrtr.StateSealer([b"k" * 32])
    token = _seal(sealer)
    assert sealer.open(token, principal=None, method="tools/call", params={"name": "t"},
                       now=1059.0) == {"k": 1}
    with pytest.raises(McpError):
        sealer.open(token, principal=None, method="tools/call", params={"name": "t"}, now=1061.0)


def test_rotation_opens_with_any_key_and_seals_with_the_first():
    old, new = b"o" * 32, b"n" * 32
    token = _seal(mrtr.StateSealer([old]))
    rotated = mrtr.StateSealer([new, old])
    assert rotated.open(token, principal=None, method="tools/call", params={"name": "t"},
                        now=1000.0) == {"k": 1}
    with pytest.raises(McpError):
        mrtr.StateSealer([new]).open(token, principal=None, method="tools/call",
                                     params={"name": "t"}, now=1000.0)


def test_the_envelope_and_round_fields_are_not_part_of_the_binding():
    plain = {"name": "t", "arguments": {"a": 1}}
    retried = {**plain, "_meta": {"x": 1}, "inputResponses": {}, "requestState": "s"}
    assert mrtr.call_digest("tools/call", plain) == mrtr.call_digest("tools/call", retried)


def test_keys_come_from_the_environment(monkeypatch):
    key = base64.b64encode(b"s" * 32).decode()
    monkeypatch.setenv(mrtr.KEY_ENV, key)
    sealer = mrtr.StateSealer.from_env()
    assert not sealer.ephemeral
    token = _seal(sealer)
    monkeypatch.setenv(mrtr.KEY_ENV, f"{base64.b64encode(b't' * 32).decode()},{key}")
    assert mrtr.StateSealer.from_env().open(token, principal=None, method="tools/call",
                                            params={"name": "t"}, now=1000.0) == {"k": 1}


@pytest.mark.parametrize("value", [base64.b64encode(b"short").decode(), "not base64!!"])
def test_a_bad_key_stops_the_server(monkeypatch, value):
    monkeypatch.setenv(mrtr.KEY_ENV, value)
    with pytest.raises(ValueError, match=mrtr.KEY_ENV):
        mrtr.StateSealer.from_env()


def test_without_a_key_the_sealer_is_ephemeral(monkeypatch):
    monkeypatch.delenv(mrtr.KEY_ENV, raising=False)
    assert mrtr.StateSealer.from_env().ephemeral


def test_a_large_state_is_refused_at_seal_time():
    with pytest.raises(ValueError):
        mrtr.StateSealer([b"k" * 32]).seal(principal=None, method="tools/call", params={},
                                           state={"x": "y" * 5000})

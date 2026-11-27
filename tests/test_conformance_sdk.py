"""The official MCP Python SDK as an independent check of the wire format.

These run only with the ``conformance`` extra installed (``pip install -e
".[conformance]"``); the runtime itself never imports ``mcp``. Each test
drives a real nuvel-mcp server (a subprocess over stdio, or a bound HTTP
server) with the SDK's ``Client`` in each era: ``auto`` probes
``server/discover`` and stays modern, ``legacy`` opens with ``initialize``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from _mcp_http_helpers import TIMEOUT, isolate_env, make_hub, start
from nuvel_mcp.protocol import envelope, mrtr

pytest.importorskip("mcp", reason="needs the conformance extra")
try:  # the v2 client; v1 has no Client(mode=...)
    from mcp import Client, StdioServerParameters
except ImportError:  # pragma: no cover
    pytest.skip("needs mcp>=2.3 (the conformance extra)", allow_module_level=True)
from mcp import types  # noqa: E402

MODES = ["auto", "2026-07-28", "legacy"]


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


async def _exercise(target, mode: str) -> dict:
    async with Client(target, mode=mode, read_timeout_seconds=TIMEOUT) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        read = await client.read_resource("skill://hr/onboarding/SKILL.md")
        call = await client.call_tool("search_skills", {"query": "rollback"})
        return {
            "version": client.protocol_version,
            "server": client.server_info.name if client.server_info else None,
            "tools": sorted(tool.name for tool in tools.tools),
            "resources": sorted(str(r.uri) for r in resources.resources),
            "read": read.contents[0].text,
            "call_error": call.is_error,
            "found": json.loads(call.content[0].text)["results"][0]["name"],
        }


def _check(seen: dict, mode: str) -> None:
    expected_version = "2025-11-25" if mode == "legacy" else "2026-07-28"
    assert seen["version"] == expected_version
    # A pinned version skips the server/discover probe, so the client never
    # learns the server's name; auto and legacy do.
    if mode != "2026-07-28":
        assert seen["server"] == "nuvel-skills"
    assert "search_skills" in seen["tools"]
    assert "skill://hr/onboarding/SKILL.md" in seen["resources"]
    assert "onboarding" in seen["read"].lower()
    assert seen["call_error"] is False and seen["found"] == "rollback"


@pytest.mark.parametrize("mode", MODES)
def test_sdk_client_over_stdio(hub, mode):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "nuvel_mcp", "serve", "--skills-dir", str(hub)],
        env={"NUVEL_FEEDBACK_DIR": os.environ["NUVEL_FEEDBACK_DIR"]})
    _check(asyncio.run(_exercise(params, mode)), mode)


@pytest.mark.parametrize("mode", MODES)
def test_sdk_client_over_http(httpd, mode):
    _check(asyncio.run(_exercise(httpd.url + "/mcp", mode)), mode)


def _ask_tool(args):
    context = envelope.current_request.get()
    if context is None or "who" not in context.input_responses:
        raise mrtr.InputRequired({"who": {"method": "elicitation/create", "params": {
            "mode": "form", "message": "Who is asking?",
            "requestedSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                                "required": ["name"]}}}}, state={"asked": True})
    answer = context.input_responses["who"]
    return {"hello": answer["content"]["name"], "state": context.state}


def test_sdk_client_completes_a_multi_round_trip_call(httpd):
    httpd.mcp._tools["ask"] = _ask_tool
    httpd.mcp._allowed_tools = httpd.mcp._allowed_tools | {"ask"}  # remote mode allow-list
    prompts: list[str] = []

    async def on_elicit(context, params: types.ElicitRequestParams):
        prompts.append(params.message)
        return types.ElicitResult(action="accept", content={"name": "Ada"})

    async def run():
        async with Client(httpd.url + "/mcp", mode="2026-07-28", elicitation_callback=on_elicit,
                          read_timeout_seconds=TIMEOUT) as client:
            return await client.call_tool("ask", {})

    result = asyncio.run(run())
    assert prompts == ["Who is asking?"]
    assert json.loads(result.content[0].text) == {"hello": "Ada", "state": {"asked": True}}


def test_sdk_reads_every_skill_file_back_to_its_manifest_digest(httpd):
    """skills/list over raw HTTP (the SDK has no Skills client), then every
    listed file through the SDK's resources/read, checked against the digest."""
    import base64
    import hashlib

    from test_mcp_http_modern import post_modern
    from test_protocol_modern import modern

    status, _, body = post_modern(httpd, modern("skills/list"))
    assert status == 200
    resources = [r for skill in body["result"]["skills"] for r in skill["resources"]]
    assert resources

    async def read_all():
        async with Client(httpd.url + "/mcp", mode="2026-07-28",
                          read_timeout_seconds=TIMEOUT) as client:
            return [await client.read_resource(r["uri"]) for r in resources]

    for resource, read in zip(resources, asyncio.run(read_all())):
        content = read.contents[0]
        data = (content.text.encode("utf-8") if hasattr(content, "text")
                else base64.b64decode(content.blob))
        assert "sha256:" + hashlib.sha256(data).hexdigest() == resource["digest"]
        assert len(data) == resource["size"]

"""The request headers Streamable HTTP mirrors from the body (2026-07-28).

``MCP-Protocol-Version`` and ``Mcp-Method`` on every request, ``Mcp-Name`` on
the methods that name a tool, prompt or resource. A server that reads the
body must check each against it: a load balancer may have routed on the
header, and the two must never disagree.
"""

from __future__ import annotations

import base64
import binascii

from nuvel_mcp.protocol.errors import HEADER_MISMATCH, McpError

PROTOCOL_VERSION = "MCP-Protocol-Version"
METHOD = "Mcp-Method"
NAME = "Mcp-Name"
# The body param each name-bearing method mirrors into Mcp-Name.
NAME_PARAMS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
_SENTINEL_START, _SENTINEL_END = "=?base64?", "?="


def decode_value(value: str | None) -> str | None:
    """A header value as the client meant it: the Base64 sentinel form
    (``=?base64?...?=``) decoded, a plain value checked to be visible ASCII.
    ``None`` for an absent or malformed value."""
    if value is None:
        return None
    if (value.startswith(_SENTINEL_START) and value.endswith(_SENTINEL_END)
            and len(value) >= len(_SENTINEL_START) + len(_SENTINEL_END)):
        encoded = value[len(_SENTINEL_START):-len(_SENTINEL_END)]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            return None
    if all(0x20 <= ord(c) <= 0x7E or c == "\t" for c in value):
        return value
    return None


def check(message: dict, version: str, get_all) -> None:
    """Raise ``HEADER_MISMATCH`` unless the headers agree with the body.

    ``get_all(name)`` returns every value of a header (case-insensitive). The
    messages name the header at fault, never a value.
    """
    def single(name: str) -> str | None:
        values = get_all(name) or []
        if len(values) > 1:
            raise McpError(HEADER_MISMATCH, f"Header mismatch: {name} appears more than once")
        return values[0] if values else None

    if single(PROTOCOL_VERSION) != version:
        raise McpError(HEADER_MISMATCH,
                       f"Header mismatch: {PROTOCOL_VERSION} does not match the request's _meta")
    method = message.get("method")
    if single(METHOD) != method:
        raise McpError(HEADER_MISMATCH, f"Header mismatch: {METHOD} does not match the method")
    key = NAME_PARAMS.get(method)
    if key is None:
        return
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    body_value = params.get(key)
    if body_value is None:
        return  # the dispatcher refuses the missing param itself
    if decode_value(single(NAME)) != body_value:
        raise McpError(HEADER_MISMATCH,
                       f"Header mismatch: {NAME} does not match the request's '{key}'")

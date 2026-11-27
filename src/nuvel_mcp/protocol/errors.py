"""JSON-RPC error codes, the spec's and ours, and the HTTP status of each.

The 2026-07-28 revision splits ``-32000`` to ``-32099``: ``-32000`` to
``-32019`` are implementation-defined (ours live there, see the transport and
OAuth modules) and ``-32020`` onwards belong to the specification.
"""

from __future__ import annotations

from http import HTTPStatus

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Defined by the 2026-07-28 specification.
HEADER_MISMATCH = -32020
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# The status a modern Streamable HTTP reply carries for each error code;
# anything else is 200 with the error in the body.
HTTP_STATUS = {
    PARSE_ERROR: HTTPStatus.BAD_REQUEST,
    INVALID_REQUEST: HTTPStatus.BAD_REQUEST,
    INVALID_PARAMS: HTTPStatus.BAD_REQUEST,
    HEADER_MISMATCH: HTTPStatus.BAD_REQUEST,
    MISSING_REQUIRED_CLIENT_CAPABILITY: HTTPStatus.BAD_REQUEST,
    UNSUPPORTED_PROTOCOL_VERSION: HTTPStatus.BAD_REQUEST,
    METHOD_NOT_FOUND: HTTPStatus.NOT_FOUND,
}


class McpError(Exception):
    """A JSON-RPC error a handler raises. ``message`` is fixed text: it never
    carries a value the caller sent."""

    def __init__(self, code: int, message: str, data: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def error_response(msg_id, code: int, message: str, data: dict | None = None) -> dict:
    error: dict = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}

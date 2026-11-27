"""The 2026-07-28 per-request envelope, read once into a :class:`RequestContext`.

Every modern request carries in ``params._meta`` its protocol version and
client capabilities (both required) and, optionally, the client's identity, a
log level, a progress token and W3C trace keys. Handlers read the context
through :data:`current_request`, never the raw ``_meta``.
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field

from nuvel_mcp.protocol import versions
from nuvel_mcp.protocol.errors import INVALID_PARAMS, UNSUPPORTED_PROTOCOL_VERSION, McpError

PREFIX = "io.modelcontextprotocol/"
PROTOCOL_VERSION = PREFIX + "protocolVersion"
CLIENT_CAPABILITIES = PREFIX + "clientCapabilities"
CLIENT_INFO = PREFIX + "clientInfo"
LOG_LEVEL = PREFIX + "logLevel"
SERVER_INFO = PREFIX + "serverInfo"

# W3C Trace Context (SEP-414). Only a well-formed traceparent is kept; the
# baggage and tracestate are never read, since they can carry caller text.
_TRACEPARENT = re.compile(r"00-(?!0{32})[0-9a-f]{32}-(?!0{16})[0-9a-f]{16}-[0-9a-f]{2}")
# What an unsupported-version error may repeat back: a version-shaped token.
_VERSION_TOKEN = re.compile(r"[0-9A-Za-z._-]{1,32}")


@dataclass(frozen=True)
class RequestContext:
    """What one modern request declared about itself."""

    version: str
    capabilities: dict
    client_info: dict | None = None
    log_level: str | None = None
    traceparent: str | None = None
    # Set by the kernel on the retry of a multi round-trip request: the
    # client's answers, keyed like the server's inputRequests, and the state
    # the server sealed into requestState (already verified).
    input_responses: dict = field(default_factory=dict)
    state: dict | None = None

    @property
    def extensions(self) -> dict:
        value = self.capabilities.get("extensions")
        return value if isinstance(value, dict) else {}

    def has_extension(self, identifier: str) -> bool:
        return isinstance(self.extensions.get(identifier), dict)


# The context of the modern request being handled on this thread; None for a
# legacy request (its capabilities came with initialize).
current_request: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar(
    "nuvel_mcp_current_request", default=None)


def meta_of(message: object) -> dict | None:
    params = message.get("params") if isinstance(message, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    return meta if isinstance(meta, dict) else None


def is_modern(message: object) -> bool:
    """True for a request on the stateless path: it names a protocol version in
    its envelope. ``initialize`` never is, even with an envelope stamped on it:
    the handshake does not exist in modern revisions."""
    meta = meta_of(message)
    return (meta is not None and PROTOCOL_VERSION in meta
            and isinstance(message, dict) and message.get("method") != "initialize")


def read(message: dict) -> RequestContext:
    """The envelope's shape, checked: raise ``INVALID_PARAMS`` naming the key
    at fault. The version is not checked against what we serve here, so a
    transport can compare its headers first (see :func:`require_supported`)."""
    meta = meta_of(message)
    if meta is None:
        raise McpError(INVALID_PARAMS, "params._meta must be an object carrying the request envelope")
    missing = [key for key in (PROTOCOL_VERSION, CLIENT_CAPABILITIES) if key not in meta]
    if missing:
        raise McpError(INVALID_PARAMS,
                       "params._meta is missing the required key(s): " + ", ".join(missing))
    version = meta[PROTOCOL_VERSION]
    if not isinstance(version, str):
        raise McpError(INVALID_PARAMS, f"params._meta.{PROTOCOL_VERSION} must be a string")
    capabilities = meta[CLIENT_CAPABILITIES]
    if not isinstance(capabilities, dict):
        raise McpError(INVALID_PARAMS, f"params._meta.{CLIENT_CAPABILITIES} must be an object")
    info = meta.get(CLIENT_INFO)
    level = meta.get(LOG_LEVEL)
    traceparent = meta.get("traceparent")
    return RequestContext(
        version=version,
        capabilities=capabilities,
        client_info=info if isinstance(info, dict) else None,
        log_level=level if isinstance(level, str) else None,
        traceparent=traceparent if isinstance(traceparent, str)
        and _TRACEPARENT.fullmatch(traceparent) else None,
    )


def require_supported(context: RequestContext) -> None:
    """Raise ``UNSUPPORTED_PROTOCOL_VERSION`` unless we serve the version."""
    if context.version in versions.MODERN:
        return
    requested = context.version if _VERSION_TOKEN.fullmatch(context.version) else ""
    raise McpError(UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                   {"supported": list(versions.MODERN), "requested": requested})


def client_capability(context: RequestContext, path: tuple[str, ...]) -> bool:
    """True when the client declared the capability at *path* (an object)."""
    node: object = context.capabilities
    for key in path:
        if not isinstance(node, dict) or not isinstance(node.get(key), dict):
            return False
        node = node[key]
    return True

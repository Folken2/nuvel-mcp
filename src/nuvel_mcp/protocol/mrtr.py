"""Multi round-trip requests (SEP-2322): ``input_required`` and ``requestState``.

A modern server never sends the client a request of its own. A handler that
needs input (a form the user fills in, a URL they visit) raises
:class:`InputRequired`; the client gathers the answers and retries the call
with a new id, ``inputResponses`` keyed like the requests, and the
``requestState`` echoed back.

``requestState`` is the only memory between the rounds, and it comes back
from the client, so it is sealed: an HMAC-SHA256 over a payload binding the
principal, the method, a digest of the call and an expiry. A state that was
altered, expired, or comes back with another principal or another call is
refused before the handler sees it. The payload is signed, not encrypted: a
handler's state holds identifiers, never user content.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time

from nuvel_mcp.protocol import envelope
from nuvel_mcp.protocol.errors import INVALID_PARAMS, MISSING_REQUIRED_CLIENT_CAPABILITY, McpError

KEY_ENV = "NUVEL_MCP_STATE_KEY"
MIN_KEY_BYTES = 32
DEFAULT_TTL_SECONDS = 600
MAX_STATE_BYTES = 4096
MAX_TOKEN_CHARS = 16384
_VERSION = "v1"
# The only methods whose result may be input_required (SEP-2322).
METHODS = frozenset({"tools/call", "prompts/get", "resources/read"})
# The params a retry adds; left out of the call digest so both rounds match.
_ROUND_PARAMS = ("_meta", "inputResponses", "requestState")
_INVALID_STATE = "Invalid or expired requestState"


class InputRequired(Exception):
    """Raised by a handler to ask the client for input before it can finish.

    ``requests`` maps a key of the handler's choice to an input request
    (``{"method": "elicitation/create", "params": {...}}``); ``state`` is what
    the handler wants back on the retry, read there as
    ``current_request.get().state``.
    """

    def __init__(self, requests: dict | None = None, state: dict | None = None):
        super().__init__("input required")
        if not requests and state is None:
            raise ValueError("InputRequired needs input requests, state, or both")
        self.requests = requests or {}
        self.state = state


def required_capability(request: dict) -> tuple[str, ...]:
    """The client capability an input request needs, as a path."""
    method = request.get("method")
    if method == "elicitation/create":
        params = request.get("params") or {}
        return ("elicitation", "url") if params.get("mode") == "url" else ("elicitation",)
    if method == "sampling/createMessage":
        return ("sampling",)
    if method == "roots/list":
        return ("roots",)
    raise ValueError(f"not an input request method: {method!r}")


def check_capabilities(context: envelope.RequestContext, requests: dict) -> None:
    """Refuse (``-32021``) an input request of a kind the client did not declare."""
    missing: dict = {}
    for request in requests.values():
        path = required_capability(request)
        if not envelope.client_capability(context, path):
            node = missing
            for key in path:
                node = node.setdefault(key, {})
    if missing:
        raise McpError(MISSING_REQUIRED_CLIENT_CAPABILITY,
                       "This request needs a client capability that was not declared",
                       {"requiredCapabilities": missing})


def call_digest(method: str, params: dict) -> str:
    """What binds a requestState to one call: the method and its params, less
    the envelope and the round-trip fields."""
    body = {k: v for k, v in (params or {}).items() if k not in _ROUND_PARAMS}
    canonical = json.dumps({"method": method, "params": body}, sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _decode_key(text: str) -> bytes:
    text = text.strip()
    try:
        key = _unb64(text.replace("+", "-").replace("/", "_").rstrip("="))
    except (binascii.Error, ValueError):
        raise ValueError(f"{KEY_ENV}: a key is not valid base64") from None
    if len(key) < MIN_KEY_BYTES:
        raise ValueError(f"{KEY_ENV}: each key must be at least {MIN_KEY_BYTES} bytes")
    return key


class StateSealer:
    """Seals and opens ``requestState``. The first key signs; any key opens,
    so a key is rotated by putting the new one first and dropping the old
    one once the longest TTL has passed."""

    def __init__(self, keys: list[bytes] | None = None):
        self.ephemeral = not keys
        self._keys = list(keys) if keys else [secrets.token_bytes(MIN_KEY_BYTES)]

    @classmethod
    def from_env(cls) -> "StateSealer":
        """Keys from ``NUVEL_MCP_STATE_KEY`` (comma-separated, base64); a random
        per-process key without it. Raises ``ValueError`` on a bad key."""
        raw = os.environ.get(KEY_ENV, "").strip()
        if not raw:
            return cls()
        return cls([_decode_key(part) for part in raw.split(",") if part.strip()])

    @staticmethod
    def _mac(key: bytes, signed: str) -> bytes:
        return hmac.new(key, signed.encode("ascii"), hashlib.sha256).digest()

    def seal(self, *, principal, method: str, params: dict, state: dict | None,
             ttl: int = DEFAULT_TTL_SECONDS, now: float | None = None) -> str:
        payload = json.dumps({
            "p": principal, "m": method, "d": call_digest(method, params),
            "e": int((now if now is not None else time.time()) + ttl), "s": state,
        }, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_STATE_BYTES:
            raise ValueError("requestState payload is too large")
        signed = f"{_VERSION}.{_b64(payload)}"
        return f"{signed}.{_b64(self._mac(self._keys[0], signed))}"

    def open(self, token: object, *, principal, method: str, params: dict,
             now: float | None = None) -> dict | None:
        """The handler's state from a retry, or ``INVALID_PARAMS`` with one
        fixed message whatever was wrong (forged, expired, another caller's)."""
        if not isinstance(token, str) or len(token) > MAX_TOKEN_CHARS:
            raise McpError(INVALID_PARAMS, _INVALID_STATE)
        version, _, rest = token.partition(".")
        body, _, mac = rest.partition(".")
        if version != _VERSION or not body or not mac:
            raise McpError(INVALID_PARAMS, _INVALID_STATE)
        signed = f"{version}.{body}"
        try:
            presented = _unb64(mac)
            if not any(hmac.compare_digest(presented, self._mac(key, signed)) for key in self._keys):
                raise McpError(INVALID_PARAMS, _INVALID_STATE)
            payload = json.loads(_unb64(body).decode("utf-8"))
        except (binascii.Error, ValueError, UnicodeDecodeError):
            raise McpError(INVALID_PARAMS, _INVALID_STATE) from None
        if not isinstance(payload, dict) or not isinstance(payload.get("e"), int):
            raise McpError(INVALID_PARAMS, _INVALID_STATE)
        expired = payload.get("e", 0) < (now if now is not None else time.time())
        if (expired or payload.get("p") != principal or payload.get("m") != method
                or not hmac.compare_digest(str(payload.get("d")), call_digest(method, params))):
            raise McpError(INVALID_PARAMS, _INVALID_STATE)
        return payload.get("s")

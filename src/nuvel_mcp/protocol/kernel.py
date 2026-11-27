"""What the dispatcher adds to a modern answer, and ``server/discover``.

A modern result always says what it is (``resultType``), carries the server's
identity in ``_meta``, and, for the discovery, list and read methods, how long
it may be cached and by whom (``ttlMs``, ``cacheScope``).
"""

from __future__ import annotations

from nuvel_mcp.protocol import envelope, versions

# Methods whose result is a CacheableResult in 2026-07-28, with their TTL.
CACHE_TTL_MS = {
    "server/discover": 300_000,
    "tools/list": 300_000,
    "resources/list": 300_000,
    "resources/templates/list": 300_000,
    "prompts/list": 300_000,
    "resources/read": 300_000,
}
# Legacy methods the 2026-07-28 revision removed: on the modern path they are
# unknown methods.
REMOVED = frozenset({
    "initialize", "ping", "logging/setLevel", "resources/subscribe", "resources/unsubscribe",
})
CACHE_SCOPES = ("private", "public")


def finish(method: str, result: dict, server_info: dict, cache_scope: str) -> dict:
    """Stamp a modern result in place and return it."""
    result.setdefault("resultType", "complete")
    if method in CACHE_TTL_MS and result["resultType"] == "complete":
        result.setdefault("ttlMs", CACHE_TTL_MS[method])
        result.setdefault("cacheScope", cache_scope)
    meta = result.get("_meta")
    result["_meta"] = {**(meta if isinstance(meta, dict) else {}),
                       envelope.SERVER_INFO: server_info}
    return result


def input_required(requests: dict, request_state: str | None) -> dict:
    result: dict = {"resultType": "input_required"}
    if requests:
        result["inputRequests"] = requests
    if request_state is not None:
        result["requestState"] = request_state
    return result


def discover(capabilities: dict, instructions: str | None = None) -> dict:
    result: dict = {"supportedVersions": list(versions.MODERN), "capabilities": capabilities}
    if instructions:
        result["instructions"] = instructions
    return result

"""Protocol revisions this server speaks, and how a request picks one.

A request that carries the per-request ``_meta`` envelope is *modern* and
names its own revision; an ``initialize`` handshake is *legacy* and negotiates
one for its session (stdio process or HTTP session).
"""

from __future__ import annotations

# Revisions served statelessly through the per-request envelope.
MODERN = ("2026-07-28",)
# Revisions reachable through the initialize handshake, oldest first.
LEGACY = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_MODERN = MODERN[-1]
LATEST_LEGACY = LEGACY[-1]


def negotiate_legacy(requested: object) -> str:
    """The version an ``initialize`` answer names: the client's own when we
    speak it, else our latest legacy one (the client then decides whether it
    can continue)."""
    return requested if isinstance(requested, str) and requested in LEGACY else LATEST_LEGACY

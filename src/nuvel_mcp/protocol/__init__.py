"""The MCP protocol kernel: what every request goes through, whatever its era.

``versions``  the revisions served and how a request picks one.
``errors``    the error codes (the spec's and ours) and their HTTP statuses.
``envelope``  the 2026-07-28 per-request ``_meta`` envelope, read once into a
              :class:`~nuvel_mcp.protocol.envelope.RequestContext`.
``mrtr``      multi round-trip requests: ``input_required`` results and the
              sealed ``requestState``.
``kernel``    what the dispatcher adds to a modern result (``resultType``, the
              cache fields, ``serverInfo``) and the ``server/discover`` result.

Standard library only, like the rest of the package. Design:
``docs/design/foundation.md``.
"""

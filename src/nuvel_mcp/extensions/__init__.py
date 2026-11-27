"""MCP extensions this server offers, one module each.

An extension object has an ``identifier`` (``{vendor-prefix}/{name}``), the
``settings()`` it declares under ``capabilities.extensions`` in
``server/discover`` and ``initialize``, and the ``methods()`` it adds to the
dispatcher. Design: ``docs/design/foundation.md`` (section 6).
"""

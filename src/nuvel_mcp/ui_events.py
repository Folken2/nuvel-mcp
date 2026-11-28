"""Page-visit UI events (``nuvel.ui_event/1`` and ``/2``) for the Nuvel Skills MCP server.

Stdlib-only. One closed, content-free JSON object per page visit on a plugin
surface (the chief-of-staff dashboard) or on one of this server's own views
(the skill browser, version 2 only): which catalogued events happened and how
often, how long the visit lasted and how long until the first action. Each
version's shape lives in a packaged JSON Schema, ``schemas/ui_event.v<N>.schema.json``;
an event is checked against the version it names.

The server's own surfaces are checked further here, since their catalogue is
the server's: the plugin must be ``nuvel-mcp`` and every event id must be in
:data:`SERVER_SURFACES`.

Everything else is shared with run traces (:mod:`nuvel_mcp.traces`): the same
validator (paths only, never values), the same ``normalize`` (``started_at``
truncated to the minute in UTC, ``tenant`` stamped by the server) and the same
day-file store, filed by ``surface`` instead of skill.
"""

from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from typing import Protocol, runtime_checkable

from nuvel_mcp import traces

SCHEMAS = {
    "nuvel.ui_event/1": "schemas/ui_event.v1.schema.json",
    "nuvel.ui_event/2": "schemas/ui_event.v2.schema.json",
}
SCHEMA_ID = "nuvel.ui_event/2"  # the current version
SCHEMA_RESOURCE = SCHEMAS[SCHEMA_ID]
UI_EVENTS_DIRNAME = "_ui_events"
# The server's own views: the plugin name they record under, and each one's
# published event catalogue.
SERVER_PLUGIN = "nuvel-mcp"
SERVER_SURFACES = {
    "skill-browser": frozenset({"search", "open_skill", "use_skill", "back"}),
}


def validate_ui_event(event: object) -> list[str]:
    """Validate *event* against the version it names (paths only, no values);
    an unknown version is checked against the current one, whose ``/schema``
    then fails."""
    name = event.get("schema") if isinstance(event, dict) else None
    errors = traces.validate(event, SCHEMAS.get(name, SCHEMA_RESOURCE))
    if errors or event["surface"] not in SERVER_SURFACES:
        return errors
    if event["plugin"]["name"] != SERVER_PLUGIN:
        errors.append("/plugin/name")
    if set(event["events"]) - SERVER_SURFACES[event["surface"]]:
        errors.append("/events/*")
    return errors


@lru_cache(maxsize=None)
def surfaces() -> frozenset[str]:
    """The surfaces any version accepts (``check_skill_health`` reads these)."""
    return frozenset(surface for resource in SCHEMAS.values()
                     for surface in traces.load_schema(resource)["properties"]["surface"]["enum"])


@runtime_checkable
class UiEventStore(Protocol):
    """Where normalised UI events go."""

    def append(self, event: dict) -> bool:
        """Store a normalised event; ``False`` if ``(tenant, run_id)`` is already stored."""
        ...

    def query(self, plugin: str, surface: str, since: datetime, tenant: str | None) -> list[dict]:
        """Events for ``plugin.surface`` started at or after *since*.

        ``tenant=None`` returns every tenant's events.
        """
        ...


class LocalUiEventStore(traces.LocalTraceStore):
    """JSONL files at ``{feedback_dir}/_ui_events/{plugin}.{surface}/{YYYY-MM-DD}.jsonl``.

    The same storage, locking and idempotency as :class:`~nuvel_mcp.traces.LocalTraceStore`.
    """

    DIRNAME = UI_EVENTS_DIRNAME

    @staticmethod
    def _group(record: dict) -> object:
        return record.get("surface")

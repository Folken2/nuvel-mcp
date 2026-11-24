"""Page-visit UI events (``nuvel.ui_event/1``) for the Nuvel Skills MCP server.

Stdlib-only. One closed, content-free JSON object per page visit on a plugin
surface (today only the dashboard): which catalogued events happened and how
often, how long the visit lasted and how long until the first action. Its
shape lives in the packaged JSON Schema ``schemas/ui_event.v1.schema.json``.

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

SCHEMA_ID = "nuvel.ui_event/1"
SCHEMA_RESOURCE = "schemas/ui_event.v1.schema.json"
UI_EVENTS_DIRNAME = "_ui_events"


def validate_ui_event(event: object) -> list[str]:
    """Validate *event* against ``nuvel.ui_event/1`` (paths only, no values)."""
    return traces.validate(event, SCHEMA_RESOURCE)


@lru_cache(maxsize=None)
def surfaces() -> frozenset[str]:
    """The surfaces the schema accepts (``check_skill_health`` reads these)."""
    return frozenset(traces.load_schema(SCHEMA_RESOURCE)["properties"]["surface"]["enum"])


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

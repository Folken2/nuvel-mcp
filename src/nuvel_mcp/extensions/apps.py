"""MCP Apps (``io.modelcontextprotocol/ui``, SEP-1865): interactive views.

A view is a fixed, versioned HTML file shipped in the package
(``nuvel_mcp/apps/``), served as a ``ui://`` resource of type
``text/html;profile=mcp-app``. A tool names its view in
``_meta.ui.resourceUri``; a host that supports Apps renders the tool's result
in that view, and any other host gets the tool's ordinary text and
``structuredContent`` result, which is always complete.

Views are self-contained (inline script and style) and need no network: no
``_meta.ui.csp`` is declared, so hosts apply the restrictive default
(``connect-src 'none'``). A view reaches data only through ``tools/call``
over the host's ``postMessage`` bridge, so it never holds a token and every
call shows in the host's audit trail. No tool ever builds HTML from user
content: what a view shows is set as text by the view itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from nuvel_mcp.protocol.errors import INVALID_PARAMS, McpError

IDENTIFIER = "io.modelcontextprotocol/ui"
MIME_TYPE = "text/html;profile=mcp-app"
SCHEME = "ui://"


@dataclass(frozen=True)
class View:
    uri: str
    name: str
    description: str
    filename: str
    prefers_border: bool = True


SKILL_BROWSER = View(
    uri="ui://nuvel-skills/skill-browser",
    name="skill_browser",
    description="Search the skills hub and read a skill's SKILL.md.",
    filename="skill-browser.html",
)
VIEWS = {view.uri: view for view in (SKILL_BROWSER,)}


@lru_cache(maxsize=None)
def _html(filename: str) -> str:
    return resources.files("nuvel_mcp").joinpath("apps", filename).read_text(encoding="utf-8")


def tool_meta(view: View) -> dict:
    """The ``_meta`` linking a tool to *view* (visible to model and app)."""
    return {"ui": {"resourceUri": view.uri}}


class AppsExtension:
    identifier = IDENTIFIER

    def settings(self) -> dict:
        return {}

    def methods(self) -> dict:
        return {}

    def read(self, uri: str) -> dict:
        view = VIEWS.get(uri)
        if view is None:
            raise McpError(INVALID_PARAMS, "Resource not found")
        return {"contents": [{"uri": view.uri, "mimeType": MIME_TYPE, "text": _html(view.filename),
                              "_meta": {"ui": {"prefersBorder": view.prefers_border}}}]}

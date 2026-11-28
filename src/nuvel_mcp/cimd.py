"""OAuth Client ID Metadata Documents (draft-ietf-oauth-client-id-metadata-document-00).

A client that has never registered here names itself by an https URL as its
``client_id``; that URL serves a JSON document with its name and redirect
URIs. This module fetches and checks that document. Stdlib only.

The fetch is a request to an address a stranger chose, so it is fenced in
(the draft's section 6, and the MCP spec's Client ID Metadata Document
Security): https only; the host's every address must be public (no
loopback, private, link-local or reserved ranges), and the connection goes to
the address that was checked, so DNS can't swap in another between check and
use; no redirects; at most ``MAX_BYTES``; a short timeout. Errors carry fixed
text, never the document or the URL.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

MAX_BYTES = 5 * 1024  # the draft's suggested limit
MAX_URL = 2048
TIMEOUT_S = 5.0
DEFAULT_CACHE_S = 300
MIN_CACHE_S = 60
MAX_CACHE_S = 24 * 3600
_MAX_AGE = re.compile(r"(?:^|,)\s*max-age\s*=\s*(\d+)\s*(?:,|$)", re.I)


class CimdError(Exception):
    """The document could not be used; the message is fixed text."""


def is_client_id_url(value: object) -> bool:
    """An https URL with a path, and no query, fragment, credentials or dot
    segments: the form a metadata document's client_id takes."""
    if not isinstance(value, str) or len(value) > MAX_URL or not value.startswith("https://"):
        return False
    parts = urllib.parse.urlsplit(value)
    try:
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return False
    segments = parts.path.split("/")[1:]
    return bool(parts.hostname and parts.path not in ("", "/") and not parts.query
                and not parts.fragment and not parts.username and not parts.password
                and not any(s in (".", "..") for s in segments)
                and not any(c.isspace() or ord(c) < 0x20 for c in value))


def public_address(address: str) -> bool:
    """Globally routable unicast: not loopback, private, link-local, reserved,
    multicast or shared (and an IPv4-mapped IPv6 address judged as IPv4)."""
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


@dataclass
class Fetcher:
    """Fetches a metadata document. The hooks exist for tests, which point it
    at a local server explicitly; the defaults are the real network, public
    addresses only."""

    resolve: Callable = socket.getaddrinfo
    address_ok: Callable[[str], bool] = public_address
    ssl_context: ssl.SSLContext = field(default_factory=ssl.create_default_context)
    timeout: float = TIMEOUT_S

    def _vetted_address(self, host: str, port: int) -> str:
        try:
            infos = self.resolve(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            raise CimdError("The client's host could not be resolved") from None
        addresses = [info[4][0] for info in infos]
        if not addresses or not all(self.address_ok(a) for a in addresses):
            raise CimdError("The client's host is not a public address")
        return addresses[0]

    def fetch(self, url: str) -> tuple[dict, int]:
        """The document and how many seconds it may be cached."""
        if not is_client_id_url(url):
            raise CimdError("client_id is not a metadata document URL")
        parts = urllib.parse.urlsplit(url)
        host, port = parts.hostname, parts.port or 443
        address = self._vetted_address(host, port)
        conn = _PinnedHTTPSConnection(host, port, address, timeout=self.timeout,
                                      context=self.ssl_context)
        try:
            conn.request("GET", parts.path, headers={
                "Accept": "application/json", "User-Agent": "nuvel-mcp",
                "Host": parts.netloc})
            response = conn.getresponse()
            if response.status != 200:  # redirects included: never followed
                raise CimdError("The client's metadata document could not be fetched")
            ctype = (response.getheader("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json" and not ctype.endswith("+json"):
                raise CimdError("The client's metadata document is not JSON")
            body = response.read(MAX_BYTES + 1)
            cache_control = response.getheader("Cache-Control") or ""
        except (OSError, http.client.HTTPException, ssl.SSLError):
            raise CimdError("The client's metadata document could not be fetched") from None
        finally:
            conn.close()
        if len(body) > MAX_BYTES:
            raise CimdError("The client's metadata document is too large")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise CimdError("The client's metadata document is not valid JSON") from None
        return document, cache_seconds(cache_control)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to *address*, with the certificate checked for *host*."""

    def __init__(self, host: str, port: int, address: str, *, timeout: float,
                 context: ssl.SSLContext):
        super().__init__(host, port, timeout=timeout, context=context)
        self._address = address
        self._context = context

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def cache_seconds(cache_control: str) -> int:
    """How long to keep a document: ``no-store``/``no-cache`` the minimum,
    ``max-age`` within bounds, else the default."""
    lowered = cache_control.lower()
    if "no-store" in lowered or "no-cache" in lowered:
        return MIN_CACHE_S
    match = _MAX_AGE.search(cache_control)
    if not match:
        return DEFAULT_CACHE_S
    return max(MIN_CACHE_S, min(MAX_CACHE_S, int(match.group(1))))



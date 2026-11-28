"""OAuth Client ID Metadata Documents, application_type, and the client
credentials extension (MCP 2026-07-28 authorization)."""

from __future__ import annotations

import http.server
import json
import shutil
import socket
import ssl
import subprocess
import threading

import pytest

from _mcp_http_helpers import (
    REDIRECT,
    TIMEOUT,
    Client,
    authorize_params,
    dev_login,
    exchange,
    isolate_env,
    make_hub,
    make_provider,
    pkce,
    register,
    start,
)
from nuvel_mcp import cimd
from nuvel_mcp.oauth import AuthorizeError, consent_page
from test_mcp_http_modern import post_modern
from test_protocol_modern import modern

CLIENT_ID = "https://app.example.com/oauth/client.json"
DOC = {"client_id": CLIENT_ID, "client_name": "Example MCP Client",
       "redirect_uris": [REDIRECT, "https://app.example.com/callback"],
       "grant_types": ["authorization_code", "refresh_token"],
       "token_endpoint_auth_method": "none"}


class FakeFetcher:
    def __init__(self, document=DOC, ttl=300, error=None):
        self.document, self.ttl, self.error, self.calls = document, ttl, error, 0

    def fetch(self, url):
        self.calls += 1
        if self.error:
            raise cimd.CimdError(self.error)
        return json.loads(json.dumps(self.document)), self.ttl


@pytest.fixture
def env(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    return tmp_path


@pytest.fixture
def server(env):
    provider = make_provider(env / "oauth" / "store.json")
    provider.cimd_fetcher = FakeFetcher()
    httpd, thread = start(make_hub(env / "hub"), provider)
    yield httpd
    httpd.stop()
    thread.join(TIMEOUT)


# --- the URL and the address -----------------------------------------------------------

@pytest.mark.parametrize("value, ok", [
    (CLIENT_ID, True),
    ("https://app.example.com:8443/c.json", True),
    ("http://app.example.com/c.json", False),      # https only
    ("https://app.example.com", False),            # needs a path
    ("https://app.example.com/", False),
    ("https://app.example.com/c.json?x=1", False),
    ("https://app.example.com/c.json#f", False),
    ("https://u:p@app.example.com/c.json", False),
    ("https://app.example.com/a/../c.json", False),
    ("https://app.example.com/c .json", False),
    ("nvcl_abc", False),
    (None, False),
])
def test_what_counts_as_a_client_id_url(value, ok):
    assert cimd.is_client_id_url(value) is ok


@pytest.mark.parametrize("address, ok", [
    ("8.8.8.8", True), ("2001:4860:4860::8888", True),
    ("127.0.0.1", False), ("10.0.0.5", False), ("192.168.1.1", False), ("172.16.0.1", False),
    ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False), ("::1", False),
    ("fe80::1%eth0", False), ("fd00::1", False), ("::ffff:127.0.0.1", False),
    ("224.0.0.1", False),
])
def test_only_public_addresses_are_fetched(address, ok):
    assert cimd.public_address(address) is ok


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["8.8.8.8", "10.0.0.1"], []])
def test_a_host_with_any_private_address_is_refused(addresses):
    def resolve(host, port, type=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (a, port)) for a in addresses]

    with pytest.raises(cimd.CimdError, match="public address|resolved"):
        cimd.Fetcher(resolve=resolve).fetch(CLIENT_ID)


@pytest.mark.parametrize("header, seconds", [
    ("", 300), ("max-age=600", 600), ("public, max-age=5", 60), ("max-age=999999", 86400),
    ("no-store", 60), ("no-cache, max-age=600", 60),
])
def test_cache_lifetime_follows_cache_control(header, seconds):
    assert cimd.cache_seconds(header) == seconds


# --- a real TLS fetch -------------------------------------------------------------------

class _DocHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body, ctype, status, extra = json.dumps(DOC).encode(), "application/json", 200, {}
        if self.path == "/redirect.json":
            status, extra = 302, {"Location": "https://169.254.169.254/latest"}
        elif self.path == "/big.json":
            body = json.dumps({**DOC, "pad": "x" * 6000}).encode()
        elif self.path == "/page.json":
            ctype = "text/html"
        elif self.path == "/cached.json":
            extra = {"Cache-Control": "public, max-age=900"}
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in extra.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def doc_server(tmp_path):
    if shutil.which("openssl") is None:
        pytest.skip("needs openssl to make a test certificate")
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-subj", "/CN=app.example.com", "-addext", "subjectAltName=DNS:app.example.com",
                    "-keyout", str(key), "-out", str(cert)], check=True, capture_output=True)
    httpd = http.server.HTTPServer(("127.0.0.1", 0), _DocHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]

    def resolve(host, port_, type=0):
        assert host == "app.example.com"
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port_))]

    # Explicitly a local server: the address check is off and the CA is ours.
    fetcher = cimd.Fetcher(resolve=resolve, address_ok=lambda address: True,
                           ssl_context=ssl.create_default_context(cafile=str(cert)))
    yield fetcher, f"https://app.example.com:{port}"
    httpd.shutdown()


def test_a_document_is_fetched_over_tls_for_its_host(doc_server):
    fetcher, base = doc_server
    document, ttl = fetcher.fetch(base + "/client.json")
    assert document == DOC and ttl == 300
    assert fetcher.fetch(base + "/cached.json")[1] == 900


@pytest.mark.parametrize("path, why", [
    ("/redirect.json", "could not be fetched"),   # redirects are never followed
    ("/big.json", "too large"),
    ("/page.json", "not JSON"),
])
def test_bad_responses_are_refused(doc_server, path, why):
    fetcher, base = doc_server
    with pytest.raises(cimd.CimdError, match=why):
        fetcher.fetch(base + path)


def test_the_certificate_must_be_valid_for_the_host(doc_server):
    fetcher, base = doc_server
    fetcher.ssl_context = ssl.create_default_context()  # the test CA isn't trusted here
    with pytest.raises(cimd.CimdError, match="could not be fetched"):
        fetcher.fetch(base + "/client.json")


# --- the sign-in flow --------------------------------------------------------------------

def test_metadata_says_documents_are_supported(server):
    status, _, body = Client(server).request("GET", "/.well-known/oauth-authorization-server")
    assert json.loads(body)["client_id_metadata_document_supported"] is True


def test_a_client_named_by_its_document_signs_in_and_gets_a_token(server):
    client = Client(server)
    verifier, challenge = pkce()
    query = dev_login(client, CLIENT_ID, challenge)
    status, _, token = exchange(client, CLIENT_ID, query["code"], verifier)
    assert status == 200 and token["refresh_token"]
    info = server.oauth.validate(token["access_token"], None)
    assert info.client_id == CLIENT_ID and info.tenant.startswith(CLIENT_ID + ":")
    record = server.oauth.store.reading
    with record() as data:
        assert data["clients"][CLIENT_ID]["source"] == "cimd"


def test_the_document_is_fetched_again_only_when_its_cache_ran_out(server):
    fetcher, provider = server.oauth.cimd_fetcher, server.oauth
    params = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
              "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "s"}
    provider.start_authorization(dict(params), server.url)
    provider.start_authorization(dict(params), server.url)
    assert fetcher.calls == 1
    clock = provider.clock
    provider.clock = lambda: clock() + 301
    provider.start_authorization(dict(params), server.url)
    assert fetcher.calls == 2


@pytest.mark.parametrize("changes, why", [
    ({"client_id": "https://other.example.com/c.json"}, "client_id"),
    ({"client_name": ""}, "client_name"),
    ({"client_name": "Evil\x1b[31m"}, "client_name"),
    ({"redirect_uris": ["https://attacker.example.net/cb"]}, "redirect URI"),
    ({"redirect_uris": []}, "redirect_uris"),
    ({"token_endpoint_auth_method": "private_key_jwt"}, "client authentication"),
    ({"grant_types": ["client_credentials"]}, "grant_types"),
    ({"response_types": ["token"]}, "response_types"),
])
def test_a_bad_document_is_refused_and_not_stored(server, changes, why):
    server.oauth.cimd_fetcher = FakeFetcher({**DOC, **changes})
    params = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
              "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "s"}
    with pytest.raises(AuthorizeError, match=why):
        server.oauth.start_authorization(params, server.url)
    with server.oauth.store.reading() as data:
        assert CLIENT_ID not in data["clients"]


def test_a_redirect_uri_must_be_one_the_document_lists(server):
    params = {"client_id": CLIENT_ID, "redirect_uri": "https://app.example.com/other",
              "response_type": "code", "code_challenge": pkce()[1],
              "code_challenge_method": "S256", "state": "s"}
    with pytest.raises(AuthorizeError, match="redirect_uri"):
        server.oauth.start_authorization(params, server.url)


def test_a_fetch_failure_shows_fixed_text(server):
    server.oauth.cimd_fetcher = FakeFetcher(error="The client's host is not a public address")
    status, _, page = Client(server).request("GET", authorize_params(CLIENT_ID, pkce()[1]))
    assert status == 400 and b"not a public address" in page


def test_documents_can_be_turned_off_or_limited_to_hosts(server):
    params = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
              "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "s"}
    server.oauth.config.cimd_hosts = frozenset({"trusted.example.org"})
    with pytest.raises(AuthorizeError, match="not accepted"):
        server.oauth.start_authorization(dict(params), server.url)
    server.oauth.config.cimd_hosts = frozenset()
    server.oauth.config.cimd = False
    with pytest.raises(AuthorizeError, match="Unknown client_id"):
        server.oauth.start_authorization(dict(params), server.url)
    assert server.oauth.cimd_fetcher.calls == 0
    status, _, body = Client(server).request("GET", "/.well-known/oauth-authorization-server")
    assert json.loads(body)["client_id_metadata_document_supported"] is False


def test_new_clients_are_rate_limited(server):
    server.oauth.cimd_limiter.per_key = 1
    params = {"client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code",
              "code_challenge": pkce()[1], "code_challenge_method": "S256", "state": "s"}
    server.oauth.cimd_fetcher.ttl = 0  # every sign-in would fetch again
    server.oauth.start_authorization(dict(params), server.url, "203.0.113.9")
    with pytest.raises(AuthorizeError, match="Too many"):
        server.oauth.start_authorization(dict(params), server.url, "203.0.113.9")


def test_the_consent_page_names_the_document_host_and_warns_about_loopback():
    login = {"client_name": "Example MCP Client", "client_host": "app.example.com",
             "redirect_uri": REDIRECT, "scope": ["skills:read"], "id": "x"}
    page = consent_page(login, "dev").decode()
    assert "<code>app.example.com</code>" in page and "come back to this computer" in page


# --- application_type and client credentials ------------------------------------------------

@pytest.mark.parametrize("value", ["native", "web"])
def test_application_type_is_kept_and_echoed(server, value):
    status, body = register(Client(server), application_type=value)
    assert status == 201 and body["application_type"] == value


def test_an_unknown_application_type_is_refused(server):
    status, body = register(Client(server), application_type="desktop")
    assert status == 400 and body["error"] == "invalid_client_metadata"


def test_without_application_type_nothing_is_echoed(server):
    status, body = register(Client(server))
    assert status == 201 and "application_type" not in body


def test_discover_declares_client_credentials_only_with_oauth(server, env):
    from _mcp_http_helpers import service_token

    message = modern("server/discover")
    _, _, body = post_modern(server, message, Authorization=f"Bearer {service_token(server)}")
    assert "io.modelcontextprotocol/oauth-client-credentials" in \
        body["result"]["capabilities"]["extensions"]
    open_server, thread = start(make_hub(env / "hub2"), no_auth=True)
    try:
        _, _, body = post_modern(open_server, message)
        assert "io.modelcontextprotocol/oauth-client-credentials" not in \
            body["result"]["capabilities"]["extensions"]
    finally:
        open_server.stop()
        thread.join(TIMEOUT)

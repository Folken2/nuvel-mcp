"""The Skills extension: skills/list, skills/get, resources/directory/read and
the skill files behind them."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from _mcp_http_helpers import TIMEOUT, isolate_env, start
from nuvel_mcp.extensions import skills as skills_ext
from nuvel_mcp.server import SkillsMCPServer
from nuvel_mcp.skills_loader import SkillsLoader
from test_mcp_http_modern import post_modern
from test_protocol_modern import modern

PDF = "skill://docs/pdf-processing/SKILL.md"
PDF_SKILL = ("---\nname: pdf-processing\ndescription: Extract and fill PDF documents\n"
             "license: Apache-2.0\nmetadata:\n  author: acme\n---\n\n"
             "# PDF\n\nPick a template from `templates/`.\n")
PDF_FILES = {
    "SKILL.md": PDF_SKILL.encode(),
    "templates/invoice.md": b"# Invoice\n",
    "templates/regional/eu.md": b"# EU invoice\n",
    "scripts/extract.py": b"print('extract')\n",
    "assets/logo.bin": bytes(range(256)),
    "my notes.md": b"spaces in the name\n",
}
NOT_SERVED = {".hidden": b"secret\n", "__pycache__/x.pyc": b"\0", ".git/config": b"[core]\n"}


def write(root: Path, rel: str, data: bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def skill(name: str, extra: str = "", description: str | None = "A skill") -> bytes:
    lines = [f"name: {name}"]
    if description is not None:
        lines.append(f"description: {description}")
    return ("---\n" + "\n".join(lines) + "\n" + extra + "---\n\nBody.\n").encode()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    root = tmp_path / "hub" / "skills"
    pdf = root / "docs" / "pdf-processing"
    for rel, data in {**PDF_FILES, **NOT_SERVED}.items():
        write(pdf, rel, data)
    (tmp_path / "outside.md").write_text("outside the hub\n")
    os.symlink(tmp_path / "outside.md", pdf / "templates" / "linked.md")
    write(root / "docs" / "crlf-skill", "SKILL.md",
          b"---\r\nname: crlf-skill\r\ndescription: Windows line ends\r\n---\r\n\r\nBody.\r\n")
    write(root / "hr" / "no-description", "SKILL.md", skill("no-description", description=None))
    write(root / "hr" / "renamed", "SKILL.md", skill("another-name"))
    write(root / "hr" / "ambiguous", "SKILL.md", skill("ambiguous", "beta: yes\n"))
    write(root / "hr" / "Bad_Name", "SKILL.md", skill("Bad_Name"))
    themes = {
        "docs": [{"name": "pdf-processing", "description": "PDFs"},
                 {"name": "crlf-skill", "description": "CRLF"}],
        "hr": [{"name": n, "description": n} for n in
               ("no-description", "renamed", "ambiguous", "Bad_Name", "vendored-elsewhere")],
    }
    (root / "index.json").write_text(json.dumps({"themes": themes}))
    return root


@pytest.fixture
def server(hub):
    return SkillsMCPServer(SkillsLoader.from_base(hub))


def result(server, method, params=None, **kw):
    response = server.dispatch(modern(method, params, **kw))
    assert "error" not in response, response
    return response["result"]


def error(server, method, params=None):
    return server.dispatch(modern(method, params))["error"]


def file_bytes(content: dict) -> bytes:
    return content["text"].encode("utf-8") if "text" in content else base64.b64decode(content["blob"])


def test_discover_declares_the_extension_with_directory_reads(server):
    caps = result(server, "server/discover")["capabilities"]
    assert caps["extensions"]["io.modelcontextprotocol/skills"] == {"directoryRead": True}
    assert "resources" in caps  # required alongside the extension


def test_list_publishes_only_skills_that_follow_the_rules(server, capsys):
    listed = result(server, "skills/list")
    assert [s["uri"] for s in listed["skills"]] == [PDF, "skill://docs/crlf-skill/SKILL.md"]
    assert listed["resultType"] == "complete" and listed["ttlMs"] > 0
    assert listed["cacheScope"] == "private" and "nextCursor" not in listed
    log = capsys.readouterr().err
    for name, why in [("no-description", "no description"), ("renamed", "name differs"),
                      ("ambiguous", "frontmatter is refused"), ("Bad_Name", "naming rules"),
                      ("vendored-elsewhere", "")]:
        assert f"hr/{name} not published: " in log and why in log


def test_the_entry_carries_the_frontmatter_verbatim(server):
    entry = result(server, "skills/list")["skills"][0]
    assert entry["frontmatter"] == {"name": "pdf-processing",
                                    "description": "Extract and fill PDF documents",
                                    "license": "Apache-2.0", "metadata": {"author": "acme"}}


def test_the_manifest_is_every_served_file_with_digest_and_size(server):
    entry = result(server, "skills/list")["skills"][0]
    by_uri = {r["uri"]: r for r in entry["resources"]}
    expected = {skills_ext.file_uri("docs", "pdf-processing", rel): data
                for rel, data in PDF_FILES.items()}
    assert set(by_uri) == set(expected)  # no dotfiles, no __pycache__, no symlinks
    for uri, data in expected.items():
        assert by_uri[uri]["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
        assert by_uri[uri]["size"] == len(data)
    assert skills_ext.file_uri("docs", "pdf-processing", "my notes.md").endswith("my%20notes.md")


def test_every_listed_file_reads_back_to_its_digest(server):
    for entry in result(server, "skills/list")["skills"]:
        for resource in entry["resources"]:
            read = result(server, "resources/read", {"uri": resource["uri"]})
            content = read["contents"][0]
            data = file_bytes(content)
            assert content["uri"] == resource["uri"]
            assert "sha256:" + hashlib.sha256(data).hexdigest() == resource["digest"]
            assert len(data) == resource["size"]
            assert read["ttlMs"] > 0


def test_binary_files_are_blobs_and_text_keeps_its_line_ends(server):
    logo = result(server, "resources/read",
                  {"uri": "skill://docs/pdf-processing/assets/logo.bin"})["contents"][0]
    assert "blob" in logo and base64.b64decode(logo["blob"]) == bytes(range(256))
    crlf = result(server, "resources/read",
                  {"uri": "skill://docs/crlf-skill/SKILL.md"})["contents"][0]
    assert "\r\n" in crlf["text"]


@pytest.mark.parametrize("uri", [
    "skill://docs/pdf-processing/.hidden",
    "skill://docs/pdf-processing/__pycache__/x.pyc",
    "skill://docs/pdf-processing/templates/linked.md",  # a symlink out of the hub
    "skill://docs/pdf-processing/../crlf-skill/SKILL.md",
    "skill://docs/pdf-processing/%2e%2e/crlf-skill/SKILL.md",
    "skill://docs/pdf-processing/templates%2Finvoice.md",
    "skill://docs/pdf-processing/%ff",
    "skill://docs/pdf-processing/nope.md",
    "skill://docs/nope/SKILL.md",
])
def test_unserved_paths_are_not_found_with_fixed_text(server, uri):
    refused = error(server, "resources/read", {"uri": uri})
    assert refused["code"] == -32602
    assert "pdf-processing" not in refused["message"]  # the caller's URI is not echoed


def test_get_returns_the_listed_entry(server):
    listed = result(server, "skills/list")["skills"][0]
    got = result(server, "skills/get", {"uri": PDF})
    assert got["skill"] == listed and got["ttlMs"] > 0 and "nextCursor" not in got


@pytest.mark.parametrize("uri", [
    "skill://docs/nope/SKILL.md", "skill://docs/pdf-processing/templates/invoice.md",
    "skill://docs/pdf-processing", "skill://hr/no-description/SKILL.md", "not a uri", None,
])
def test_get_refuses_what_is_not_a_published_skill(server, uri):
    assert error(server, "skills/get", {"uri": uri}) == {
        "code": -32602, "message": "No skill is served at that URI"}


def test_get_answers_for_a_skill_outside_the_listing_scope(hub):
    scoped = SkillsMCPServer(SkillsLoader.from_base(hub), theme="hr")
    assert result(scoped, "skills/list")["skills"] == []
    assert result(scoped, "skills/get", {"uri": PDF})["skill"]["uri"] == PDF


def test_directory_read_lists_direct_children(server):
    root = result(server, "resources/directory/read", {"uri": "skill://docs/pdf-processing"})
    names = {(r["name"], r["mimeType"]) for r in root["resources"]}
    assert names == {("SKILL.md", "text/markdown"), ("my notes.md", "text/markdown"),
                     ("assets", "inode/directory"), ("scripts", "inode/directory"),
                     ("templates", "inode/directory")}
    templates = result(server, "resources/directory/read",
                       {"uri": "skill://docs/pdf-processing/templates"})["resources"]
    assert [(r["uri"], r["mimeType"]) for r in templates] == [
        ("skill://docs/pdf-processing/templates/invoice.md", "text/markdown"),
        ("skill://docs/pdf-processing/templates/regional", "inode/directory")]
    assert root["ttlMs"] > 0


def test_directory_read_of_a_theme_lists_its_skills(server):
    themed = result(server, "resources/directory/read", {"uri": "skill://docs"})["resources"]
    assert [r["uri"] for r in themed] == ["skill://docs/crlf-skill", "skill://docs/pdf-processing"]


@pytest.mark.parametrize("uri", [PDF, "skill://docs/pdf-processing/nope", "skill://nope",
                                 "skill://docs/nope"])
def test_directory_read_of_a_file_or_nothing_is_invalid(server, uri):
    assert error(server, "resources/directory/read", {"uri": uri})["code"] == -32602


def test_listing_pages_with_an_opaque_cursor(server, monkeypatch):
    monkeypatch.setattr(skills_ext, "PAGE_SIZE", 1)
    first = result(server, "skills/list")
    second = result(server, "skills/list", {"cursor": first["nextCursor"]})
    assert [first["skills"][0]["uri"], second["skills"][0]["uri"]] == [
        PDF, "skill://docs/crlf-skill/SKILL.md"]
    assert "nextCursor" not in second


@pytest.mark.parametrize("cursor", ["garbage", "b246", 7, "x" * 100])
def test_a_bad_cursor_is_invalid_params(server, cursor):
    assert error(server, "skills/list", {"cursor": cursor}) == {
        "code": -32602, "message": "Invalid cursor"}


def test_a_skill_over_the_file_limit_is_not_published(server, monkeypatch, capsys):
    monkeypatch.setattr(skills_ext, "MAX_FILES", 3)
    assert PDF not in [s["uri"] for s in result(server, "skills/list")["skills"]]
    assert "more than 3 files" in capsys.readouterr().err


def test_a_changed_file_gets_a_new_digest(server, hub):
    invoice = hub / "docs" / "pdf-processing" / "templates" / "invoice.md"
    before = result(server, "skills/get", {"uri": PDF})["skill"]["resources"]
    invoice.write_bytes(b"# Invoice v2, longer\n")
    after = result(server, "skills/get", {"uri": PDF})["skill"]["resources"]
    uri = "skill://docs/pdf-processing/templates/invoice.md"
    old, new = ({r["uri"]: r for r in rs}[uri] for rs in (before, after))
    assert old["digest"] != new["digest"] and new["size"] == len(b"# Invoice v2, longer\n")


def test_the_problem_is_logged_once(server, capsys):
    result(server, "skills/list")
    result(server, "skills/list")
    assert capsys.readouterr().err.count("hr/renamed not published") == 1


def test_the_earlier_uri_still_reads_the_skill_md(server):
    read = result(server, "resources/read", {"uri": "skill://docs/pdf-processing"})
    assert read["contents"][0]["text"] == PDF_SKILL


def test_resources_list_names_each_skill_md(server):
    uris = [r["uri"] for r in result(server, "resources/list")["resources"]]
    assert PDF in uris and "skill://hr/renamed/SKILL.md" in uris


def test_legacy_sessions_can_use_the_extension_too(server):
    response = server.dispatch({"jsonrpc": "2.0", "id": 1, "method": "skills/get",
                                "params": {"uri": PDF}})
    assert response["result"]["skill"]["uri"] == PDF
    assert "ttlMs" not in response["result"]  # stamping is for the modern path
    init = server.dispatch({"jsonrpc": "2.0", "id": 2, "method": "initialize",
                            "params": {"protocolVersion": "2025-11-25", "capabilities": {}}})
    assert "io.modelcontextprotocol/skills" in init["result"]["capabilities"]["extensions"]


def test_feedback_only_mode_offers_no_extension(tmp_path, monkeypatch):
    isolate_env(monkeypatch, tmp_path)
    bare = SkillsMCPServer(None)
    assert "extensions" not in result(bare, "server/discover")["capabilities"]
    assert error(bare, "skills/list")["code"] == -32601


def test_over_http_a_file_is_read_with_its_name_header(hub):
    httpd, thread = start(hub, no_auth=True)
    try:
        status, _, body = post_modern(httpd, modern("skills/list"))
        assert status == 200 and body["result"]["skills"][0]["uri"] == PDF
        uri = "skill://docs/pdf-processing/my%20notes.md"
        status, _, body = post_modern(httpd, modern("resources/read", {"uri": uri}))
        assert status == 200 and body["result"]["contents"][0]["text"] == "spaces in the name\n"
        status, _, body = post_modern(httpd, modern("skills/get", {"uri": "skill://x/y/SKILL.md"}))
        assert status == 400 and body["error"]["code"] == -32602
    finally:
        httpd.stop()
        thread.join(TIMEOUT)

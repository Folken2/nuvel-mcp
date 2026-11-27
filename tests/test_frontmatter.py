"""The strict SKILL.md frontmatter reader.

Every accepted case is also read with PyYAML (YAML 1.1) when it is installed
(the conformance extra): the two must agree, since a host compares what we
send with what its own parser reads. Refused cases are the ones parsers
disagree on, or that this reader does not handle.
"""

from __future__ import annotations

import pytest

from nuvel_mcp.frontmatter import FrontmatterError, parse

ACCEPTED = {
    "minimal": ("name: pdf\ndescription: Work with PDFs\n",
                {"name": "pdf", "description": "Work with PDFs"}),
    "agent skills fields": (
        "name: refunds\ndescription: Process refunds\nlicense: Apache-2.0\n"
        "compatibility: Requires git\nallowed-tools: Bash(git:*) Read\n"
        "metadata:\n  author: acme\n  version: \"1.0\"\n",
        {"name": "refunds", "description": "Process refunds", "license": "Apache-2.0",
         "compatibility": "Requires git", "allowed-tools": "Bash(git:*) Read",
         "metadata": {"author": "acme", "version": "1.0"}}),
    "scalars": (
        "a: 1\nb: -7\nc: 1.5\nd: .5\ne: true\nf: False\ng: ~\nh: null\ni:\nj: 1.0.0\n"
        "k: C#\nl: a#b\nm: http://x.y/z\nn: 'it''s'\no: \"tab\\there \\u00e9\"\n",
        {"a": 1, "b": -7, "c": 1.5, "d": 0.5, "e": True, "f": False, "g": None, "h": None,
         "i": None, "j": "1.0.0", "k": "C#", "l": "a#b", "m": "http://x.y/z",
         "n": "it's", "o": "tab\there \u00e9"}),
    "comments": ("# top\nname: x # trailing\n\n# between\ndescription: why\n",
                 {"name": "x", "description": "why"}),
    "lists": (
        "requires: [api_key, 'b c', \"d\"]\nempty: []\nblock:\n  - one\n  - 2\n"
        "same_indent:\n- a\n- b\n",
        {"requires": ["api_key", "b c", "d"], "empty": [], "block": ["one", 2],
         "same_indent": ["a", "b"]}),
    "nested": ("metadata:\n  a:\n    b: c\n  d: e\nname: nm\n",
               {"metadata": {"a": {"b": "c"}, "d": "e"}, "name": "nm"}),
    "literal": ("description: |\n  line one\n  line two\n\n  after blank\nname: nm\n",
                {"description": "line one\nline two\n\nafter blank\n", "name": "nm"}),
    "literal strip and keep": (
        "a: |-\n  x\n  y\n\nb: |+\n  z\n\nc: done\n",
        {"a": "x\ny", "b": "z\n\n", "c": "done"}),
    "folded": ("description: >\n  folded text\n  continues here\n\n  new paragraph\nname: nm\n",
               {"description": "folded text continues here\nnew paragraph\n", "name": "nm"}),
    "folded strip": ("description: >-\n  a\n  b\n", {"description": "a b"}),
    "crlf": ("name: x\r\ndescription: why\r\n", {"name": "x", "description": "why"}),
    "unicode": ("name: café\ndescription: 日本語のスキル\n",
                {"name": "café", "description": "日本語のスキル"}),
    "reserved metadata key": ("metadata:\n  io.modelcontextprotocol/x: z\n",
                              {"metadata": {"io.modelcontextprotocol/x": "z"}}),
}

REFUSED = {
    "yes": "flag: yes\n",
    "on": "flag: On\n",
    "octal": "mode: 0755\n",
    "hex": "n: 0x1F\n",
    "sexagesimal": "t: 1:30\n",
    "underscore": "n: 1_000\n",
    "exponent": "n: 1e5\n",
    "inf": "n: .inf\n",
    "date": "released: 2025-01-01\n",
    "timestamp": "at: 2025-01-01T10:00:00Z\n",
    "big int": "n: 12345678901234567890\n",
    "anchor": "a: &x 1\nb: *x\n",
    "tag": "a: !!str 1\n",
    "flow mapping": "a: {b: c}\n",
    "nested flow": "a: [[1, 2]]\n",
    "multi-line plain": "description: first\n  second\n",
    "multi-line quoted": "description: \"first\n  second\"\n",
    "duplicate key": "name: a\nname: b\n",
    "merge key": "<<: x\n",
    "tab indent": "metadata:\n\tauthor: a\n",
    "mapping in list": "a:\n  - b: c\n",
    "plain colon": "a: b: c\n",
    "bad escape": "a: \"\\q\"\n",
    "text after quote": "a: 'x' y\n",
    "more-indented folded": "a: >\n  x\n    y\n",
    "indicator start": "a: %x\n",
    "y as a boolean": "flag: y\n",
    "quoted key": "\"a\": b\n",
}


def wrap(body: str) -> str:
    return "---\n" + body + "---\n\n# Body\n"


@pytest.mark.parametrize("body, expected", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_accepted(body, expected):
    assert parse(wrap(body)) == expected


@pytest.mark.parametrize("body, expected", ACCEPTED.values(), ids=ACCEPTED.keys())
def test_accepted_matches_pyyaml(body, expected):
    yaml = pytest.importorskip("yaml")
    assert yaml.safe_load(body.replace("\r\n", "\n")) == expected


@pytest.mark.parametrize("body", REFUSED.values(), ids=REFUSED.keys())
def test_refused(body):
    with pytest.raises(FrontmatterError):
        parse(wrap(body))


@pytest.mark.parametrize("text", ["---\nname: x\n", "name: x\n---\n", ""],
                         ids=["unclosed", "no opening fence", "empty file"])
def test_a_missing_block_is_refused(text):
    with pytest.raises(FrontmatterError):
        parse(text)


def test_errors_name_a_line_and_never_the_text():
    with pytest.raises(FrontmatterError) as exc:
        parse(wrap("name: ok\nsecret: yes\n"))
    assert "line 3" in str(exc.value) and "secret" not in str(exc.value)

"""A strict reader for ``SKILL.md`` YAML frontmatter, as JSON-ready data.

The Skills extension sends a skill's frontmatter "verbatim as a JSON object",
and a host compares it field by field with what its own YAML parser reads from
the file; any difference and the host refuses the skill. Hosts use different
parsers (YAML 1.1 like PyYAML, YAML 1.2 like the JavaScript ``yaml`` package),
which disagree on plain scalars such as ``yes``, ``012`` or ``2025-01-01``.

So this reader accepts only the subset of YAML on which they agree, and
refuses (:class:`FrontmatterError`) anything else rather than guess: a skill
with such frontmatter is not served through the extension, and the operator
log says why. Accepted:

- block mappings, nested by indentation (spaces only), plain keys;
- block sequences (``- item``) and flow sequences (``[a, b]``) of scalars;
- plain, single-quoted and double-quoted scalars on one line, and literal
  (``|``) or folded (``>``) block scalars with ``-``/``+`` chomping;
- ``#`` comments.

Plain scalars resolve to null (``~``, ``null``, empty), ``true``/``false``,
decimal integers, decimal floats, or strings; a value one schema would read
differently from another is refused. Error messages name a line, never its
text. Standard library only.
"""

from __future__ import annotations

import re

MAX_DEPTH = 8
_SAFE_INT = 2 ** 53 - 1

_KEY = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.\-/]*)[ ]*:(?:[ ]+|$)")
_INT = re.compile(r"[-+]?(?:0|[1-9][0-9]*)")
_FLOAT = re.compile(r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)")
_NULLS = {"", "~", "null", "Null", "NULL"}
_BOOLS = {"true": True, "True": True, "TRUE": True,
          "false": False, "False": False, "FALSE": False}
# Plain scalars YAML 1.1 and 1.2 parsers read differently: refused.
_AMBIGUOUS_WORDS = {"y", "Y", "yes", "Yes", "YES", "n", "N", "no", "No", "NO",
                    "on", "On", "ON", "off", "Off", "OFF", "=", "<<"}
_AMBIGUOUS = re.compile(
    r"[-+]?0[0-9]+(?:\.[0-9]*)?"                   # leading zero: octal in 1.1
    r"|[-+]?0[xXoObB][0-9a-fA-F_]+"                # hex, octal, binary
    r"|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+(?:\.[0-9_]*)?"  # sexagesimal (1.1)
    r"|[-+]?[0-9][0-9_]*_[0-9_]*(?:\.[0-9_]*)?"    # underscores (1.1)
    r"|[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)[eE][-+]?[0-9]+"  # exponents (sign rules differ)
    r"|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)"   # not JSON
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt ].*)?"  # timestamps (1.1, js-yaml)
)
# A plain scalar may not start with an indicator.
_INDICATORS = set("-?:,[]{}#&*!|>'\"%@`")
_ESCAPES = {"0": "\0", "a": "\a", "b": "\b", "t": "\t", "\t": "\t", "n": "\n",
            "v": "\v", "f": "\f", "r": "\r", "e": "\x1b", " ": " ", '"': '"',
            "/": "/", "\\": "\\", "N": "\x85", "_": "\xa0"}


class FrontmatterError(ValueError):
    """The frontmatter is missing, malformed, or outside the accepted subset."""


def _fail(line_no: int, why: str):
    raise FrontmatterError(f"line {line_no}: {why}")


def split(text: str) -> tuple[list[str], int]:
    """The frontmatter's lines (between the ``---`` fences) and the file line
    number of the first one. Raises when there is no closed block."""
    lines = text.split("\n")
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    if not lines or lines[0].rstrip(" ") != "---":
        raise FrontmatterError("line 1: SKILL.md must start with a '---' line")
    for end in range(1, len(lines)):
        if lines[end].rstrip(" ") in ("---", "..."):
            return lines[1:end], 2
    raise FrontmatterError("the frontmatter block is not closed with '---'")


def parse(text: str) -> dict:
    """The frontmatter of *text* (a whole ``SKILL.md``) as a dict."""
    lines, first = split(text)
    parser = _Parser(lines, first)
    result = parser.mapping(0, depth=0)
    parser.skip_blank()
    if parser.i < len(lines):
        _fail(parser.line_no, "unexpected indentation")
    return result if result is not None else {}


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _strip_comment(rest: str, line_no: int) -> str:
    """A value with a trailing `` #comment`` removed; the comment needs a space before it."""
    index = rest.find(" #")
    if index != -1:
        rest = rest[:index]
    if rest.startswith("#"):
        return ""
    return rest.rstrip(" ")


def _plain(value: str, line_no: int):
    if value in _NULLS:
        return None
    if value in _BOOLS:
        return _BOOLS[value]
    if value in _AMBIGUOUS_WORDS or _AMBIGUOUS.fullmatch(value):
        _fail(line_no, "a value that YAML parsers read differently; quote it")
    if _INT.fullmatch(value):
        number = int(value)
        if abs(number) > _SAFE_INT:
            _fail(line_no, "an integer too large to compare exactly; quote it")
        return number
    if _FLOAT.fullmatch(value):
        return float(value)
    if value[0] in _INDICATORS or ": " in value or value.endswith(":") or "\t" in value:
        _fail(line_no, "a plain value YAML would not read as text; quote it")
    return value


def _double_quoted(rest: str, line_no: int) -> tuple[str, str]:
    """``"..."`` at the start of *rest*: the decoded text and what follows."""
    out: list[str] = []
    i = 1
    while i < len(rest):
        c = rest[i]
        if c == '"':
            return "".join(out), rest[i + 1:]
        if c == "\\":
            if i + 1 >= len(rest):
                break
            code = rest[i + 1]
            widths = {"x": 2, "u": 4, "U": 8}
            if code in widths:
                digits = rest[i + 2:i + 2 + widths[code]]
                if len(digits) != widths[code] or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                    _fail(line_no, "a malformed escape in a quoted value")
                point = int(digits, 16)
                if 0xD800 <= point <= 0xDFFF or point > 0x10FFFF:
                    _fail(line_no, "a malformed escape in a quoted value")
                out.append(chr(point))
                i += 2 + widths[code]
                continue
            if code not in _ESCAPES:
                _fail(line_no, "an unknown escape in a quoted value")
            out.append(_ESCAPES[code])
            i += 2
            continue
        out.append(c)
        i += 1
    _fail(line_no, "a quoted value must close on its own line")


def _single_quoted(rest: str, line_no: int) -> tuple[str, str]:
    out: list[str] = []
    i = 1
    while i < len(rest):
        if rest[i] == "'":
            if rest[i + 1:i + 2] == "'":
                out.append("'")
                i += 2
                continue
            return "".join(out), rest[i + 1:]
        out.append(rest[i])
        i += 1
    _fail(line_no, "a quoted value must close on its own line")


def _after_quote(tail: str, line_no: int) -> None:
    tail = tail.rstrip(" ")
    if tail and not tail.lstrip(" ").startswith("#"):
        _fail(line_no, "text after a quoted value")
    if tail and not tail.startswith(" "):
        _fail(line_no, "text after a quoted value")


def _flow_item(item: str, line_no: int):
    item = item.strip(" ")
    if not item:
        _fail(line_no, "an empty item in a [list]")
    if item[0] == '"':
        value, tail = _double_quoted(item, line_no)
    elif item[0] == "'":
        value, tail = _single_quoted(item, line_no)
    else:
        if item[0] in _INDICATORS or "#" in item:
            _fail(line_no, "an item in a [list] that YAML would not read as text")
        return _plain(item, line_no)
    if tail.strip(" "):
        _fail(line_no, "text after a quoted item in a [list]")
    return value


def _flow_sequence(rest: str, line_no: int) -> list:
    close = rest.rfind("]")
    if close == -1:
        _fail(line_no, "a [list] must close on its own line")
    if rest[close + 1:].strip(" "):
        _fail(line_no, "text after a [list]")
    inner = rest[1:close]
    if any(c in inner for c in "[]{}"):
        _fail(line_no, "a nested collection in a [list]")
    if not inner.strip(" "):
        return []
    items, current, quote = [], [], None
    for c in inner:  # split on commas outside quotes
        if quote:
            current.append(c)
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            current.append(c)
        elif c == ",":
            items.append("".join(current))
            current = []
        else:
            current.append(c)
    if quote:
        _fail(line_no, "a quoted item in a [list] is not closed")
    items.append("".join(current))
    if items and not items[-1].strip(" "):
        items.pop()  # a trailing comma
    return [_flow_item(item, line_no) for item in items]


class _Parser:
    def __init__(self, lines: list[str], first_line_no: int):
        self.lines = lines
        self.first = first_line_no
        self.i = 0

    @property
    def line_no(self) -> int:
        return self.first + self.i

    def skip_blank(self) -> None:
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if "\t" in line[:_indent(line) + 1] and line.strip(" \t"):
                _fail(self.line_no, "tabs are not allowed for indentation")
            stripped = line.strip(" ")
            if stripped and not stripped.startswith("#"):
                return
            self.i += 1

    def peek(self) -> tuple[int, str] | None:
        self.skip_blank()
        if self.i >= len(self.lines):
            return None
        line = self.lines[self.i]
        return _indent(line), line

    def mapping(self, indent: int, depth: int) -> dict | None:
        if depth > MAX_DEPTH:
            _fail(self.line_no, "nested too deep")
        result: dict = {}
        while (head := self.peek()) is not None:
            col, line = head
            if col < indent:
                break
            if col > indent:
                _fail(self.line_no, "unexpected indentation")
            body = line[col:]
            match = _KEY.match(body)
            if match is None:
                _fail(self.line_no, "expected 'key: value'")
            key = match.group(1)
            if key in result:
                _fail(self.line_no, "a key appears twice")
            if key == "<<":
                _fail(self.line_no, "merge keys are not allowed")
            line_no = self.line_no
            self.i += 1
            result[key] = self.value(body[match.end():], indent, depth, line_no)
        return result

    def value(self, rest: str, indent: int, depth: int, line_no: int):
        """The value after ``key:`` on a line indented *indent*."""
        if rest.startswith(("|", ">")):
            return self.block_scalar(rest, indent, line_no)
        text = _strip_comment(rest, line_no)
        if text == "":
            nested = self.peek()
            if nested is None:
                return None
            col, line = nested
            body = line[col:]
            if (col > indent or col == indent) and (body == "-" or body.startswith("- ")):
                return self.sequence(col, depth + 1)
            if col > indent:
                return self.mapping(col, depth + 1)
            return None
        if text[0] == '"':
            value, tail = _double_quoted(rest, line_no)
            _after_quote(tail, line_no)
        elif text[0] == "'":
            value, tail = _single_quoted(rest, line_no)
            _after_quote(tail, line_no)
        elif text[0] == "[":
            value = _flow_sequence(text, line_no)
        else:
            if text[0] in "{&*!%@`?" or text.startswith("- ") or text == "-":
                _fail(line_no, "a YAML feature this reader does not accept")
            value = _plain(text, line_no)
        self.refuse_continuation(indent)
        return value

    def refuse_continuation(self, indent: int) -> None:
        head = self.peek()
        if head is not None and head[0] > indent:
            _fail(self.line_no, "a value continued on the next line; use | or >")

    def sequence(self, indent: int, depth: int) -> list:
        if depth > MAX_DEPTH:
            _fail(self.line_no, "nested too deep")
        items: list = []
        while (head := self.peek()) is not None:
            col, line = head
            body = line[col:]
            if col != indent or not (body == "-" or body.startswith("- ")):
                if col > indent:
                    _fail(self.line_no, "unexpected indentation")
                break
            line_no = self.line_no
            self.i += 1
            rest = body[2:] if body.startswith("- ") else ""
            text = _strip_comment(rest, line_no)
            if not text or text.startswith(("- ", "|", ">")) or _KEY.match(text):
                _fail(line_no, "a list item must be a single value")
            if text[0] == '"':
                value, tail = _double_quoted(rest.lstrip(" "), line_no)
                _after_quote(tail, line_no)
            elif text[0] == "'":
                value, tail = _single_quoted(rest.lstrip(" "), line_no)
                _after_quote(tail, line_no)
            elif text[0] == "[":
                _fail(line_no, "a nested collection in a list")
            else:
                if text[0] in "{&*!%@`?":
                    _fail(line_no, "a YAML feature this reader does not accept")
                value = _plain(text, line_no)
            self.refuse_continuation(col)
            items.append(value)
        return items

    def block_scalar(self, rest: str, indent: int, line_no: int) -> str:
        header = _strip_comment(rest, line_no)
        if header not in ("|", ">", "|-", ">-", "|+", ">+"):
            _fail(line_no, "a block scalar header this reader does not accept")
        style, chomp = header[0], header[1:]
        raw: list[str] = []
        content_indent = None
        while self.i < len(self.lines):
            line = self.lines[self.i]
            if not line.strip(" "):
                raw.append(line)
                self.i += 1
                continue
            col = _indent(line)
            if "\t" in line[:col + 1]:
                _fail(self.line_no, "tabs are not allowed for indentation")
            if content_indent is None:
                if col <= indent:
                    break
                if any(len(blank) > col for blank in raw):
                    _fail(self.line_no, "leading blank lines indented past the text")
                content_indent = col
            elif col < content_indent:
                if col > indent:
                    _fail(self.line_no, "a block scalar line indented less than its text")
                break
            raw.append(line)
            self.i += 1
        if content_indent is None:
            if chomp == "+" and raw:
                _fail(line_no, "an empty kept block scalar")
            return ""
        # Trailing blank lines belong to the chomping, not the text.
        trailing = 0
        while raw and not raw[-1].strip(" "):
            raw.pop()
            trailing += 1
        body = [line[content_indent:] if len(line) >= content_indent else "" for line in raw]
        if style == "|":
            text = "\n".join(body)
        else:
            if any(line.startswith((" ", "\t")) for line in body):
                _fail(line_no, "more-indented lines in a folded block scalar; use |")
            text, blanks = "", 0
            for index, line in enumerate(body):
                if not line:
                    blanks += 1
                    continue
                if index == blanks:  # leading blank lines
                    text = "\n" * blanks
                elif text and blanks:
                    text += "\n" * blanks
                elif text:
                    text += " "
                text += line
                blanks = 0
        if chomp == "-":
            return text
        if chomp == "+":
            return text + "\n" + "\n" * trailing
        return text + "\n"

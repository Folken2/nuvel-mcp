"""The Skills extension (``io.modelcontextprotocol/skills``, SEP-2640).

Serves the hub's skills as the extension describes them: every file of a
skill is a resource at ``skill://<theme>/<name>/<file-path>``, read with
``resources/read``; ``skills/list`` and ``skills/get`` return each skill's
entry (its frontmatter, verbatim, and a manifest of every file with its
SHA-256 digest and size); ``resources/directory/read`` lists a directory.

A skill is published through ``skills/list`` and ``skills/get`` only when the
extension's rules hold for it: a ``SKILL.md`` whose frontmatter the strict
reader accepts (:mod:`nuvel_mcp.frontmatter`), with ``name`` equal to the
index entry's name and ``description`` set, a name and theme the URI rules
allow, and at most 512 files and 16 MiB. Any other skill stays readable file
by file but is left out of the listing, and the operator log says why (once).

What a skill's files are: every regular file under its directory, except
names starting with ``.``, ``__pycache__`` directories and symbolic links,
which are never served. The manifest and ``resources/read`` use the same
list, so they cannot disagree.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import mimetypes
import os
import re
import stat
import sys
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from nuvel_mcp import frontmatter
from nuvel_mcp.protocol.errors import INVALID_PARAMS, McpError
from nuvel_mcp.skills_loader import SkillsError, SkillsLoader

IDENTIFIER = "io.modelcontextprotocol/skills"
SCHEME = "skill://"
SKILL_FILE = "SKILL.md"
MAX_FILES = 512
MAX_BYTES = 16 * 1024 * 1024
PAGE_SIZE = 100
DIRECTORY_MIME = "inode/directory"
# Agent Skills naming: lowercase letters, digits and single hyphens, 64 at most.
_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# A theme is the URI authority: a plain reg-name.
_THEME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._~-]*[A-Za-z0-9])?")
_MIME = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
         ".py": "text/x-python", ".sh": "text/x-shellscript", ".json": "application/json",
         ".yaml": "application/yaml", ".yml": "application/yaml", ".csv": "text/csv",
         ".html": "text/html", ".js": "text/javascript", ".ts": "text/plain",
         ".toml": "application/toml", ".xml": "application/xml"}
_NOT_FOUND = "Resource not found"
_NO_SKILL = "No skill is served at that URI"
_NOT_A_DIRECTORY = "Not a directory resource"
_BAD_CURSOR = "Invalid cursor"


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


class Unservable(Exception):
    """Why a skill is not published through the extension (operator log only)."""


@dataclass(frozen=True)
class SkillFile:
    rel: str  # posix path relative to the skill directory
    path: Path
    size: int
    digest: str


def _quote(segment: str) -> str:
    return urllib.parse.quote(segment, safe="-._~!$&'()*+,;=:@")


def file_uri(theme: str, name: str, rel: str = "") -> str:
    """``skill://<theme>/<name>[/<rel>]``, each segment percent-encoded."""
    parts = [theme, name, *(rel.split("/") if rel else [])]
    return SCHEME + "/".join(_quote(part) for part in parts)


def parse_uri(uri: object) -> list[str] | None:
    """The decoded segments of a ``skill://`` URI, or None when it is not one
    we could serve (empty, ``.`` or ``..`` segments, backslashes, NUL)."""
    if not isinstance(uri, str) or not uri.startswith(SCHEME) or "?" in uri or "#" in uri:
        return None
    raw = uri[len(SCHEME):]
    if not raw:
        return None
    try:
        segments = [urllib.parse.unquote(part, errors="strict") if "%" in part else part
                    for part in raw.split("/")]
    except UnicodeDecodeError:
        return None
    if any(seg in ("", ".", "..") or "/" in seg or "\\" in seg or "\x00" in seg
           for seg in segments):
        return None
    return segments


def _mime(rel: str) -> str:
    suffix = os.path.splitext(rel)[1].lower()
    return _MIME.get(suffix) or mimetypes.guess_type(rel)[0] or "application/octet-stream"


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: object) -> int:
    if cursor is None:
        return 0
    try:
        if not isinstance(cursor, str) or len(cursor) > 64:
            raise ValueError
        text = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        if not re.fullmatch(r"o:(0|[1-9][0-9]{0,8})", text):
            raise ValueError
        return int(text[2:])
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise McpError(INVALID_PARAMS, _BAD_CURSOR) from None


def _page(items: list, params: dict) -> tuple[list, dict]:
    size = PAGE_SIZE
    start = _decode_cursor(params.get("cursor"))
    if start > len(items):
        raise McpError(INVALID_PARAMS, _BAD_CURSOR)
    page = items[start:start + size]
    extra = {"nextCursor": _encode_cursor(start + size)} if start + size < len(items) else {}
    return page, extra


class SkillsExtension:
    """The extension's methods over one hub. ``scoped_entries`` yields the
    ``(theme, entry)`` pairs discovery shows (``--theme``, ``--require``)."""

    identifier = IDENTIFIER

    def __init__(self, loader: SkillsLoader, scoped_entries):
        self.loader = loader
        self.scoped_entries = scoped_entries
        self._digests: dict[str, tuple[int, int, str]] = {}
        self._lock = threading.Lock()
        self._logged: set[tuple[str, str, str]] = set()

    def settings(self) -> dict:
        return {"directoryRead": True}

    def methods(self) -> dict:
        return {"skills/list": self.handle_list, "skills/get": self.handle_get,
                "resources/directory/read": self.handle_directory_read}

    # --- files -----------------------------------------------------------------------

    def _root(self) -> Path:
        return self.loader.skills_dir.resolve()

    def skill_dir(self, theme: str, entry: dict) -> Path:
        """The skill's directory, which must sit inside the hub."""
        directory = self.loader.skill_path(theme, entry).parent
        resolved = directory.resolve()
        root = self._root()
        if resolved != root and root not in resolved.parents:
            raise Unservable("its directory is outside the hub")
        if not resolved.is_dir() or directory.is_symlink():
            raise Unservable("its directory is missing or a symbolic link")
        return resolved

    def _digest(self, path: Path, info: os.stat_result) -> str:
        key = str(path)
        with self._lock:
            cached = self._digests.get(key)
        if cached and cached[:2] == (info.st_mtime_ns, info.st_size):
            return cached[2]
        digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        with self._lock:
            self._digests[key] = (info.st_mtime_ns, info.st_size, digest)
        return digest

    def files(self, directory: Path) -> list[SkillFile]:
        """Every servable file of the skill at *directory*, sorted, with digests."""
        found: list[tuple[str, Path, os.stat_result]] = []
        total = 0
        for current, dirnames, filenames in os.walk(directory, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith(".")
                                 and d != "__pycache__"
                                 and not os.path.islink(os.path.join(current, d)))
            for filename in sorted(filenames):
                if filename.startswith("."):
                    continue
                path = Path(current) / filename
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode):
                    continue  # symbolic links, sockets, devices: never served
                found.append((path.relative_to(directory).as_posix(), path, info))
                total += info.st_size
                if len(found) > MAX_FILES:
                    raise Unservable(f"it has more than {MAX_FILES} files")
                if total > MAX_BYTES:
                    raise Unservable(f"its files exceed {MAX_BYTES} bytes")
        return [SkillFile(rel, path, info.st_size, self._digest(path, info))
                for rel, path, info in sorted(found)]

    # --- entries ---------------------------------------------------------------------

    def entry(self, theme: str, index_entry: dict) -> dict:
        """The extension's ``Skill`` entry, or raise :class:`Unservable`."""
        name = index_entry.get("name")
        if not isinstance(name, str) or len(name) > 64 or not _NAME.fullmatch(name):
            raise Unservable("its name breaks the Agent Skills naming rules")
        if not _THEME.fullmatch(theme):
            raise Unservable("its theme is not a valid URI authority")
        files = self.files(self.skill_dir(theme, index_entry))
        main = next((f for f in files if f.rel == SKILL_FILE), None)
        if main is None:
            raise Unservable("it has no SKILL.md")
        try:
            text = main.path.read_bytes().decode("utf-8")
            meta = frontmatter.parse(text)
        except UnicodeDecodeError:
            raise Unservable("its SKILL.md is not UTF-8") from None
        except frontmatter.FrontmatterError as exc:
            raise Unservable(f"its frontmatter is refused ({exc})") from None
        if meta.get("name") != name:
            raise Unservable("its frontmatter name differs from the index name")
        if not isinstance(meta.get("description"), str) or not meta["description"].strip():
            raise Unservable("its frontmatter has no description")
        return {
            "uri": file_uri(theme, name, SKILL_FILE),
            "frontmatter": meta,
            "resources": [{"uri": file_uri(theme, name, f.rel), "digest": f.digest,
                           "size": f.size} for f in files],
        }

    def _entry_or_none(self, theme: str, index_entry: dict) -> dict | None:
        try:
            return self.entry(theme, index_entry)
        except (Unservable, SkillsError, OSError) as exc:
            key = (theme, str(index_entry.get("name")), str(exc))
            if key not in self._logged:
                self._logged.add(key)
                _log(f"skills extension: {theme}/{index_entry.get('name')} not published: {exc}")
            return None

    def _find(self, theme: str, name: str) -> dict | None:
        found_theme, index_entry = self.loader.find(theme, name)
        return index_entry if found_theme == theme else None

    # --- methods ---------------------------------------------------------------------

    def handle_list(self, params: dict) -> dict:
        skills = [entry for theme, index_entry in self.scoped_entries()
                  if (entry := self._entry_or_none(theme, index_entry)) is not None]
        page, extra = _page(skills, params)
        return {"skills": page, **extra}

    def handle_get(self, params: dict) -> dict:
        segments = parse_uri(params.get("uri"))
        if segments is None or len(segments) != 3 or segments[2] != SKILL_FILE:
            raise McpError(INVALID_PARAMS, _NO_SKILL)
        index_entry = self._find(segments[0], segments[1])
        entry = self._entry_or_none(segments[0], index_entry) if index_entry else None
        if entry is None:
            raise McpError(INVALID_PARAMS, _NO_SKILL)
        return {"skill": entry}

    def handle_directory_read(self, params: dict) -> dict:
        segments = parse_uri(params.get("uri"))
        if segments is None:
            raise McpError(INVALID_PARAMS, _NOT_A_DIRECTORY)
        theme = segments[0]
        if len(segments) == 1:
            names = sorted({e.get("name") for t, e in self.scoped_entries()
                            if t == theme and isinstance(e.get("name"), str)})
            if not names:
                raise McpError(INVALID_PARAMS, _NOT_A_DIRECTORY)
            children = [{"uri": file_uri(theme, n), "name": n, "mimeType": DIRECTORY_MIME}
                        for n in names]
        else:
            files = self._skill_files(theme, segments[1])
            prefix = "/".join(segments[2:])
            children = self._children(theme, segments[1], files, prefix)
        page, extra = _page(children, params)
        return {"resources": page, **extra}

    def _skill_files(self, theme: str, name: str) -> list[SkillFile]:
        index_entry = self._find(theme, name)
        if index_entry is None:
            raise McpError(INVALID_PARAMS, _NOT_FOUND)
        try:
            return self.files(self.skill_dir(theme, index_entry))
        except (Unservable, SkillsError, OSError):
            raise McpError(INVALID_PARAMS, _NOT_FOUND) from None

    def _children(self, theme: str, name: str, files: list[SkillFile], prefix: str) -> list:
        lead = prefix + "/" if prefix else ""
        dirs: set[str] = set()
        children: list[dict] = []
        for f in files:
            if not f.rel.startswith(lead):
                continue
            head, _, tail = f.rel[len(lead):].partition("/")
            if tail:
                dirs.add(head)
            else:
                children.append({"uri": file_uri(theme, name, f.rel), "name": head,
                                 "mimeType": _mime(f.rel), "size": f.size})
        if not children and not dirs:
            raise McpError(INVALID_PARAMS, _NOT_A_DIRECTORY)
        children += [{"uri": file_uri(theme, name, lead + d), "name": d,
                      "mimeType": DIRECTORY_MIME} for d in sorted(dirs)]
        return sorted(children, key=lambda c: c["name"])

    def read(self, segments: list[str]) -> dict:
        """``resources/read`` of ``skill://<theme>/<name>/<file>``: the file's
        bytes, as text when they are UTF-8, else base64."""
        files = self._skill_files(segments[0], segments[1])
        rel = "/".join(segments[2:])
        match = next((f for f in files if f.rel == rel), None)
        if match is None:
            raise McpError(INVALID_PARAMS, _NOT_FOUND)
        raw = match.path.read_bytes()
        content: dict = {"uri": file_uri(segments[0], segments[1], rel), "mimeType": _mime(rel)}
        try:
            content["text"] = raw.decode("utf-8")
        except UnicodeDecodeError:
            content["blob"] = base64.b64encode(raw).decode("ascii")
        return {"contents": [content]}

    def resource_list(self) -> list[dict]:
        """``resources/list``: each skill's ``SKILL.md``."""
        resources = []
        for theme, index_entry in self.scoped_entries():
            name = index_entry.get("name")
            if not isinstance(name, str):
                continue
            resources.append({"uri": file_uri(theme, name, SKILL_FILE), "name": name,
                              "description": index_entry.get("description", ""),
                              "mimeType": "text/markdown"})
        return resources

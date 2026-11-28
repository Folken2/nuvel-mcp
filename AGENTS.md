# AGENTS.md

Guidance for coding agents in this repository. This file is the source of truth;
`CLAUDE.md` only imports it, so make changes here.

## What this is

`nuvel-mcp`: an MCP server that serves a skills hub (a directory of `SKILL.md` skills with
an `index.json`) over stdio or HTTP, and records skill feedback, content-free run traces,
content-free UI events and shared recipes. Python 3.11+, package in `src/nuvel_mcp/`, one
console script `nuvel-mcp` (`cli.py`). `README.md` is the user documentation: flags,
endpoints, environment variables, storage.
The plan for MCP 2026-07-28 and the extensions (skills, apps, tasks, plugins) is
`docs/design/foundation.md`: read it before changing the protocol layer.

`nuvel-cli` ([Folken2/nuvel](https://github.com/Folken2/nuvel)) depends on this package
(`nuvel mcp serve` delegates here) and pins a release tag. This package never depends on
`nuvel-cli` except through the `org-memory` extra.

## Commands

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"          # add ",postgres" for the database tests
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m pytest tests/test_mcp_http.py -q   # one file
.venv/bin/pip install -e ".[test,conformance]"        # the official SDK as a test oracle
.venv/bin/python -m pytest tests/test_conformance_sdk.py -q
```

The Postgres tests run only when `NUVEL_MCP_TEST_DATABASE_URL` points at a throwaway
database; otherwise they skip. CI (`.github/workflows/tests.yml`) runs the suite on Python
3.11 to 3.13, a clean `pip install .` that starts the server over stdio and HTTP, and the
Postgres suites. Run the suite before every commit.

## Rules

- **Standard library only at runtime.** `dependencies = []` in `pyproject.toml`, and
  `tests/test_packaging.py` checks the built wheel. An optional feature imports its driver
  lazily, only when configured, and declares it as an extra (`postgres`, `org-memory`).
- **Content-free means content-free.** Run traces (`nuvel.skill_run/1`), UI events
  (`nuvel.ui_event/1`) and recipe signals carry identifiers and counts, never user content.
  The usage log never stores IP addresses or tool arguments. Validation errors name a JSON
  pointer, never the offending value. A change that would store or echo user text is a
  design change: stop and ask.
- **Task results are the one store of user content** (decided for phase 3): a task's
  result is a tool's output, kept only until its TTL (`NUVEL_MCP_TASK_TTL`, 24 hours by
  default), returned only to the tenant that created it (anyone else gets the same
  `-32602` as an unknown id), never logged and never relayed. Task files are `0600`.
  Keep it that way; widening any of it is a design change.
- **Schemas are versioned files.** Each payload's shape is a JSON Schema in
  `src/nuvel_mcp/schemas/` (`<name>.v<N>.schema.json`), read by a small validator that
  refuses keywords it doesn't support. A breaking change is a new version, not an edit.
- **The masker fails closed.** `masking.py` drops a field rather than return it unmasked.
  The held-out masking corpora (`tests/test_masking_holdout*_format.py` and their fixtures)
  were written blind: never read them to tune the masker or the lexicons. Lexicons are built
  by `scripts/build_lexicon.py` from permissively licensed sources listed in
  `scripts/lexicon_src/sources.json`; downloaded sources stay outside the repo (`--cache`).
- **HTTP is secure by default.** Loopback bind, OAuth required, `Origin` checked, every
  limit capped (`NUVEL_MCP_MAX_*`, deadlines). Don't loosen a default to make a test pass;
  tests use the explicit flags (`--no-auth`, `NUVEL_OAUTH_DEV_LOGIN=1`) on loopback.
- **Docs move with the code.** A new flag, tool, environment variable or schema goes into
  `README.md` and under `[Unreleased]` in `CHANGELOG.md` (Keep a Changelog) in the same PR.

## Git and releases

- Never push to `main`; open a PR. Commits use conventional prefixes (`feat:`, `fix:`,
  `docs:`, `test:`, `chore(release):`).
- Not on PyPI yet: a git tag is the release. Follow "Releasing" in `README.md` (version in
  `pyproject.toml`, `CHANGELOG.md` section, merge, then tag `vX.Y.Z` on the merged commit).
- This repository is public: commits, PRs and the changelog mention nothing from private
  projects.

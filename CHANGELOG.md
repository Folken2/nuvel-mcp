# Changelog

All notable changes to `nuvel-mcp` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

`nuvel-mcp` has not been published yet: the version in `pyproject.toml` is
`0.5.0.dev0`, and 0.5.0 will be its first release. Until 0.4.0 the server
shipped inside `nuvel-cli` as `nuvel.mcp`; that history is in the
[`nuvel` changelog](https://github.com/Folken2/nuvel/blob/main/CHANGELOG.md).
From here on the package is versioned and released on its own.

## [Unreleased]

### Added

- `nuvel-mcp` as its own dependency-free distribution and repository, with
  the `nuvel-mcp` console script (`serve`, `client`, `token`, `db`, `clients`,
  `report`) and `python -m nuvel_mcp`.
- Optional extras declaring the lazily imported drivers: `postgres`
  (`psycopg[binary,pool]`), `org-memory` (`nuvel-cli`) and `test` (`pytest`).
- HTTP transport: Streamable HTTP on `/mcp`, legacy HTTP+SSE, `GET /healthz`,
  OAuth 2.1 (its own authorization server, `client_credentials`, URL keys),
  per-connection, per-session and per-principal limits.
- Postgres storage for OAuth, run traces, recipes and a per-request usage log
  (`NUVEL_MCP_DATABASE_URL`), with `db migrate|status|import`.
- Usage insights: client app and optional country per request, never an IP
  address or tool arguments; `clients label|list` (and `client create
  --person/--company`) and `report`.
- `record_run_trace`, `record_recipe_signal` and `share_recipe` tools, with
  server-side masking of recipe free text; `check_skill_health` gains a
  `runs` block.
- Feedback-only mode (`serve` without `--skills-dir`) and the `--tools`
  allow-list.
- CI on Python 3.11–3.13, a clean-install smoke test and the Postgres suites.

### Changed

- `serverInfo.version` in the `initialize` response is the installed
  `nuvel-mcp` version.

### Deprecated

- The `nuvel.mcp` import path in `nuvel-cli`; import `nuvel_mcp` instead.

### Fixed

- The run-trace and recipe day-file index trusted an unchanged ctime as proof
  the file was untouched. A rewrite in place within the same timestamp tick
  (1 ms on ext4) kept the ctime, so the index missed it and could accept a
  duplicate or reject a new run. A snapshot is now trusted on ctime only once
  it is older than any filesystem's timestamp granularity, and the size
  must match as well.

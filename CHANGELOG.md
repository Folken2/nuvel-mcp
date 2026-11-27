# Changelog

All notable changes to `nuvel-mcp` are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

0.5.0 is the first release of `nuvel-mcp` as its own package. Until 0.4.0
the server shipped inside `nuvel-cli` as `nuvel.mcp`; that history is in the
[`nuvel` changelog](https://github.com/Folken2/nuvel/blob/main/CHANGELOG.md).
From here on the package is versioned and released on its own.

## [Unreleased]

### Added

- `subscriptions/listen` for 2026-07-28 clients: `resourcesListChanged` and
  `resourceSubscriptions` for skill files (up to 256 URIs), found by polling
  the hub every `NUVEL_MCP_WATCH_INTERVAL` seconds (default 2). The
  acknowledgment names what is honored; tools and prompts are never promised.
  Over HTTP the stream is an SSE response under the per-principal stream cap,
  closed by the client to cancel, and ended by the server with the listen
  result on shutdown; on stdio, `notifications/cancelled` ends one.
- `server/discover` declares `resources: {"listChanged": true, "subscribe":
  true}` when a hub is served. `initialize` doesn't: legacy sessions get no
  change notifications.

### Added

- The MCP Skills extension (`io.modelcontextprotocol/skills`, with
  `directoryRead`): `skills/list` (paginated), `skills/get` and
  `resources/directory/read`, and every file of a skill readable at
  `skill://<theme>/<name>/<file-path>`. Each entry carries the skill's
  frontmatter as JSON and a manifest of its files with `sha256:` digests and
  sizes. A skill whose frontmatter falls outside the strict YAML subset, whose
  name differs from the index or breaks the Agent Skills rules, that has no
  `description`, or that exceeds 512 files or 16 MiB is left out of the
  listing, and the log says why.
- `nuvel_mcp.frontmatter`: a strict SKILL.md frontmatter reader that refuses
  what YAML 1.1 and 1.2 parsers read differently. The `conformance` extra adds
  PyYAML to check it.

### Changed

- `resources/list` names each skill's `SKILL.md` as
  `skill://<theme>/<name>/SKILL.md`. The earlier `skill://<theme>/<name>` still
  reads it but is no longer listed.

### Added

- MCP **2026-07-28**, alongside 2024-11-05 to 2025-11-25 on the same stdio
  process and `/mcp` endpoint. A request carrying the per-request `_meta`
  envelope is served statelessly: `server/discover`, `resultType` on every
  result, the server's identity in result `_meta`, and `ttlMs` / `cacheScope`
  on discover, list and read results. Unsupported versions get `-32022`, the
  removed methods (`ping`, `logging/setLevel`, resource subscriptions) `-32601`.
- Over HTTP, 2026-07-28 requests need no session; `MCP-Protocol-Version`,
  `Mcp-Method` and `Mcp-Name` are checked against the body (`400`, `-32020`),
  and the status follows the error code (`404` for an unknown method).
- Multi round-trip requests in the kernel (`nuvel_mcp.protocol.mrtr`): a tool
  can ask the client for input with `input_required`; `requestState` is sealed
  with HMAC-SHA256 and bound to the caller, the call and an expiry.
  `NUVEL_MCP_STATE_KEY` sets the key (no tool asks for input yet).
- `conformance` extra and `tests/test_conformance_sdk.py`: the official MCP
  SDK's client drives the server in both eras over stdio and HTTP, in its own
  CI job.

### Changed

- `initialize` answers with the client's protocol version when it is one we
  speak (2024-11-05 to 2025-11-25), else `2025-11-25`; it used to answer
  `2024-11-05` whatever was asked. Capabilities are now the spec's shape
  (`{"resources": {}, "tools": {}}`).
- `tools/list` is sorted by name.

## [0.6.0] - 2026-11-24

### Added

- `record_ui_event`: records one content-free `nuvel.ui_event/1` page visit on
  a plugin surface (today only `dashboard`), with which catalogued events
  happened and how often (at most 50 identifier keys, counts 0 to 10000), the
  visit's duration and the optional time to first action. Closed schema,
  rejections return field paths only, the server stamps `tenant`, and
  re-sending a `run_id` returns `duplicate`. On by default over HTTP.
- `check_skill_health` with `plugin` and a surface as `skill_name`
  (`"dashboard"`) adds a `ui` block: page visits in the last 30 days and the
  summed event counts, scoped to the caller's tenant.
- Storage follows the trace store: Postgres table `ui_events` (migration
  `0004_ui_events`, row-level security forced), else JSONL under
  `<feedback-dir>/_ui_events/`. `db import` copies those files in, and the relay
  sends them as `{"type": "ui_event"}`.

## [0.5.0] - 2026-11-04

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

[Unreleased]: https://github.com/Folken2/nuvel-mcp/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/Folken2/nuvel-mcp/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Folken2/nuvel-mcp/releases/tag/v0.5.0

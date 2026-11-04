# nuvel-mcp

A dependency-free [MCP](https://modelcontextprotocol.io) server that exposes a
Nuvel skills hub (a directory of `SKILL.md` skills) to any MCP client, over
stdio or HTTP. It also records skill feedback, content-free run traces and
shared recipes. The server needs nothing beyond the Python standard library
(3.11+); Postgres storage is an optional extra.

## Install

`nuvel-mcp` is not on PyPI yet; install it from this repository:

```bash
pip install "git+https://github.com/Folken2/nuvel-mcp"

# or from a clone
git clone https://github.com/Folken2/nuvel-mcp && cd nuvel-mcp
pip install .

# or run it once without installing
uvx --from "git+https://github.com/Folken2/nuvel-mcp" nuvel-mcp serve --help
```

Optional extras (`pip install ".[postgres]"` from a clone):

| Extra | Installs | For |
|---|---|---|
| `postgres` | `psycopg[binary,pool]` | `NUVEL_MCP_DATABASE_URL` (Postgres storage) |
| `org-memory` | `nuvel-cli` | `serve --with-org-memory` |
| `test` | `pytest` | running the test suite |

## Run

The package installs one console script, `nuvel-mcp` (`python -m nuvel_mcp`
is the same). An MCP client normally starts the server over stdio:

```bash
nuvel-mcp serve --skills-dir /path/to/skills-hub
```

The hub is a directory with an `index.json` and `<theme>/<name>/SKILL.md`
files, or a repository root with them under `skills/`. Without
`--skills-dir` the server starts in feedback-only mode. A client configuration
looks like:

```json
{"mcpServers": {"nuvel": {"command": "nuvel-mcp",
                          "args": ["serve", "--skills-dir", "/path/to/skills-hub"]}}}
```

Run `nuvel-mcp serve --help` for all flags (`--theme`, `--require`,
`--with-org-memory`, `--feedback-dir`, `--tools`, `--trace-store`,
`--recipe-store`, `--transport`, `--host`, `--port`, `--allow-remote`,
`--no-auth`, `--require-auth`). The other subcommands are `client`, `token`,
`db`, `clients` and `report`; `nuvel-mcp --help` lists them.

### Over HTTP

The HTTP transport (Streamable HTTP on `/mcp`, legacy SSE, OAuth 2.1) is part of
the same dependency-free package. It binds `127.0.0.1:8080` and requires an
OAuth token unless you pass `--no-auth` on a loopback bind. It also needs a
login provider: GitHub (`NUVEL_OAUTH_GITHUB_CLIENT_ID` and
`NUVEL_OAUTH_GITHUB_CLIENT_SECRET`) for interactive sign-in, or
`NUVEL_OAUTH_LOGIN=none` to accept only operator-issued clients. Create a
client for a trace-recording workload and serve:

```bash
nuvel-mcp client create --name acme-radar
NUVEL_OAUTH_LOGIN=none nuvel-mcp serve --transport http --skills-dir /path/to/skills-hub
```

`client create` prints a `client_id` and a `client_secret` (shown once). A
workload exchanges them for an access token with the `client_credentials` grant,
then sends it as a bearer token to `/mcp`. On the default loopback bind the
server is `http://127.0.0.1:8080`; for a remote deployment use its public URL,
`$NUVEL_MCP_PUBLIC_URL`, instead:

```bash
curl -s -u "$NUVEL_CLIENT_ID:$NUVEL_CLIENT_SECRET" \
  -d grant_type=client_credentials "http://127.0.0.1:8080/token"
# -> {"access_token": "...", ...}; call /mcp with
#    Authorization: Bearer $ACCESS_TOKEN
```

For an MCP client that can only be given a URL (a Claude custom connector with
no sign-in), `nuvel-mcp client create --name alice --url-key` prints a
per-client `connector_url` (`$NUVEL_MCP_PUBLIC_URL/mcp/k/<key>`) once; add
`--expires-in DAYS` for a key that stops working after that long. To rotate a
key, issue a new one, hand it out, then `client delete` the old client.

Over HTTP the default tools are the hub read tools plus `record_run_trace`,
`check_skill_health`, `record_recipe_signal` and `share_recipe`; `record_feedback`, `propose_improvement` and
`install_skill` are exposed only when named in `--tools`. The tenant of each
run trace is the OAuth `client_id`.

The `Dockerfile` in this repository builds an HTTP server image for container
platforms. The [deployment guide](https://folken2.github.io/nuvel/how-to/deploy-skills-mcp/)
covers TLS, scopes, the interactive flow and what is exposed over HTTP.

### Storage and usage insights

State lives in files by default. Set `NUVEL_MCP_DATABASE_URL` (for example a
Neon pooled connection string) and OAuth, run traces, recipes and a per-request
usage log all go to Postgres instead; that needs the `postgres` extra, and the
package itself stays dependency-free. The schema is migrated at startup;
`nuvel-mcp db migrate|status|import` manage it, and `db import` copies existing
files in. See
[Store everything in Postgres](https://folken2.github.io/nuvel/how-to/deploy-skills-mcp/#store-everything-in-postgres-neon).

With a database, each request is logged for the operator: when, which OAuth
client, method and tool name (known names only), outcome, error code, latency,
the session's client app (`initialize` `clientInfo` name and version, sanitized
to 64 safe characters) and, optionally, the caller's country. IP addresses,
tool arguments and any other content are never stored. The country needs
`NUVEL_MCP_GEOIP_CSV` pointing at the DB-IP "IP to Country Lite" CSV, which you
download yourself (CC BY 4.0: credit "IP Geolocation by DB-IP"); turn it off
with `NUVEL_MCP_RECORD_COUNTRY=0`. `nuvel-mcp clients label|list` records who is
behind a client id, and `nuvel-mcp report [--days 7] [--json]` prints the usage
report. Nothing is pruned automatically: delete old `usage_events` rows with SQL.
See [Usage insights](https://folken2.github.io/nuvel/how-to/deploy-skills-mcp/#usage-insights-what-is-collected)
for exactly what is collected and retention.

## Environment variables

All are optional. Command-line flags, where they exist, take precedence.

| Variable | Default | Meaning |
|---|---|---|
| `NUVEL_FEEDBACK_DIR` | `~/.nuvel/feedback` | Local feedback store (feedback, queued proposals, run traces, recipes); `--feedback-dir` overrides it. |
| `NUVEL_FEEDBACK_URL` | unset | Relay URL that run traces, recipe signals and recipes are also sent to, and that `propose_improvement` uses when it cannot file a GitHub issue. |
| `GITHUB_TOKEN` | unset | Lets `propose_improvement` file a GitHub issue. |
| `NUVEL_SKILLS_INSTALL_DIR` | `~/.claude/skills` | Target directory for `install_skill`. |
| `NUVEL_MCP_APP_NAME`, `NUVEL_MCP_USER_ID` | `nuvel-skills`, `default` | App name and user id for `--with-org-memory`. |
| `NUVEL_MCP_DATABASE_URL` | unset | Postgres connection string; moves every store to Postgres. |
| `NUVEL_MCP_DATABASE_SCHEMA` | `nuvel_mcp` | Postgres schema. |
| `NUVEL_MCP_DATABASE_POOL_MAX` | `10` | Connection pool size. |
| `NUVEL_MCP_DATABASE_AUTO_MIGRATE` | `1` | Migrate the schema at startup. |
| `NUVEL_MCP_USAGE` | `1` | Write the per-request usage log (database only). |
| `NUVEL_MCP_GEOIP_CSV` | unset | DB-IP country CSV for the usage log's country. |
| `NUVEL_MCP_RECORD_COUNTRY` | `1` | `0` stops recording the country. |
| `NUVEL_MCP_OAUTH_STORE` | `~/.nuvel/oauth.json` | OAuth store file (without a database). |
| `NUVEL_MCP_PUBLIC_URL` | unset | Public base URL of a remote HTTP server (OAuth resource and issuer). |
| `NUVEL_MCP_ACCESS_TOKEN` | unset | Bearer token the stdio server checks with `--require-auth`. |
| `NUVEL_OAUTH_LOGIN` | GitHub login | `none` disables interactive login (operator-issued clients only). |
| `NUVEL_OAUTH_GITHUB_CLIENT_ID`, `NUVEL_OAUTH_GITHUB_CLIENT_SECRET` | unset | GitHub OAuth app for the interactive login. |
| `NUVEL_OAUTH_ALLOWED_USERS` | unset | Comma-separated GitHub users allowed to sign in. |
| `NUVEL_OAUTH_REDIRECT_ALLOWLIST` | unset | Comma-separated extra redirect URIs accepted at registration. |
| `NUVEL_OAUTH_DEV_LOGIN` | unset | `1` enables a local development login page (not for production). |
| `NUVEL_MCP_ALLOWED_ORIGINS` | unset | Comma-separated extra `Origin` values the HTTP transport accepts. |
| `NUVEL_MCP_TRUST_FORWARDED_FOR` | unset | `1` takes the caller's IP from `X-Forwarded-For` (only behind a proxy that appends it). |
| `NUVEL_MCP_MAX_BODY_BYTES` | `1048576` | Request body cap. |
| `NUVEL_MCP_MAX_CONNECTIONS` | `128` | Concurrent connections; excess gets `503`. |
| `NUVEL_MCP_MAX_CONNECTIONS_PER_IP` | `16` | Concurrent connections per client IP. |
| `NUVEL_MCP_MAX_SESSIONS` | `64` | Open MCP sessions. |
| `NUVEL_MCP_MAX_SESSIONS_PER_PRINCIPAL` | `8` | Open sessions per OAuth principal. |
| `NUVEL_MCP_MAX_STREAMS_PER_PRINCIPAL` | `4` | Open SSE streams per OAuth principal. |
| `NUVEL_MCP_SESSION_TTL` | `3600` | Idle session lifetime, seconds. |
| `NUVEL_MCP_READ_TIMEOUT` | `30` | Socket read and keep-alive idle timeout, seconds. |
| `NUVEL_MCP_REQUEST_DEADLINE` | `30` | Total time to receive one request, seconds. |
| `NUVEL_MCP_HEADER_DEADLINE` | `5` | Time to receive the request headers, seconds. |

Invalid HTTP limits stop the server at startup.

## Relationship to the `nuvel` CLI

[`nuvel-cli`](https://github.com/Folken2/nuvel) (the `nuvel` command) depends
on this package: `nuvel mcp serve` delegates to `nuvel-mcp serve`, and the old
`nuvel.mcp` import path is a deprecated shim, so import `nuvel_mcp` instead.
`nuvel-mcp` does not depend on the CLI; it is only needed for
`--with-org-memory` (the `org-memory` extra). The two are versioned and
released separately; see [CHANGELOG.md](CHANGELOG.md).

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest tests/ -q
```

The Postgres tests run when `NUVEL_MCP_TEST_DATABASE_URL` points at a
throwaway database (and the `postgres` extra is installed); otherwise they are
skipped. CI (`.github/workflows/tests.yml`) runs the suite on Python 3.11–3.13,
a clean-install smoke test and the Postgres suites. The recorded clean-install
and CI results are in [docs/verification.md](docs/verification.md).

## Releasing

`nuvel-mcp` is not on PyPI yet, so a git tag is the release: `nuvel` pins
`nuvel-mcp @ git+https://github.com/Folken2/nuvel-mcp@vX.Y.Z`.

1. Set `version = "X.Y.Z"` in `pyproject.toml` (no `.devN` suffix).
2. In `CHANGELOG.md`, rename `[Unreleased]` to `[X.Y.Z] - YYYY-MM-DD`, add a
   new empty `[Unreleased]` section above it and update the links at the end.
3. Merge to `main`, then tag the merged commit and push the tag:

   ```bash
   git tag -a vX.Y.Z -m "nuvel-mcp X.Y.Z"
   git push origin vX.Y.Z
   ```

The tag must point at a commit whose `pyproject.toml` declares `X.Y.Z`;
otherwise the installed distribution reports a different version.

## License

MIT; see [LICENSE](LICENSE).

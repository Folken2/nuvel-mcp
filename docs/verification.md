# Standalone verification (#1)

The evidence that this repository installs and tests on its own, recorded on
2026-11-04 for the commit that adds this file. Every run below used a fresh
`git clone` of that tree (identical apart from this file) and a fresh venv,
so nothing outside the repository could be picked up.

## Clean install: `pip install .` in an empty venv

Python 3.12.3, run from outside the clone:

```console
$ python3.12 -m venv venv
$ venv/bin/pip list
Package Version
------- -------
pip     24.0
$ venv/bin/pip install ./nuvel-mcp
Processing ./nuvel-mcp
Building wheels for collected packages: nuvel-mcp
Successfully built nuvel-mcp
Installing collected packages: nuvel-mcp
Successfully installed nuvel-mcp-0.5.0.dev0
$ venv/bin/pip list
Package   Version
--------- ----------
nuvel-mcp 0.5.0.dev0
pip       24.0
$ export NUVEL_FEEDBACK_DIR=/tmp/proof/feedback
$ venv/bin/nuvel-mcp --help | head -3
usage: nuvel-mcp [-h] {serve,client,token,db,clients,report} ...

Nuvel Skills MCP server.
```

The server starts over stdio and answers `initialize` and `tools/list`:

```console
$ printf '%s\n' \
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"proof","version":"0"}}}' \
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
    | timeout 30 venv/bin/nuvel-mcp serve > out.jsonl
Storage: files under /tmp/proof/feedback.
nuvel-skills MCP server starting (skills_dir=none (feedback-only))
nuvel-skills MCP server: EOF on stdin, shutting down.
$ venv/bin/python -c 'import json; r=[json.loads(l) for l in open("out.jsonl")]; print("initialize ->", r[0]["result"]["serverInfo"]["name"], r[0]["result"]["serverInfo"]["version"]); print("tools/list ->", [t["name"] for t in r[1]["result"]["tools"]])'
initialize -> nuvel-skills 0.5.0.dev0
tools/list -> ['propose_improvement', 'record_feedback', 'check_skill_health', 'record_run_trace', 'record_recipe_signal', 'share_recipe']
```

And over HTTP:

```console
$ NUVEL_MCP_OAUTH_STORE=/tmp/proof/oauth.json venv/bin/nuvel-mcp serve --transport http --no-auth --port 8765 &
$ curl -fsS http://127.0.0.1:8765/healthz
{"status": "ok", "transport": "http", "storage": "files", "skills": 0, "themes": 0, "tools": 4}
```

## CI: `.github/workflows/tests.yml`

The workflow runs on pushes to `main` and on pull requests against `main`. It
had not run on GitHub when this was recorded: the branch had not been pushed
and no pull request had been opened. Each job's steps were therefore replayed
locally, each in a fresh clone and venv:

| Job | Interpreter | Result |
| --- | --- | --- |
| `test` (3 runs per version, the three versions concurrently) | 3.11.16 | 1573 passed, 46 skipped, 2 xfailed — ×3 |
| | 3.12.3 | 1573 passed, 46 skipped, 2 xfailed — ×3 |
| | 3.13.15 | 1573 passed, 46 skipped, 2 xfailed — ×3 |
| `clean-install` | 3.12.3 | the transcript above |
| `postgres`: suite | 3.12.3, PostgreSQL 16.15 (UTF8) | 1616 passed, 3 skipped, 2 xfailed |
| `postgres`: `NUVEL_MCP_TEST_OAUTH_STORE=postgres` | 3.12.3, PostgreSQL 16.15 (UTF8) | 1601 passed, 18 skipped, 2 xfailed |

The local Postgres was 16, not CI's 17. The database must be UTF8, which is the
`postgres` image default: on a `SQL_ASCII` cluster psycopg returns text as
`bytes` and 16 tests fail.

Under concurrent load the first `test` replay hit a flake,
`test_mcp_http.py::test_rejected_second_stream_releases_its_connection_slot`
(503 where 409 was expected, about 1 run in 17 in isolation). The server frees
a connection slot in the handler thread *after* writing the reply, so the
client could send its next request first. The test now waits for the slot,
bounded by its timeout, before each request. It passed 300 of 300 isolated runs
and still fails if the slot release is removed. The table above was recorded
after that fix.

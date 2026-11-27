# Design: the nuvel-mcp foundation

Status: proposed, 2026-11-27. Target protocol: MCP **2026-07-28**, while still serving
2025-11-25 clients.

This document says how nuvel-mcp grows from a skills hub server into the platform for
skills, interactive apps, plugins and background agents served over MCP. It fixes the
decisions, the layers, the wire details we depend on, and the order of work. Anything
marked **verify** was not confirmed against the specification text and has to be checked
before it is built.

## 1. Where we are

- `server.py` answers `initialize` with `protocolVersion: "2024-11-05"`
  (`PROTOCOL_VERSION`), whatever version the client asked for. One class
  (`SkillsMCPServer`, about 1,900 lines) holds the dispatcher, every tool definition and
  every tool body.
- `http_transport.py` serves Streamable HTTP on `/mcp` with `Mcp-Session-Id` sessions, a
  GET stream and DELETE, plus the 2024-11-05 HTTP+SSE transport (`/sse`, `/messages`).
  Sessions, their outboxes and the stream slots are in-process state, so one process
  serves one deployment.
- Skills are resources at `skill://{theme}/{name}` (`text/markdown`), and the hub tools
  (`search_skills`, `list_skills`, `list_themes`, `get_skill`, `install_skill`,
  `fence_value`) wrap the same index.
- The feedback tools record content-free run traces, UI events and recipes, and each has
  a versioned JSON Schema. The stores are files or Postgres.
- The server is also its own OAuth 2.1 authorization server: dynamic registration,
  GitHub login, client credentials, URL keys. The usage log reads the client app from
  `initialize`'s `clientInfo`, once per session.

All of that keeps working. What changes is the protocol layer under it and how
capabilities are added.

## 2. What 2026-07-28 changes for us

The revision removes protocol sessions. These are the parts that touch this server:

| Area | 2026-07-28 rule | Effect here |
|---|---|---|
| Handshake | No `initialize` and no `notifications/initialized`. Each request carries `params._meta["io.modelcontextprotocol/protocolVersion"]` and `.../clientCapabilities` (required), and `.../clientInfo` (should). Results should carry `.../serverInfo`. | Capabilities and the client app are read per request. |
| Discovery | `server/discover` is required. It returns the server's capabilities and `capabilities.extensions`. | A new method. |
| Results | Every result has `resultType`: `"complete"`, `"input_required"` or (with Tasks) `"task"`. | The kernel adds it. |
| Sessions | No `Mcp-Session-Id`. List results don't vary per connection. | Modern HTTP holds no state, so it can scale out. |
| Server → client | No server-initiated requests. Elicitation, sampling and roots go through multi round-trip requests (below). | Removes the need for the GET stream. |
| HTTP | `Mcp-Method` on every POST. `Mcp-Name` for `tools/call`, `prompts/get` and `resources/read`. `MCP-Protocol-Version` must match `_meta`. A mismatch gets 400 with `-32020`. GET and DELETE get 405. No resumable streams. Closing the response stream cancels the request. | The transport checks the headers. |
| Notifications | `subscriptions/listen` is a POST whose response is a long-lived SSE stream. It replaces the GET stream and `resources/subscribe`. | A new method. |
| Caching | `ttlMs` and `cacheScope` (`"public"` or `"private"`) are required on discover, list, read and template-list results. | The kernel adds them. |
| Removed | `ping`, `logging/setLevel`, the roots and elicitation-complete notifications. | Drop the handlers on the modern path. |
| Deprecated | Roots, sampling and logging (SEP-2577). The 2024-11-05 HTTP+SSE transport. Dynamic client registration, in favour of Client ID Metadata Documents. | Plan to remove them; don't build on them. |
| Errors | Resource not found becomes `-32602`. `-32000` to `-32019` are ours, `-32020` to `-32099` belong to the spec. `-32020` HeaderMismatch, `-32021` MissingRequiredClientCapability, `-32022` UnsupportedProtocolVersion (`data.supported`). | Pick our codes from `-32000` to `-32019`. |

Multi round-trip requests (MRTR, SEP-2322) work only for `tools/call`, `prompts/get` and
`resources/read`. The server answers `resultType: "input_required"` with `inputRequests`
(a map of key to `elicitation/create`, `sampling/createMessage` or `roots/list` request)
and an opaque `requestState`. The client retries the call with a new JSON-RPC id,
`inputResponses` under the same keys, and the same `requestState`. A server must not ask
for a kind of input the client did not declare. If `requestState` affects authorization
or business logic, the server must protect its integrity.

Extensions are named `{reversed-domain}/{name}`. A client declares them per request under
`clientCapabilities.extensions`, and the server declares them in `server/discover`. They
are off unless both sides declare them, and each one defines a fallback.

## 3. Decisions

**D1. The runtime stays standard library only.** The official Python SDK v2 (2.3.0)
serves both revisions, but it brings pydantic, anyio, starlette, sse-starlette, httpx2
and the OpenTelemetry API. The no-dependency rule is why the server installs anywhere
and passes `test_packaging.py`, so we keep it and write the kernel ourselves. We pay for
that with a conformance burden, which D6 covers.

**D2. One endpoint serves both revisions.** The way a request opens selects its era:
`initialize` means legacy (2025-11-25, session-scoped), and a request with the modern
`_meta` envelope means modern (stateless). Both run through one kernel. The legacy path
is an adapter around it, not a second implementation of each method. The 2024-11-05
HTTP+SSE transport stays until the first release after phase 2, then goes.

**D3. Each capability is an extension module.** Skills, Apps, Tasks and Plugins are each
one module that registers its methods, its resources, its tools and its entry in
`server/discover`. `server.py` stops growing: its tool bodies move into those modules,
and it becomes the wiring.

**D4. One gateway mounts plugins.** A deployment is one nuvel-mcp process (or several
identical stateless replicas) that mounts any number of plugins, each under its own
namespace. We don't run one MCP server per plugin. That keeps one auth, one usage log
and one place to apply limits.

**D5. Agents run outside nuvel-mcp, behind Tasks.** nuvel-mcp is the front door: it
accepts the call, owns the task record, applies auth and limits, and reports status.
The agent loop (models, keys, tools, sandbox) runs in a separate agent runtime, such as
an ADK agent generated by `nuvel agent create`. The Tasks extension is the contract
toward the MCP client. A small executor interface (section 6.4) is the contract toward
the runtime. Model keys never enter this package.

**D6. Two checks of spec conformance.** Our own tests pin the wire format from the
specification text. Separately, a test-only extra, `conformance` with `mcp>=2.3,<3`,
runs the official SDK's `Client` against our server on both revisions, as an
independent check. The runtime stays dependency-free, and `test_packaging.py` keeps
checking that. No official conformance suite was found; if one appears, it joins this
check.

## 4. Layers

```
 transports        stdio            HTTP (modern)            HTTP (legacy adapter)
                     │      headers, Origin, auth, limits      Mcp-Session-Id, GET, SSE
                     └───────────────┬──────────────────────────────┘
 kernel            envelope ─ era ─ discover ─ dispatch ─ resultType/cache ─ MRTR state ─ errors
                                     │
 registry          methods · tools · resources · resource templates · prompts · extensions
                                     │
 extensions        skills       apps (ui)       tasks          plugins (ours)
                     │             │              │                 │
 domain            hub loader   ui:// bundles   task store ─► executors ─► agent runtime
                   feedback · traces · ui events · recipes · usage · masking (unchanged)
                                     │
 storage           files | Postgres (as today)
```

Proposed module layout. These are new modules; the existing ones move only when a phase
needs them to.

```
src/nuvel_mcp/
  protocol/
    envelope.py      _meta parsing: version, capabilities, client info, log level, trace keys
    versions.py      supported revisions, era detection, UnsupportedProtocolVersion
    kernel.py        dispatch, resultType, cache fields, error mapping, serverInfo
    mrtr.py          input_required results, sealed requestState
    registry.py      what extensions and features register; builds server/discover
    errors.py        the code table, spec and ours
    subscriptions.py subscriptions/listen
  transports/
    stdio.py
    http_modern.py   header validation, 405, SSE response streams
    http_legacy.py   today's sessions, moved here
  extensions/
    skills.py        io.modelcontextprotocol/skills
    apps.py          io.modelcontextprotocol/ui
    tasks.py         io.modelcontextprotocol/tasks
    plugins.py       io.github.folken2/plugins
  tasks/
    store.py         file and Postgres task stores
    executors.py     the executor interface and the built-in executors
```

`http_transport.py` keeps its HTTP server, OAuth routes, limits and Origin checks.
It hands each MCP POST to one of the two transport handlers.

## 5. The protocol kernel

### 5.1 Request envelope and era

- A request whose `params._meta` has `io.modelcontextprotocol/protocolVersion` is
  modern. The kernel requires `clientCapabilities` on it, then checks the version against
  `versions.MODERN` (today `["2026-07-28"]`). An unknown version gets
  `UnsupportedProtocolVersion` (`-32022`, `data.supported`). Over HTTP that is status
  400.
- `initialize` is legacy. The adapter negotiates as 2025-11-25 does: the client's
  version if we support it, otherwise our latest legacy version (`2025-11-25`). This
  fixes today's bug, where every client is answered `2024-11-05` whatever it asked for.
  The capabilities from `initialize` are stored on the session, and the kernel sees them
  exactly as if they had come in `_meta`.
- Any other request with neither envelope nor session is an error that names the
  supported versions, which is how a client tells the two eras apart.
- Each handler receives a `RequestContext` (era, version, client capabilities,
  extensions declared, client info, principal, log level, trace keys) and never reads
  the raw `_meta`.

### 5.2 `server/discover`

It is built from the registry, so a new extension appears there without anyone editing
the method:

```json
{"resultType": "complete",
 "supportedVersions": ["2026-07-28"],
 "capabilities": {
   "tools": {"listChanged": true}, "resources": {"listChanged": true}, "prompts": {},
   "extensions": {
     "io.modelcontextprotocol/skills": {"directoryRead": true},
     "io.modelcontextprotocol/ui": {},
     "io.modelcontextprotocol/tasks": {},
     "io.github.folken2/plugins": {"version": 1}}},
 "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "nuvel-skills", "version": "0.7.0"}},
 "ttlMs": 300000, "cacheScope": "private"}
```

Checked against the 2026-07-28 schema (`DiscoverResult`): `supportedVersions`,
`capabilities` and an optional `instructions`; `serverInfo` goes in result `_meta`.
`supportedVersions` lists the modern versions only, as the official SDK does: a legacy
version can't be named in a request envelope, and a legacy client opens with
`initialize` anyway. The `-32022` error's `data.supported` lists the same.

### 5.3 Results, caching and errors

- The kernel adds `resultType: "complete"` to every result that lacks one, and
  `serverInfo` in result `_meta` on the modern path.
- Cache fields come from the registry, per method. `tools/list`, `resources/list` and
  `prompts/list` use `cacheScope: "private"` whenever auth is on, because what a caller
  sees depends on the token's scopes and tenant. They use `"public"` only for an
  unauthenticated loopback hub. The default `ttlMs` is 5 minutes for lists and 1 hour
  for a skill file whose digest is in the manifest.
- `tools/list` is sorted by name, as the spec now asks.
- Error codes: we move `Skill not found` to `-32602` (already the case) and keep any
  code of our own in `-32000` to `-32019`, listed in `protocol/errors.py`. The existing
  rule stays: an error message never echoes caller input.

### 5.4 MRTR and `requestState`

A tool that needs input returns `InputRequired(requests={...}, state={...})`, and the
kernel turns that into the wire result.

- **The state is sealed, not trusted.** `requestState` is
  `base64url(version || nonce || HMAC-SHA256(key, payload) || payload)`. The payload is
  JSON with the principal, the method, a SHA-256 of the canonical `name` + `arguments`,
  an expiry (default 10 minutes) and the tool's own state. It is signed, not encrypted,
  so a tool's state must hold identifiers, never user content. That is the same
  content-free rule the traces follow. On the retry, the kernel checks the MAC, the
  expiry, the principal and the call digest before the tool sees anything. A failure
  gets `-32602`, with a fixed message.
- **Key.** The key comes from a new variable, `NUVEL_MCP_STATE_KEY` (32 or more bytes,
  base64). Without it, the key is random per process, which is right for stdio and
  wrong for replicas. Over HTTP with `NUVEL_MCP_PUBLIC_URL` set, the server warns at
  start without the key; it will refuse to start once a shipped tool asks for input,
  so that today's deployments don't break on an upgrade that changes nothing for them. Rotation is a comma-separated list: the first key signs, any
  key verifies.
- **Capabilities are respected.** If the client did not declare `elicitation`, the tool
  must have a fallback, normally a tool error that says what is missing. The kernel
  refuses to emit an input request of a kind the client did not declare.
- **Legacy clients.** On a 2025-11-25 session, the adapter turns an `InputRequired`
  into a real `elicitation/create` request on the session stream, then resumes the tool
  with the answer. That keeps one way of writing tools for both eras. Until the first
  tool needs it, a legacy session gets an error naming 2026-07-28 instead.
- Sampling is deprecated, so no tool of ours will ask for it.

### 5.5 Subscriptions and notifications

`subscriptions/listen` opens an SSE response. Its first message is
`notifications/subscriptions/acknowledged`, followed by `tools/list_changed`,
`resources/list_changed`, `resources/updated` (for a skill file whose digest changed)
and, with Tasks, `notifications/tasks`. Each carries the subscription id, which is the
request's JSON-RPC id.

The existing stream limits (`NUVEL_MCP_MAX_STREAMS_PER_PRINCIPAL`, keep-alives, the
deadlines) apply to these streams. Built (`protocol/subscriptions.py`): every change this
server can report is a change to the hub's files, so a poller compares fingerprints
(the `resources/list` URIs, each subscribed file's digest) with what each subscription
saw when it opened, every `NUVEL_MCP_WATCH_INTERVAL` seconds. Each replica watches its
own copy of the hub, so no cross-replica feed is needed yet; `LISTEN/NOTIFY` becomes
necessary only for state that lives in Postgres, like tasks (6.3). The acknowledgment
never promises `toolsListChanged` or `promptsListChanged`: tools and prompts don't change
while the server runs.

Request-scoped notifications (`notifications/progress`, and `notifications/message` only
when `_meta` asks for a log level) go only on that request's own response stream.

## 6. Extensions

### 6.1 Skills: `io.modelcontextprotocol/skills`

The hub already is what the Skills extension (SEP-2640, beta) describes: a directory of
`SKILL.md` files with frontmatter. The work:

- **URIs.** `skill://{theme}/{name}/SKILL.md`, with supporting files under
  `skill://{theme}/{name}/...`. The extension wants the parent directory to match the
  skill's `name`, and this layout meets that. Today's `skill://{theme}/{name}` stays
  readable as an alias of the `SKILL.md`, so existing clients don't break, but it is no
  longer listed.
- **Methods.**
  - `skills/list` is paginated (`cursor`, `nextCursor`) and respects `--theme` and
    `--require`.
  - `skills/get` takes a `uri` and returns the entry: the frontmatter (`name`,
    `description`, the rest as is) and a manifest of every file, each with `uri`,
    `digest` (`sha256:...`) and `size`.
  - `resources/read` serves the files.
  - `resources/directory/read` lists a directory (`inode/directory`), with
    `directoryRead: true` in discover.
- **Frontmatter.** The entry carries the frontmatter "verbatim as a JSON object", and a
  host compares it field by field with what its own YAML parser reads, so the lossy
  frontmatter parser the hub tools use won't do. `nuvel_mcp.frontmatter` is a strict
  reader of the subset YAML 1.1 and 1.2 parsers agree on; a skill whose frontmatter
  falls outside it (a bare `yes`, `0755`, a date, an anchor, a multi-line plain value)
  is not published, and the log says which line. Checked against PyYAML on the
  `SKILL.md` files of the nuvel and nuvel-plugins repositories: 47 of 49 agree exactly,
  and the other two are refused because they are not valid YAML (a `: ` inside a plain
  `description`).
- **Publishing rules.** A skill is listed and answered by `skills/get` only when its
  frontmatter is accepted, its `name` equals the index name and follows the Agent Skills
  naming rules, it has a `description`, its theme is a valid URI authority, and it is
  within the limits (512 files, 16 MiB). Any other skill stays readable file by file.
  The log names each one once, with the reason.
- **Files.** A skill's files are every regular file under its directory except names
  starting with `.`, `__pycache__` and symbolic links; the manifest and `resources/read`
  share that list, so they can't disagree. Bytes are served as read (no newline
  translation), as text when they are UTF-8, else base64.
- **Digests** are computed on demand and cached per file by modification time and size,
  so a changed file gets a new digest on the next listing. `resources/updated`
  notifications come with `subscriptions/listen`, in a later PR.
- **Fallback.** Support among hosts is partial, so the hub tools stay and remain the
  default over HTTP. A client that declares the extension sees both. `get_skill` keeps
  its template variables and org-memory resolution; that is ours, not part of the
  extension.
- **Security.** Hosts treat skill content as untrusted, and so do we. The skills
  extension doesn't change what a hub may contain. `install_skill` stays off over HTTP.

### 6.2 Apps: `io.modelcontextprotocol/ui`

MCP Apps (SEP-1865) are stable. Claude, VS Code GitHub Copilot and Microsoft 365 Copilot
render them, among others.

- **Shape.**
  - A tool with a view names a `ui://` resource in `_meta.ui.resourceUri`.
  - The resource is HTML of type `text/html;profile=mcp-app`, with
    `_meta.ui.csp` and `_meta.ui.permissions` stating exactly what it needs.
  - Tools that only the view calls set `_meta.ui.visibility: ["app"]`. Those are hidden
    from the model, and a `tools/call` from the model is refused.
- **One bundle per view.** A view is a single self-contained HTML file (inline script
  and style) in the package or in a plugin's `apps/` directory. Its CSP is the default
  (`connect-src 'none'`) unless the bundle declares more. Every view reaches data only
  through `tools/call` over the host's `postMessage` bridge, which keeps the host's
  audit trail whole and means a view never needs our token.
- **Fallback.** Every tool with a view still returns a complete text and
  `structuredContent` result. The view is extra, never required.
- **Telemetry.** A view reports its visit through `record_ui_event` (app-only), which
  already exists and is content-free. A new `surface` value per view goes in the
  `ui_event` catalogue. Not done for the skill browser: `nuvel.ui_event/1` describes a
  plugin's surface (a plugin name and version, the plugin's event catalogue), and the
  browser belongs to the server, so it needs a schema decision first.
- **First view.** A skill browser: search and read, built on `get_skill` and one new
  read-only tool, `browse_skills`, which opens the view and returns the same matches as
  `structuredContent` and text. Built (`extensions/apps.py`, `apps/skill-browser.html`).
  Health is left out: over HTTP `check_skill_health` needs a plugin and reports runs, not
  hub skills. Views are not in `resources/list` (the extension allows that), so the list
  stays the skills. A view must not use `<form>`: hosts sandbox views without
  `allow-forms`, and Chromium then never fires `submit`. `tests/test_apps_view.py` runs
  the view in Chromium through a minimal host under the default CSP.
- **No HTML from tool results.** A view is a fixed, versioned resource. A tool never
  makes HTML out of user content.

### 6.3 Tasks: `io.modelcontextprotocol/tasks`

Tasks (SEP-2663) moved out of core into an extension. They are how a call outlives its
HTTP request, and so how a background agent is reached.

- **When.** A tool that may run long is registered with `task: "optional"` or
  `"required"` (our flag, not a wire field). When the client has declared the extension
  on the request, the kernel answers `tools/call` with `resultType: "task"`: `taskId`,
  `status: "working"`, `ttlMs` and `pollIntervalMs`. When the client hasn't, an optional
  task runs in the request, and a required one gets `-32021`. The server never returns a
  task to a client that didn't declare the extension.
- **Methods.**
  - `tasks/get` returns the status and, once finished, `result` or `error`.
  - `tasks/update` delivers `inputResponses` when the status is `input_required`, using
    the same `inputRequests` shape as MRTR.
  - `tasks/cancel` cancels.
  - Over HTTP, `Mcp-Name` carries the `taskId`.
  - Status changes also go out as `notifications/tasks` on `subscriptions/listen`.
- **Statuses.** `working`, `input_required`, `completed`, `failed` and `cancelled`.
  `failed` means a protocol-level error. A tool that reports `isError: true` ends as
  `completed`.
- **Store.** A task is a row: id, tenant, tool, status, timestamps, expiry, the result
  or error, and pending input requests. Files under `<feedback-dir>/_tasks/` or a
  Postgres table `tasks` (a new migration, with row-level security forced, like
  `ui_events`). A caller sees only its own tenant's tasks. An unknown id and another
  tenant's id get the same `-32602`.
- **Content.** A task's result is the tool's output and is returned to its owner only.
  It is not telemetry, so the content-free rule for traces doesn't apply to it. It is
  kept only until `ttlMs` (default 24 hours) and then deleted. It is never logged, and
  never relayed to `NUVEL_FEEDBACK_URL`. **verify** with you: this is the first store
  that holds user content, so it is the "design change" AGENTS.md says to stop and ask
  about.

### 6.4 Executors and agents

A task runs on an executor:

```python
class Executor(Protocol):
    def start(self, task: TaskRecord, call: ToolCall) -> None: ...
    def cancel(self, task_id: str) -> None: ...
```

- **`inline`**: a worker thread in this process, for our own long tools (a health
  report across many skills, a hub reindex). It needs nothing outside the package.
- **`remote`**: an HTTP POST to a configured agent runtime URL, with the call and a
  callback URL. The runtime posts status, input requests and the result back to
  `POST /tasks/<id>/events`, authenticated with an operator-issued client
  (`client_credentials`, its own scope `tasks:report`). A runtime can only report on
  tasks it was given. This is how an ADK agent generated by `nuvel` becomes a
  background agent behind MCP. Its loop, models and keys stay in the runtime.

The agent runtime protocol is ours until the MCP Agents Working Group publishes
something. We keep it small (start, cancel, report) so it can be swapped for a standard
later. Mounting an agent is a plugin's job (6.5): a plugin declares an agent tool and
the runtime URL it runs on.

### 6.5 Plugins: `io.github.folken2/plugins`

There is no official plugins extension, so this one is ours. The prefix is the reversed
domain of `folken2.github.io`, which we control. It changes if nuvel gets its own domain.

- **A plugin is a directory** with the same `plugin.json` our plugin repositories
  already ship, plus optional `skills/`, `apps/` (MCP App bundles), `tools.json`
  (tools that map to an executor) and `agents.json` (remote executors). One nuvel-mcp
  mounts many, from a `--plugins-dir` or a list in a config file.
- **Namespacing.** A plugin's tools are listed as `<plugin>__<tool>` and its resources
  under `skill://<plugin>/...` and `ui://<plugin>/...`. Names are unique per server, and
  a clash refuses to start rather than shadowing a tool.
- **Extension methods.** `plugins/list` returns each mounted plugin's name, version and
  what it contributes. A client without the extension still gets everything, because
  the contributions are ordinary tools, resources and skills. The extension only adds
  grouping.
- **Isolation.** A plugin can't run code inside the server process. Its tools are
  declarations bound to an executor, never Python imported from the plugin. That keeps
  "standard library only" and our security rules true no matter what a plugin
  contains.
- **Access.** Scopes per plugin (`plugin:<name>`) decide which mounted plugins a token
  sees. That is part of the private cache scope in 5.3.

## 7. Auth

The server remains both resource server and authorization server. For 2026-07-28:

- Return `iss` in authorization responses (RFC 9207, SEP-2468).
- Accept Client ID Metadata Documents (fetch the client's metadata from its `client_id`
  URL, with the same fetch limits as the rest of the server), and keep dynamic
  registration for older clients, since it is deprecated, not removed.
- Honour `application_type` at registration (SEP-837), so native clients' loopback
  redirects are accepted.
- Declare `io.modelcontextprotocol/oauth-client-credentials` in discover. That grant
  already exists.
- On the modern path, auth is per request. The principal comes from the token on each
  POST, as it does today after `initialize`.

**verify**: the 2026-07-28 authorization pages, which this design was written without.

## 8. Telemetry and usage

- The usage log reads `clientInfo` from each request's `_meta` on the modern path,
  sanitized as today. It still stores no IP address and no arguments.
- `traceparent`, `tracestate` and `baggage` in `_meta` (SEP-414) are read and passed to
  the executors, so a task's agent run joins the caller's trace. We store only the trace
  id, never the baggage.
- Run traces, UI events and recipes don't change. The extensions report into them.

## 9. Order of work

Each phase is a release and keeps every earlier client working.

| Phase | Builds | Done when |
|---|---|---|
| **0. Kernel** | `protocol/`, the modern HTTP and stdio paths, the legacy adapter, `server/discover`, cache fields, header checks, sealed `requestState`, the `conformance` test extra. Correct legacy version negotiation. | Every existing tool works on 2025-11-25 and 2026-07-28 over stdio and HTTP. The SDK `Client` passes in both modes. The existing suite passes unchanged. |
| **1. Skills** | `extensions/skills.py`, new URIs and alias, manifests with digests, directory reads, `resources/updated`. | A client that declares the extension can list, get and read every hub skill and verify each digest. The hub tools behave as before. |
| **2. Apps** | `extensions/apps.py`, the skill browser view, app-only tools, `ui_event` surface. | The view renders in an MCP Apps host and degrades to text in one without. Then remove the 2024-11-05 HTTP+SSE transport. |
| **3. Tasks** | `extensions/tasks.py`, the task store and migration, the `inline` executor, `subscriptions/listen` task notifications. | A long tool returns a task, survives a server restart when on Postgres, and is cancellable. A client without the extension still gets a result. |
| **4. Agents and plugins** | The `remote` executor and `/tasks/<id>/events`, `--plugins-dir`, `extensions/plugins.py`, plugin scopes. Server Card when it leaves beta. | A plugin from a plugin repository mounts and serves its skills and a view, and an agent tool runs on a generated agent runtime as a task. |

Moves of existing code into `transports/` and `extensions/` happen in their own PRs,
apart from behaviour changes, as for the agent packages.

## 10. Open questions

1. The task store holds user content (6.3). Is a 24-hour, owner-only, never-relayed
   store acceptable, or should results stay in the agent runtime with only a status
   here?
2. The plugin manifest: reuse the plugin repositories' `plugin.json` exactly, or define
   a nuvel-mcp manifest that points at it?
3. Remove the 2024-11-05 transport after phase 2, as proposed, or keep it until a major
   version?
4. A domain for our extension prefix, if `io.github.folken2` should not be permanent.
5. The item still marked **verify**: the authorization pages. (The discover schema was
   checked in phase 0; see 5.2.)

## Sources

- 2026-07-28 announcement: https://blog.modelcontextprotocol.io/posts/2026-07-28/
- Changelog: https://modelcontextprotocol.io/specification/2026-07-28/changelog
- Streamable HTTP: https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http
- Multi round-trip requests: https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr
- Subscriptions: https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/subscriptions
- `server/discover`: https://modelcontextprotocol.io/specification/2026-07-28/server/discover
- Versioning: https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning
- Extensions: https://modelcontextprotocol.io/extensions
- MCP Apps: https://github.com/modelcontextprotocol/ext-apps
- Skills: https://github.com/modelcontextprotocol/ext-skills
- Tasks: https://github.com/modelcontextprotocol/ext-tasks
- Agents Working Group: https://github.com/modelcontextprotocol/agents-ig
- Python SDK v2: https://py.sdk.modelcontextprotocol.io/whats-new/

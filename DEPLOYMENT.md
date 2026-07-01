# Deployment

How to run blue-prism-mcp against a Blue Prism v7 Enterprise estate: the
service account it needs, the environment contract, wiring it into an MCP
client, and the day-one verification checklist for the action surface.

## Prerequisites

- **Blue Prism 7.2+** with the v7 REST API and the Blue Prism Authentication
  Server deployed. The API surface is stable from 7.2 through 7.5.1; the
  visibility tools degrade gracefully to 7.1 (with `list_queue_configurations`
  needing 7.4 and degrading to an "unavailable" note below it), but the control
  tools need 7.2.
- **Python 3.11+** on the machine that runs the MCP client (the server speaks
  stdio, so it runs wherever the client runs).
- A **dedicated service account** registered with the Authentication Server
  for OAuth2 client-credentials (the only scheme the v7 API documents). Do not
  reuse an interactive user's credentials: the audit story depends on the MCP
  server having its own identity.

## Install

```bash
pip install blue-prism-mcp            # light base: MCP runtime + HTTP client
pip install "blue-prism-mcp[pii]"     # + Presidio NER scrubbing (optional)
python -m spacy download en_core_web_sm   # only for [pii]
```

## Service-account permissions

The server inherits exactly the service account's Blue Prism permissions —
there is no permission model of its own to misconfigure. Two rules follow:

- **Visibility/Insight tools** need the corresponding read permissions
  (queues, sessions, resources, schedules, processes, dashboards). A standard
  read-only operations role covers them. The v0.5.0 context/topology reads add
  a few more read clauses: `list_queue_configurations` needs Read (or Full)
  Access to Queue Management; `list_resource_pools` a View Resource clause;
  `list_environment_variables` a View Environment Variables clause (Business
  Objects or Processes); `list_process_groups` a process-view clause (e.g. View
  Process Definition). A read denied for lack of a clause surfaces as an error
  on that one tool, not a startup failure — and `list_queue_configurations`
  additionally needs **Blue Prism 7.4+**, degrading to an "unavailable" note on
  older estates rather than failing. The v0.7.0 deeper reads add no new clause
  in practice: `get_session_log`'s filters use the same permissions as the base
  read, and `list_schedules`' last-run fold-in reads the schedule logs (a
  Schedule-view clause — e.g. View Schedule — or System - Scheduler); if that
  read is denied the listing still stands and sets `meta.last_run_unavailable`
  rather than failing.
- **Control tools are capability-gated at startup.** When actions are enabled,
  the server reads `GET /user/permissions` and registers only the action
  tools the account can actually execute — a tool the account cannot run does
  not exist as far as the model is concerned. The documented requirements:

  | Action tool | Required permissions |
  |-------------|----------------------|
  | `retry_queue_item` / `defer_queue_item` | Full Access to Queue Management |
  | `start_process` | one of Create Process / Edit Process / Execute Process, **and** Control Resource |
  | `stop_session` | same as `start_process` (one process permission **and** Control Resource) |
  | `set_schedule_enabled` (retire) | Edit Schedule **and** Retire Schedule |
  | `set_schedule_enabled` (unretire) | the retire pair **and** Create Schedule (enforced at call time, so retire-only accounts keep the tool) |
  | `trigger_schedule` | Edit Schedule |
  | `stop_schedule` | Edit Schedule (same as `trigger_schedule`) |

  Grant the *least* of these that covers the actions you want exposed; the
  startup audit line records which tools registered and which were withheld,
  with the permission clauses they lack.

## Configuration

The environment is the deployment contract — see [.env.example](.env.example)
for the full annotated template. The short version:

| Variable | Purpose |
|----------|---------|
| `BP_API_BASE_URL` | v7 API base, e.g. `https://<server>/api/v7` — **required (live)** |
| `BP_AUTH_URL` | Authentication Server base — **required (live)** |
| `BP_CLIENT_ID` / `BP_CLIENT_SECRET` | service-account credentials — **required (live)** |
| `BP_DATA_SOURCE` | `live` (default) / `mock` — lean in-memory fixtures / `demo` — a larger populated estate; both run the full surface with no estate needed |
| `BP_API_VERIFY_SSL` | TLS verification (default `true`) |
| `BP_PII_BACKEND` | `null` (default) / `regex` / `presidio` — fails loud at startup if the backend can't load |
| `BP_PII_CUSTOM_PATTERNS` | JSON array of `{"name", "pattern"}` domain identifiers |
| `BP_ENABLE_ACTIONS` | register the Tier 3 control tools (default `false`) |
| `BP_AUDIT_LOG_PATH` | JSON-lines audit file — **required when actions are enabled** |

Startup is fail-loud by design: missing connection settings (all named in one
error), an unknown data source or PII backend, a missing or unwritable audit
path, and a failed permissions call each refuse to start rather than serve a
degraded surface.

## Wiring into an MCP client

The console entrypoint speaks the stdio transport; stdout carries JSON-RPC
only, all logging goes to stderr. For Claude Desktop
(`claude_desktop_config.json`) or any client with the same shape:

```json
{
  "mcpServers": {
    "blue-prism": {
      "command": "blue-prism-mcp",
      "env": {
        "BP_API_BASE_URL": "https://bp-api.example.com/api/v7",
        "BP_AUTH_URL": "https://bp-auth.example.com",
        "BP_CLIENT_ID": "svc-mcp",
        "BP_CLIENT_SECRET": "…",
        "BP_PII_BACKEND": "regex"
      }
    }
  }
}
```

To evaluate without an estate, the whole `env` block can be just
`{"BP_DATA_SOURCE": "mock"}` — or `"demo"` for a larger, more realistic estate
(varied queue health, in-flight/stale sessions, a failed schedule, months of
session history) that's a better fit for a live walkthrough.

## Enabling the action surface

Three layers sit between the model and a write; none can be silently relaxed:

1. **Capability gating** — only the tools the service account's permissions
   satisfy are registered (table above).
2. **Audit** — `BP_AUDIT_LOG_PATH` is required; every invocation appends a
   JSON line (UTC timestamp, tool, args, status), and the *attempt* line is
   written before the write is issued, so no estate mutation can outrun its
   audit record. Audit args carry ids, names, and dates — never payloads or
   exception text. Rotate/retain the file per your audit policy; the server
   only appends.
3. **Dry-run by default** — every action tool takes `dry_run` defaulting to
   `true`: the default call resolves names, validates inputs, and returns the
   exact write it *would* issue without sending anything. A mutation requires
   an explicit `dry_run=false` from the model.

Recommended rollout: run read-only first; then enable actions with a
retire-only/queue-only account; widen permissions as trust is established.

## Day-one verification checklist

The OpenAPI specs underdocument three things the control tools depend on.
Verify them against *your* estate (7.2+) before allowing `dry_run=false`:

1. **Attempt PATCH paths** — `defer_queue_item` sends an RFC 6902 JSON Patch
   to `PATCH /workqueues/{id}/items/{itemId}/attempts/{attemptNumber}` with
   path `/deferredDate` (inferred from the item schema; the spec does not
   list the accepted paths). Run a dry-run, inspect the returned patch, then
   issue it against a scratch queue item and confirm the deferral lands.
2. **Schedule retire/unretire body** — `set_schedule_enabled` issues
   `PUT /schedules/{id}` with the `isRetired` flag; the published request
   schema omits the flag even though the endpoint documents retire
   permissions. Confirm on a disposable schedule that retire and unretire
   both take effect.
3. **Live permission strings** — `GET /user/permissions` returns permission
   *names*, but the spec never shows a real response. The capability resolver
   matches the documented display names case-insensitively; check the startup
   audit line's registered/withheld split against what the account can
   actually do in Interact/Hub, and report mismatches.

A fourth, lower-stakes check: `start_process` uses the create-then-run flow
(`POST /sessions`, then `PATCH /sessions/{id}` to `Running`) — confirm it
end-to-end with a harmless utility process. When called with `parameters`, a
`PUT /sessions/{id}/parameters` is issued between the two (while the session is
Pending); verify on a process with declared inputs that the values land before
it runs. Note a session can be created and started against a logged-out worker —
it is the *process* that fails at run time if it depends on an interactive
desktop/login session on the resource, not the start call itself. `stop_session`
drives the same endpoint with `{status: Stopped}`; confirm against a running
session that the stop request is accepted and the run winds down. `stop_schedule`
issues `DELETE /schedules/{id}/runs/active` — confirm against a schedule with an
active run that the run is cancelled.

The v0.3.0 diagnostic reads add two non-control checks (reads, so no
`dry_run` gate, but worth confirming): `get_queue_item` returns item `data` only
for queues that are unencrypted or use a database encryption key — on an
application-server-encrypted queue the call returns a 4xx, so confirm the
behaviour on the queue types your estate runs; and that `get_queue_item`'s
type-aware scrub leaves no personal data in the payload your processes carry.

The v0.5.0 context reads add one more (again a read, no `dry_run`):
`list_environment_variables` returns each variable's `value`, typed `object` in
the spec with no inner shape. The tool scrubs it type-aware on the variable's
`dataType` (Password redacted, free text scrubbed, binary/image dropped, scalars
kept) — confirm against your estate that the value arrives in the shape that
policy expects and that no secret or personal data reaches the model.

## Operational notes

- **stdout is sacred.** The stdio transport speaks JSON-RPC over stdout;
  anything else that writes there corrupts the session. The server pins all
  logging to stderr — keep it that way in any wrapper scripts.
- **Caching** — reads are cached per server instance for `BP_API_CACHE_TTL`
  seconds (default 30); two server instances never share state, so one client
  can safely point at two estates via two server entries. The default cache is
  thread-safe; a host that embeds the engine in-process and shares one client
  across worker threads can inject its own store (e.g. a shared/Redis-backed
  cache) behind the `Cache` protocol via `BPClient(config, cache=...)`.
- **PII** — scrubbing applies at the exception-message and session-log
  boundaries before text leaves the process. `null` is an explicit choice,
  not a fallback; if you configured `regex` or `presidio` and the backend
  cannot load, the server refuses to start rather than run unredacted.

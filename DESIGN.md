# blue-prism-mcp — design & build plan

The architecture, decisions, and phased plan for this project.

## What this is

A **standalone, distributable Model Context Protocol (MCP) server for Blue Prism
v7 Enterprise**. It gives an LLM agent governed access to a Blue Prism estate
over the supported v7 REST API — no direct database reads.

### Why it exists
- No public/community Blue Prism MCP server exists today — green field.
- SS&C is building MCP natively into Blue Prism Next Generation (AI Gateway,
  2026 roadmap). Building a Next Gen MCP means racing the platform vendor.
- **v7 / Enterprise is the defensible gap**: a large installed base with a
  documented REST API, but no native agentic path because SS&C's agentic
  investment is all on Next Gen. This server fills that gap over the supported
  API surface — low support liability, no database coupling.

### Provenance
The Blue Prism v7 REST client at the core of this server was first proven inside
my Blue Prism dashboard, which sources all of its data from the v7 REST API (no
DB reads). The tool shapes (the relevance-sorted envelope, ISO validation,
boundary PII scrubbing) were validated there before being extracted here as a
standalone library. The dashboard is the application; this is the reusable
library it was always implicitly built on.

---

## Design decisions

- **Standalone artifact.** Independent repo, independently versioned and
  installable.
- **Auth is OAuth2 client-credentials** against the Blue Prism Authentication
  Server (form-encoded `POST <auth-server>/connect/token`, scope
  `bp-api bpserver`, JWT bearer on every request). This is the only scheme the
  API documents, and it is
  identical across 7.0–7.5, so one implementation covers every supported
  version. Config carries the auth-server URL and a client id/secret alongside
  the API base URL; there is no username/password flow.
- **Version floor: v7.2.** The surface is verified against the official OpenAPI
  specs (7.0.1 through 7.5.1 — see *v7 API ground truth* below): Tier 1+2 need
  only 7.1 (7.0 lacks just `/processes`), the Tier 3 writes need 7.2, and the
  API is endpoint-stable from 7.2 through 7.5.1. Build against 7.5.1, declare
  support for 7.2+.
- **Names in, UUIDs underneath.** Every v7 entity ID is a UUID; agents speak in
  names ("the Invoices queue"). Tools accept names and resolve them to IDs via
  the list endpoints (Phase 4).
- **Tool surface designed as estate primitives**, not a copy of the dashboard's
  MI-oriented tools. Read-only management information is what a dashboard does;
  the differentiated value of an MCP server is a **governed action surface** on
  v7 Enterprise.
- **Pluggable PII scrubbing.** A `Scrubber` protocol returning a `ScrubResult`
  (scrubbed text + entity types found — the audit trail records types, never
  content), with three shipped tiers: a no-op `NullScrubber`; a zero-dependency
  `RegexScrubber` covering the pattern-shaped entities that dominate UK FS
  estates (NI numbers, sort codes, account numbers, PANs Luhn-checked, emails,
  phones), so the *base* install has a credible redaction story; and a
  Presidio-backed NER tier behind the optional `[pii]` extra, which reuses the
  same patterns as score-1.0 recognizers so exact domain matches beat NER
  guesses. Backend selection (`pii_backend = null | regex | presidio`) is
  explicit config and **fails loud at startup** when the requested backend
  cannot load — redaction must never silently degrade. Replacement is a
  pluggable operator: typed tokens (`[UK_NI_NUMBER]`) by default, with a seam
  for correlation-preserving numbered pseudonyms (`[PERSON_1]`) later.
- **Insight views are separate tools**, not parameters on the primitives — tight,
  single-purpose tool descriptions drive better model tool-selection.
- **Mock mode is a first-class run mode** (`BP_DATA_SOURCE=mock`): the server
  runs the full tool surface over `MockBPClient`'s in-memory fixtures with no
  estate and no credentials. It exists so the artifact can be evaluated
  end-to-end in any MCP client risk-free (and so Phase 7's validation needs no
  live estate). Live mode validates connection settings at startup — the
  client is lazy, so without that check a misconfigured server would register
  a tool surface where every call fails. Governance does not relax in mock
  mode: enabling actions still requires the audit path.
- **Session-log read is in v1.** `get_session_log` is the highest-value agentic
  read ("why did this run fail?"). It is a second PII vector beyond exception
  messages (stage data can carry item payloads) and therefore routes through the
  scrubber. Queue-item *lists* are not a payload vector — the v7 list endpoint
  returns items without their `data` field by design — but `exceptionReason` in
  item lists is scrubbed, and any future single-item read (the one shape that
  does carry `data`) must strip or scrub the payload.
- **ROI and AI-triage tools are out of core** — they are dashboard-specific and,
  for ROI, depend on commercial configuration no other deployment has.
- Naming: `list_*` for collections, `get_*` for a single entity.

## Tool surface — three tiers

### Tier 1 — Visibility (read, maps to v7 entities, envelope-shaped)
- `list_queues` / `get_queue` — work-queue health
- `list_queue_items` — **requires a queue + status + date window** (queues run to
  millions of items; no estate-wide item listing). Envelope-capped.
- `get_queue_item` — one item in full **including its payload `data`** (the only
  read that returns it). The `data` DataCollection is scrubbed type-aware: free
  text through the scrubber, passwords redacted, binary/image dropped, scalars
  kept, nested collections recursed. (Blue Prism cannot return data for
  application-server-encrypted queues — that call fails; use the no-data tools.)
- `list_item_attempts` — an item's attempt history (no payload data; the
  exception reason at each attempt is scrubbed)
- `list_sessions` — run history; filter by process/resource/status/date
- `get_session` — one session's detail by id (no date window), PII-scrubbed
- `get_session_log` — stage-level log for one session (PII-scrubbed, size-capped)
- `list_resources` — digital workers + status
- `list_schedules` — the schedule catalogue + retirement state. (The API holds
  no next-run field anywhere, and last-outcome lives in the schedule run logs —
  see the ground truth below; run-history enrichment is deferred.)
- `list_processes` — published process catalogue

### Tier 2 — Insight (separate derived tools)
- `exception_summary` — exceptioned items for one queue + window, grouped by
  *scrubbed* exception reason (grouping after scrubbing folds messages that
  differ only in personal data into one bucket)
- `throughput_summary` — per-process session outcomes over a window: status
  counts, completion rate, and the terminationReason breakdown
- `estate_health` — resource status rollup + the licence limits-vs-usage block
  from `GET /dashboards/currentLimitsAndUsage`

No v7 endpoint aggregates exceptions or throughput (see the `/dashboards/*`
verdict below), so the first two are client-side aggregations over the Tier 1
reads, scoped by the same required-window rules.

### Tier 3 — Control (designed in, shipped disabled)
Gated behind `enable_actions=False`. Governance, capability-gating, audit, and
dry-run scaffolding is present; the action tools are registered only when
actions are enabled.
- `retry_queue_item` / `defer_queue_item`
- `start_process` / `stop_session`
- `set_schedule_enabled` / `trigger_schedule` / `stop_schedule`

**The governance contract (Phase 5).** Three layers sit between the model and
a write, and every one fails loud rather than degrading:
- **Capability gating.** At registration the resolver reads
  `GET /user/permissions` and registers only the action tools the service
  account's permissions satisfy (the per-endpoint requirements are in the
  ground truth below) — a tool the account cannot execute does not exist as
  far as the model is concerned. A failed permissions call refuses to start.
  Unretiring a schedule needs `Create Schedule` on top of the retire pair;
  that extra is enforced at call time so accounts that can only retire still
  get the tool.
- **Audit.** Enabling actions requires `audit_log_path` (`BP_AUDIT_LOG_PATH`)
  — there is no default and no opt-out. Every invocation appends a JSON line
  (UTC timestamp, tool, args, status: startup/dry_run/attempt/success/error),
  and the *attempt* line is written before the write is issued, so no estate
  mutation can outrun its audit record. Audit args carry ids, names, and
  dates only — never payloads or exception text (error lines record the
  exception class and HTTP status, not the message) — and the file is touched
  at startup so an unwritable path fails before the first action. Never
  stdout. Post-write audit failures are flagged and logged to stderr rather
  than raised: once the write is issued an audit failure can only misreport
  the estate, and a completed action must never look failed.
- **Dry-run by default.** Every action tool takes `dry_run: bool = True`: the
  default call resolves names, validates inputs, and returns the exact write
  it *would* issue without sending anything. Mutating the estate requires an
  explicit `dry_run=False`.

The originally planned `mark_exception_resolved` is **dropped**: the v7 item
lifecycle is attempt-based (retry creates a new attempt; PATCH/DELETE modify
one) and the API has no "resolve" semantic to map it to.

**Shared contract.** List tools return an envelope —
`{"items": [...], "meta": {total, returned, truncated, sorted_by}}` — with
server-side relevance sort and a `limit`, so a large estate can never blow the
client's context and the model always knows how much it did not see. Dated
parameters are ISO-validated and fail loudly. Per-message PII scrubbing is
cached.

---

## v7 API ground truth (verified)

The endpoint surface was verified against the official OpenAPI 3.0.3 specs for
7.0.1, 7.1.2, 7.2.0, 7.3.0, 7.4.0, and 7.5.1 (the latest; 7.5 shipped December
2025). The docs pages embed the full spec JSON — e.g.
`https://documentation.blueprism.com/bp-7-5/en-us/bp-api/bpe-7-5-1-api-spec.html`
— so the mapping below is spec-confirmed, not assumed. The API grew at 7.1 and
7.2 and has been endpoint-stable since: 7.3 added nothing, 7.4 added only
lightweight reads (`/sessions/{id}/logslight`, `/workqueues/light`), 7.5 only
webhook plumbing.

| Tool | Endpoint | Since |
|------|----------|-------|
| `list_queues` / `get_queue` | `GET /workqueues`, `GET /workqueues/{id}` | 7.0 |
| `list_queue_items` | `GET /workqueues/{id}/items` | 7.0 |
| `get_queue_item` | `GET /workqueues/items/{id}` (→ `WorkQueueItem`, the only read with `data`) | 7.0 |
| `list_item_attempts` | `GET /workqueues/{id}/items/{itemId}/attempts` (→ `WorkQueueItemNoData[]`) | 7.2 |
| `list_sessions` | `GET /sessions` | 7.0 |
| `get_session` | `GET /sessions/{id}` (→ `SessionSummary`) | 7.0 |
| `get_session_log` | `GET /sessions/{id}/logs` (`logslight` on 7.4+) | 7.0 |
| `list_resources` | `GET /resources` | 7.0 |
| `list_schedules` | `GET /schedules` | 7.0 |
| `list_processes` | `GET /processes` | 7.1 |
| `retry_queue_item` | `POST .../items/{id}/attempts` (201 → `{attemptId}`) | 7.2 |
| `defer_queue_item` | `PATCH .../attempts/{attemptId}` (RFC 6902 JSON Patch, sent as `application/json-patch+json`) | 7.2 |
| `start_process` | `POST /sessions` → UUID, optional `PUT /sessions/{id}/parameters`, then `PATCH /sessions/{id}` `{status: Running}` | 7.1 |
| `stop_session` | `PATCH /sessions/{id}` `{status: Stopped}` | 7.1 |
| `set_schedule_enabled` | `PUT /schedules/{id}` (retire/unretire) | 7.0 |
| `trigger_schedule` | `POST /schedules/{id}/runs` (optional `startTime`) | 7.2 |
| `stop_schedule` | `DELETE /schedules/{id}/runs/active` (202, no body) | 7.2 |

Wire-level contract (identical across versions):
- **Paging is token-based only**: `itemsPerPage` + `pagingToken` request params;
  responses are `{"items": [...], "pagingToken": "..."}`. No offset paging
  exists in v7 (the client keeps offset support only as a config escape hatch
  for gateways).
- **Filters are deepObject-encoded**: `startTime[gte]=<ISO>`,
  `lastUpdated[lte]=<ISO>`, `status[eq]=<text>`. Queue items distinguish
  `state` (the lifecycle enum: Pending/Locked/Deferred/Completed/Exceptioned)
  from `status` (free user-supplied text) — the tools' "status" concept is the
  API's `state`.
- **IDs are UUIDs**; writes return `204`/empty bodies or bare values
  (`POST /sessions` returns a bare UUID string), so the client must not assume
  JSON object responses.
- **The items list returns `WorkQueueItemNoData`** — the API itself excludes
  item payload `data` from list responses; only the single-item GET carries it.
  `exceptionReason` *is* present in lists and remains a scrub target.
- **`SessionSummary` carries `exceptionMessage`** (plus `exceptionType` and a
  `terminationReason` enum: None/ProcessError/InternalError), so session
  *lists* are an exception-message scrub boundary exactly like item lists'
  `exceptionReason`. Its `status` enum is
  Pending/Running/Terminated/Stopped/Completed/Stopping/Warning.
- **`/sessions/{id}/logslight` (7.4+) is shape-identical to `/logs`** — same
  parameters, same `SessionLogsPage`/`SessionLogSummary` response (verified
  field-by-field on 7.5.1); the "light" is a cheaper server-side query, not a
  reduced payload. The client therefore probes `logslight` first and falls
  back to `/logs`; it only *pins* `/logs` for the life of the instance when
  the fallback succeeds, because a bare 404 may equally mean the session id
  itself is unknown and must not demote a 7.4+ estate.
- **`/dashboards/*` (all five present at the 7.2 floor; verified on 7.2.0 and
  7.5.1):** `currentLimitsAndUsage` (licence limits vs current usage —
  concurrent sessions, runtime resources, published processes) backs
  `estate_health`'s licence block and is the only dashboards endpoint this
  server consumes. `workQueueCompositions` is redundant here —
  `GET /workqueues` already returns the same per-state counts on
  `WorkQueueSummary` (pending/completed/locked/exceptioned/total plus
  `averageWorkTime`). The two resource-utilization endpoints are dashboard MI
  (and the API's only page-*number*-paged reads); utilisation stays out of the
  reusable surface. Nothing under `/dashboards` aggregates exceptions or
  throughput.
- **`ScheduleSummary` is the schedule definition only** (interval fields,
  `isRetired`; its `id` is an *integer*, unlike every other entity's UUID).
  No next-run field exists anywhere in the API; per-run history (a status
  enum, start/end, duration) lives in `GET /schedules/logs`. v1
  `list_schedules` returns definitions; run-log enrichment is deferred.
- **`GET /user/permissions` (7.1+) answers a flat JSON array of
  permission-name strings** — no envelope, no paging (verified on 7.5.1; the
  spec's example shows placeholder names). The Phase 5 capability resolver
  queries it at startup and registers only the action tools the account can
  actually execute.
- **Per-write permission requirements** (from the endpoint descriptions —
  the spec defines no permissions enum, so these display names are prose):

  | Action | Documented requirement |
  |--------|------------------------|
  | `retry_queue_item` / `defer_queue_item` | `Full Access to Queue Management` |
  | `start_process` (session create + control) | one of `Create Process` \| `Edit Process` \| `Execute Process`, **and** `Control Resource` |
  | `stop_session` (`PATCH /sessions/{id}` → `Stopped`) | same as `start_process` (one process permission **and** `Control Resource`) |
  | `set_schedule_enabled` — retire | `Edit Schedule` **and** `Retire Schedule` |
  | `set_schedule_enabled` — unretire | retire pair **and** `Create Schedule` |
  | `trigger_schedule` / `stop_schedule` | `Edit Schedule` (both run-control verbs document the same single permission) |

  Deployment docs (Phase 6) must cover service-account permission mapping.

**Needs day-one verification against a live estate** (the spec underdocuments
all three): the JSON Patch paths accepted by the attempt PATCH
(`/deferredDate` is inferred from the item schema); the retire/unretire body
for the schedule PUT (`ScheduleSummary` carries `isRetired` and the PUT
documents retire permissions, but the published request schema omits the
flag); and the exact permission strings `GET /user/permissions` returns —
the capability resolver matches the documented display names
case-insensitively, but the spec never shows a real response.

---

## Architecture (layers, bottom-up)
1. **`BPClient`** (`client.py`) — the v7 REST client as a stateful object: the
   OAuth2 token (with its expiry), a per-instance TTL cache, and the injected
   config all live on the instance, so two estates / two server instances never
   collide.
2. **Config** (`config.py`) — a per-deployment object built from the environment
   (credentials, base URL, pagination, SSL verification, feature flags including
   `enable_actions`).
3. **PII** (`pii.py`) — the `Scrubber` protocol (`scrub(text) -> ScrubResult`),
   the null/regex/Presidio tiers, and the fail-loud `build_scrubber` factory.
   Applied at the exception-message and session-log boundaries.
4. **Tools** (`tools/`) — the three tiers over `BPClient`, carrying the envelope
   contract.
5. **Governance** (`governance.py`) — capability gating, audit, and dry-run;
   gates Tier 3.
6. **Server** (`server.py`) — the FastMCP stdio server and console entrypoint.

### The client decoupling
The client began as a module of functions bound to a UI framework's caching and
session state. As a standalone library it becomes a class: framework caching is
replaced with an instance-level TTL cache, session-bound auth state becomes
instance state, and module-level configuration globals become an injected config
object.

### The surface is extended, not just lifted
The dashboard only ever needed resources, queues, schedules, and sessions. The
reusable surface additionally requires queue-**item** listing, the process
catalogue, the session stage-log, and (for Tier 3) the write endpoints.

---

## Phased build plan
- **Phase 0 — Scaffold** (done): packaging, CI, licence, layer skeleton.
- **Phase 1 — Decouple the client** into `BPClient`; replace framework caching
  and session state; provide a mock client and port its tests. *The core risk.*
- **Phase 2 — Extend the client**: queue-item listing, process catalogue,
  session stage-log; the Tier 3 write endpoints; align the whole surface (auth,
  paging, filter encoding, write paths) with the verified API ground truth
  above.
- **Phase 3 — Pluggable PII**: the `Scrubber` protocol, the null/regex/Presidio
  tiers, and the fail-loud factory; wiring at the exception-message and
  session-log boundaries (and the cached scrub) lands with the tools in Phase 4.
- **Phase 4 — Tier 1 + 2 tools**: visibility primitives and the three insight
  tools, with the envelope, ISO validation, and cached scrub; tool tests.
- **Phase 5 — Governance scaffold + Tier 3 (disabled)**: the `enable_actions`
  flag, capability resolver (backed by `GET /user/permissions`), audit log, and
  dry-run; action tools registered only when enabled.
- **Phase 6 — Server + packaging**: the FastMCP server, console entrypoint, and
  deployment/day-one verification documentation.
- **Phase 7 — Validate**: a byte-clean stdio handshake, end-to-end validation in
  an MCP client, and the coverage gate.

Phases 0–7 are **v0.1.0** — the shipped surface described above. What follows is
the next cycle: it does not change v0.1.0's behaviour, it deepens the v7 coverage
and opens the core to hosts that embed it beyond the single stdio process.

### Post-v0.1.0 releases
A full audit of the client against the 7.5.1 OpenAPI spec confirmed the
18-endpoint surface and surfaced an auth bug plus a set of useful unused endpoints.
Sequenced as:
- **v0.1.1 (patch) — auth correctness.** The token request must carry the
  `bp-api bpserver` scope pair the spec requires on every endpoint; v0.1.0 requested
  only `bp-api`, so as released it could not reach a live estate. Bugfix only.
- **v0.2.0 (minor) — process-control completion.** `start_process` gains optional
  typed start-up `parameters` (POST /sessions → PUT .../parameters → PATCH Running;
  the audit records parameter names and types, never values), and **`stop_session`
  is pulled forward from Phase 10** as `start_process`'s control sibling
  (`PATCH /sessions/{id}` `{status: Stopped}`, the same permissions as
  `start_process`).
- **v0.3.0 (minor) — incident response & diagnostics.** One control verb and three
  drill-down reads that turn the list surface into a diagnostic one:
  - `stop_schedule` (`DELETE /schedules/{id}/runs/active`) — incident sibling of
    `trigger_schedule`, sharing its single `Edit Schedule` permission.
  - `get_queue_item` (`GET /workqueues/items/{id}`) — one item in full **including
    the `data` payload**, the only read that returns it. The DataCollection is
    scrubbed type-aware: free text through the scrubber, passwords redacted,
    binary/image dropped, scalars kept, nested collections recursed.
  - `list_item_attempts` (`GET /workqueues/{id}/items/{itemId}/attempts`) — an
    item's attempt history (no payload data; scrubbed exception reasons).
  - `get_session` (`GET /sessions/{id}`) — one run's detail by id, no date window.
- **Endpoint-gap backlog (post-0.3.0 — each a real value-add the audit identified;
  most map onto Phases 9–10 / console E7–E9):**
  - *Insight:* dashboard aggregates (`workQueueCompositions`, `resourceUtilization`,
    `licensesEntitlement`) for cheaper Tier-2 metrics and the console baseline feed;
    schedule run-history is already Phase 9.
  - *Context/console:* `workqueues/configurations` (the process→queue map console
    severity needs — L2), `resources/pools`, environment-variable reads, process groups.
- **North star (not a near-term gap):** 7.5's `subscriptions` PATCH and work-queue
  item `callbacks`/webhooks are the event-driven orchestration plumbing for the
  watch-and-react capability.

### Beyond v1 (phases 8+)
- **Phase 8 — Embeddable core.** A tool's logic and its presentation are one body
  today: each closure resolves names, scrubs, sorts, and wraps the result in the
  LLM-shaped top-N envelope in a single pass. Split them — a pure domain function
  returning the full ranked records, with the envelope as one adapter over it — so
  a host embedding the engine in-process can consume the records and apply its own
  representation instead of re-deriving the logic. The cache gets the same
  treatment: the per-instance dict suits one stdio process, but a long-lived,
  multi-threaded host sharing a client across workers needs a **thread-safe,
  injectable cache** behind a small protocol, with the in-process implementation
  kept as the default. No tool gains or loses behaviour; the v0.1 surface is
  unchanged.
- **Phase 9 — Fuller v7 read coverage.** Push the filtering the v7 API already
  supports down into the reads the envelope currently caps client-side.
  `get_session_log` gains an errors-only filter and a time window, and exposes the
  API's token paging rather than top-N only (the client already probes `logslight`
  on 7.4+). `list_schedules` gains last-outcome enrichment from
  `GET /schedules/logs` — the run-history read v1 deferred — so a schedule carries
  its last result, not just its definition. `exception_summary` gains an
  estate-wide variant grouped across queues, so the dominant failure mode is one
  call rather than a loop over every queue.
- **Phase 10 — `stop_session` (Tier 3).** *Pulled forward into v0.2.0 — see
  Post-v0.1.0 releases above.* The missing control sibling of
  `start_process`: `PATCH /sessions/{id}` with `{status: Stopped}`, the same
  endpoint `start_process` already drives for `{status: Running}`. Capability-
  gated, audited, and dry-run by default like every Tier 3 tool. It carries
  `start_process`'s permission clause (a process permission **and** `Control
  Resource`); the Stopped transition is a day-one live-verification item — the
  same posture as the v1 writes the spec underdocuments.

## Conventions
- The stdio transport speaks JSON-RPC over stdout; nothing else may write there.
  Silence noisy library loggers before and after importing heavy dependencies.
- PII audit logging records entity *types* only, never raw content, and writes
  to files, never stdout.
- Lint clean, tests passing, and the coverage gate met before each phase lands.

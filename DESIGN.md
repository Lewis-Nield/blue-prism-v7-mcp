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
  Server (form-encoded `POST <auth-server>/connect/token`, scope `bp-api`, JWT
  bearer on every request). This is the only scheme the API documents, and it is
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
- `list_sessions` — run history; filter by process/resource/status/date
- `get_session_log` — stage-level log for one session (PII-scrubbed, size-capped)
- `list_resources` — digital workers + status
- `list_schedules` — next runs, last outcome
- `list_processes` — published process catalogue

### Tier 2 — Insight (separate derived tools)
- `exception_summary` — grouped exception counts
- `throughput_summary` — straight-through-processing rate / items processed
- `estate_health` — resource status rollup

### Tier 3 — Control (designed in, shipped disabled)
Gated behind `enable_actions=False`. Governance, capability-gating, audit, and
dry-run scaffolding is present; the action tools are registered only when
actions are enabled.
- `retry_queue_item` / `defer_queue_item`
- `start_process`
- `set_schedule_enabled` / `trigger_schedule`

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
| `list_sessions` | `GET /sessions` | 7.0 |
| `get_session_log` | `GET /sessions/{id}/logs` (`logslight` on 7.4+) | 7.0 |
| `list_resources` | `GET /resources` | 7.0 |
| `list_schedules` | `GET /schedules` | 7.0 |
| `list_processes` | `GET /processes` | 7.1 |
| `retry_queue_item` | `POST .../items/{id}/attempts` (201 → `{attemptId}`) | 7.2 |
| `defer_queue_item` | `PATCH .../attempts/{attemptId}` (RFC 6902 JSON Patch) | 7.2 |
| `start_process` | `POST /sessions` → UUID, then `PATCH /sessions/{id}` `{status: Running}` | 7.1 |
| `set_schedule_enabled` | `PUT /schedules/{id}` (retire/unretire) | 7.0 |
| `trigger_schedule` | `POST /schedules/{id}/runs` (optional `startTime`) | 7.2 |

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
- **`GET /user/permissions` (7.1+)** returns the service account's Blue Prism
  permissions; the Phase 5 capability resolver queries it at startup and
  registers only the action tools the account can actually execute.
- Every endpoint documents required Blue Prism user permissions (e.g. queue
  writes need *Full Access to Queue Management*) — deployment docs (Phase 6)
  must cover service-account permission mapping.

**Needs day-one verification against a live estate** (the spec underdocuments
both): the JSON Patch paths accepted by the attempt PATCH (`/deferredDate` is
inferred from the item schema), and the retire/unretire body for the schedule
PUT (`ScheduleSummary` carries `isRetired` and the PUT documents retire
permissions, but the published request schema omits the flag).

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
5. **Governance** — capability gating, audit, and dry-run; gates Tier 3.
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

## Conventions
- The stdio transport speaks JSON-RPC over stdout; nothing else may write there.
  Silence noisy library loggers before and after importing heavy dependencies.
- PII audit logging records entity *types* only, never raw content, and writes
  to files, never stdout.
- Lint clean, tests passing, and the coverage gate met before each phase lands.

# blue-prism-v7-mcp — design & build plan

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
  support for 7.2+. The one tool above the 7.2 floor is
  `list_queue_configurations` (7.4): rather than raise the floor for a single
  read, it degrades on a 7.2/7.3 estate — a 404 (or a denied read) returns an
  empty envelope with a `meta.unavailable` note, so the rest of the surface is
  unaffected.
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
- **`demo` is a populated sibling of `mock`, not a replacement (v0.8.0).**
  `BP_DATA_SOURCE=demo` runs the same `MockBPClient` seeded with `demo_estate()`
  — a lively, realistic estate (pooled workers across departments, queues in
  varied health including a stalled-but-not-empty one, in-flight and silently
  stale sessions, a failed schedule, and a deterministic ~180-day session
  history with real weekday/weekend and error-mix shape) so a demo or a
  downstream console has something worth looking at end-to-end. The lean
  default `mock` fixtures stay untouched as the minimal substrate the unit
  tests assert against — `demo` is additive, evaluation-only, and carries no
  tool-surface or control-plane change. Fixture timestamps for both are
  anchored relative to import time, never a hardcoded calendar date, so
  neither estate drifts stale.
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
- `list_queue_items` — **requires a queue + state + date window** (queues run to
  millions of items; no estate-wide item listing), UNLESS the query is already
  scoped by `within_sla` (breached/not-yet-breached) and/or `sla_before` (an
  approaching-SLA upper bound), in which case the window is optional. Also
  takes `sort_by="loadedDate asc"` for the exact oldest item first (server-side
  sorted, so a max-pages-capped fetch still returns the true oldest). Envelope-capped.
  The domain method (`Engine.list_queue_items`, embeddable-core only — v0.15.0)
  additionally takes `max_records`, a fetch-time cap that stops paging as soon
  as enough rows are collected (e.g. `sort_by="loadedDate asc", max_records=1`
  for "the single oldest item" without paging a whole queue's history). Not on
  the MCP tool: early-stop is only correct paired with a server-side sort, a
  pairing rule an embeddable-core caller controls but an MCP caller can't be
  trusted to hold.
- `get_queue_item` — one item in full **including its payload `data`** (the only
  read that returns it). The `data` DataCollection is scrubbed type-aware: free
  text through the scrubber, passwords redacted, binary/image dropped, scalars
  kept, nested collections recursed. (Blue Prism cannot return data for
  application-server-encrypted queues — that call fails; use the no-data tools.)
- `list_item_attempts` — an item's attempt history (no payload data; the
  exception reason at each attempt is scrubbed)
- `list_sessions` — run history; filter by process/resource/status/date
- `get_session` — one session's detail by id (no date window), PII-scrubbed
- `get_session_log` — stage-level log for one session (PII-scrubbed, size-capped).
  Two optional server-side filters narrow a long run: `errors_only` returns just
  the exception-handling stages (Exception/Recover/Resume), and a
  `start_date`/`end_date` window bounds the stages' execution time — so "why did
  this fail?" need not drag the whole log back
- `list_resources` — digital workers + status
- `list_schedules` — the schedule catalogue + retirement state, each schedule's
  last run folded in (status, start/end, duration) from the schedule run logs,
  added only where a schedule has actually run. (The API still holds no next-run
  field anywhere.) The fold is one newest-first sweep of the plural schedule
  log — not a call per schedule — with a per-schedule fallback only for
  long-dormant stragglers the swept window missed.
- `get_schedule` — one schedule's full definition by name or id: beyond the
  list row, the complete timing definition (interval type, start/end dates,
  time zone, DST flag, and the per-interval details carrying calendar ids) —
  the whole input for reasoning about when a schedule should run.
- `list_schedule_tasks` — one schedule's task chain in execution order (walked
  from its initial task, success path first), each task carrying its failure
  policy, its on-success/on-failure links, and a folded `sessions` list —
  the process each scheduled session runs and the worker it targets. If the
  per-task session read fails, the tasks stand and
  `meta.sessions_unavailable` is set.
- `list_schedule_logs` — schedule run history, newest first: estate-wide in
  one call (each row names its schedule) or scoped to one schedule, filtered
  by outcome status and/or a start-time window. "What ran overnight and what
  failed?" as a single read.
- `list_processes` — published process catalogue
- `list_queue_configurations` — the active queues' process→queue map: each
  active queue's assigned process and resource group plus its live activity
  (active sessions, available resources, time/ETA to clear). Needs 7.4+;
  degrades to an "unavailable" note on an older estate rather than failing.
- `list_resource_pools` — the resource pools (groupings of digital workers),
  their member counts and database status (a bare array endpoint — no paging)
- `list_environment_variables` — the shared process-configuration variables;
  the value is scrubbed type-aware (the same fail-closed policy as the
  queue-item payload, keyed on the variable's Blue Prism data type)
- `list_process_groups` — the process tree (folders and processes) so an agent
  can see how the catalogue is organised

### Tier 2 — Insight (separate derived tools)
- `exception_summary` — exceptioned items for one queue + window, grouped by
  *scrubbed* exception reason (grouping after scrubbing folds messages that
  differ only in personal data into one bucket)
- `estate_exception_summary` — the same grouping across *every* queue in one
  call (each reason group also recording which queues exhibit it), so the
  dominant failure mode across the estate is one call rather than a loop over
  each queue
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
- `retry_queue_item` / `defer_queue_item` / `create_queue_items`
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
| `get_session_log` | `GET /sessions/{id}/logs` (`logslight` on 7.4+); `errors_only`→`stageType=Exception,Recover,Resume`, window→`resourceStartTime[gte]/[lte]` | 7.0 |
| `list_schedules` last-run | `GET /scheduleLogs` (→ `ScheduleLogSummary`, one newest-first sweep grouped by `scheduleId`; per-schedule `GET /scheduleLogs/{id}` only as the straggler fallback — not the deprecated `/schedules/{id}/logs`) | 7.1 |
| `list_resources` | `GET /resources` | 7.0 |
| `list_schedules` | `GET /schedules` | 7.0 |
| `get_schedule` | `GET /schedules/{id}` (→ `ScheduleDefinitionResponseModel`, the full interval definition; ids are integers) | 7.1 |
| `list_schedule_tasks` | `GET /schedules/{id}/tasks` (→ `ScheduledTask[]`, bare array; chain-linked by `onSuccessTaskId`/`onFailureTaskId`) + `GET /schedules/tasks/{taskId}/sessions` per task (→ `{processName, resourceName, taskSessionId}[]` — names, not ids) | 7.0 |
| `list_schedule_logs` | `GET /scheduleLogs` / `GET /scheduleLogs/{id}` (`scheduleLogStatus` Capitalised in the query, lowercase in responses; window as `startTime[gte]/[lte]`) | 7.1 |
| `list_processes` | `GET /processes` | 7.1 |
| `list_queue_configurations` | `GET /workqueues/configurations` (→ `WorkQueueConfigurationSummary`, active queues only) | **7.4** |
| `list_resource_pools` | `GET /resources/pools` (→ `ResourcePool[]`, bare array, no paging) | 7.1 |
| `list_environment_variables` | `GET /environmentvariables` (→ `EnvironmentVariable`, `value` scrubbed type-aware) | 7.2 |
| `list_process_groups` | `GET /processgroups/root/descendants` (→ `ProcessGroupItem[]`, Item/Group nodes) | 7.2 |
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
  `WorkQueueItemNoData` (list rows AND attempt-history rows) also carries
  **`sessionId`** — the session that worked the item/attempt, the
  item→session→resource correlation (`resource` is the worker's name;
  `sessionId` is the session that produced it). There is no item-level
  exception-type field anywhere in the API — system-vs-business
  classification is derived by following `sessionId` to
  `get_session`/`SessionSummary.exceptionType`. Rows also carry the
  `sla`/`slaDatetime` pair (SLA-breach signal), `loadedDate` (true item
  age), `processName`, and `tags`. The single-item `WorkQueueItem` carries
  the identical field set plus `data`, but spells the SLA field
  `slaDateTime` (capital T) where the NoData shape spells it `slaDatetime`
  — a real API inconsistency, not a typo in this doc.
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
  7.5.1):** three of the five are consumed (v0.4.0 spec-pin, field-by-field on
  7.5.1). `currentLimitsAndUsage` (licence limits vs current usage — concurrent
  sessions, runtime resources, published processes) backs `estate_health`'s
  licence block. `licensesEntitlement` (no params, single object) adds the
  *entitlement* side that nothing else carries — `activeLicenseTypes` plus
  per-tier ceilings split `enterprise`/`desktop` — and backs the
  `license_entitlement` tool. `workQueueCompositions` is *almost* redundant:
  `GET /workqueues`'s `WorkQueueSummary` already carries every per-state count
  (pending/completed/locked/exceptioned/total + `averageWorkTime`) **except**
  `deferred`, so it is consumed only for that one count, which `list_queues`
  folds into its rows (it requires `workQueueIds`, so the call covers just the
  queues being returned, and degrades to omitting `deferred` if denied). The two
  resource-utilization endpoints stay out **as raw reads** — not because they
  belong to any one consumer, but because of their *shape*:
  `resourceUtilization` is a heat-map feed (one row per worker per day, a
  24-integer array of minutes-worked-per-hour) and `resourcesSummaryUtilization`
  is an unlabelled aggregate time-series (`{usagehour, usage}`, no per-worker
  breakdown). Both are chart series, not the point facts an LLM tool surface
  wants. The genuinely useful form — a *derived* per-worker "% of available
  minutes worked over a window" — is a real value-add for any consumer, but it
  needs a deliberate aggregation design and page-*number* paging support (this
  is the API's only page-number-paged read; the client today does
  none/token/offset), so it is deferred to its own utilisation-insight release
  (see backlog). Nothing under `/dashboards` aggregates exceptions or throughput.
- **Context & topology reads (v0.5.0, pinned field-by-field on 7.5.1 + the
  7.2/7.4 specs).** `workqueues/configurations` (7.4) answers
  `WorkQueueConfigurationSummary` for *active* queues only — `id`, `name`,
  `activeWorkQueueConfiguration{assignedProcessId, assignedResourceGroupId}`,
  and `activeQueueStats{activeSessions, availableResources, timeRemaining,
  elapsedRemaining, ETA}`; it is a separate tool, not a fold into `list_queues`,
  because the population and shape differ (the opinionated queue→process *name*
  mapping stays a console concern; the tool exposes the generic id-based
  primitive). `resources/pools` (7.1) is a *bare array* with no paging envelope
  (`ResourcePool{id, name, members, databaseStatus}`; `databaseStatus` enum
  Unknown/Ready/Offline/Pending). `environmentvariables` (7.2) is token-paged
  `EnvironmentVariable{id, name, description, dataType, value}` — `dataType`
  carries the same Blue Prism type vocabulary as a `DataValue`, so the `value`
  (a config payload that can hold a secret or PII) runs through the identical
  fail-closed type-aware scrub as the queue-item payload. `processgroups/root/
  descendants` (7.2) is token-paged `ProcessGroupItem{id, name, nodeType
  (Item/Group), lastModified}` — a flat descendant list of the process tree.
- **`ScheduleSummary` is the schedule definition only** (interval fields,
  `isRetired`; its `id` is an *integer*, unlike every other entity's UUID).
  No next-run field exists anywhere in the API; per-run history lives in the
  schedule run logs. `list_schedules` folds in each schedule's last run from
  **`GET /scheduleLogs/{scheduleId}`** (the current endpoint — `/schedules/logs`
  and `/schedules/{id}/logs` return the spec's *deprecated* page type), read
  newest-first and capped to one row. `ScheduleLogSummary` =
  `scheduleLogId, startTime, endTime, duration, status` (the run outcome enum
  pending/running/terminated/completed/partExceptioned), `serverName,
  scheduleId, scheduleName`; the fold keeps status + the three timings.
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
case-insensitively, but the spec never shows a real response. v0.5.0 adds one
more: the `environmentvariables` `value` is typed `object` (nullable) in the
spec with no inner shape, so whether it arrives as the raw typed value or a
`DataValue` wrapper is unverified — the type-aware scrub keys on the variable's
sibling `dataType` and works either way, but the exact value shape wants a live
check.

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
  - *Insight (DELIVERED v0.4.0):* the dashboard aggregates were spec-pinned
    field-by-field on 7.5.1 — `licensesEntitlement` shipped as the
    `license_entitlement` tool and `workQueueCompositions`' one net-new datum
    (`deferred`) folded into `list_queues`. Schedule run-history is still Phase 9.
  - *Utilisation insight (DELIVERED v0.10.0):* a `resource_utilization` Tier-2
    tool aggregating `resourceUtilization`'s 24h heat-map into per-worker "% of
    available minutes worked over a window" — genuinely useful to any consumer,
    not console-only. Held back from v0.4.0 because it needed (a) page-*number*
    paging support added to the client (the API's only such read) and (b) a
    deliberate aggregation/denominator design. Shipped as its own small release.
    **Design resolved by an L2-first consumer spike (Custera):**
    - *Contract shape — return the per-worker daily grain, not a collapsed
      scalar.* The raw feed is already per-worker-per-day; returning that grain
      plus a windowed roll-up is both more opinion-free and pre-satisfies the
      demanding consumer (a windowed trend). Collapsing to one window would be
      the engine *adding* opinion. Alongside each % return the raw
      worked-minutes so no consumer is locked into our denominator.
    - *Denominator = wall-clock minutes in the window* (24 × days) — the honest,
      opinion-free choice. "vs. scheduled/online minutes" is a consumer
      reframe and stays out of L1. An offline day counts as 0% worked against
      full wall-clock (idle *is* the signal), not excluded from the denominator.
    - *Estate roll-up = total-worked ÷ total-wall-clock* (true estate duty
      cycle), not a mean of per-worker %s, which diverge when windows differ.
    - *L1/L2 line:* L1 returns numbers (paging, raw read, the mechanical
      minutes-worked ÷ wall-clock aggregation). Thresholds, "saturated"/severity,
      trend persistence, and any denominator reframe are the consumer's (L2).
      Leak test: the moment a `saturated` flag or severity band is wanted in the
      return, opinion has crossed down.
    - *Paging:* add a generic `_get_paged_by_number` helper, not a one-off —
      future page-number reads may appear.
    - *Skip* `resourcesSummaryUtilization`: the per-worker feed sums to the same
      estate figure, so the aggregate endpoint is redundant.
    - *Mock:* seed a plausible per-worker heat-map in the demo/mock estates so
      downstream consumers can exercise it (engine-PR-first: a consumer's CI may
      build against engine `main` HEAD).
  - *Context/console:* `workqueues/configurations` (the process→queue map console
    severity needs — L2), `resources/pools`, environment-variable reads, process groups.
- **North star (not a near-term gap):** 7.5's `subscriptions` PATCH and work-queue
  item `callbacks`/webhooks are the event-driven orchestration plumbing for the
  watch-and-react capability.

### Beyond v1 (phases 8+)
- **Phase 8 — Embeddable core.** *Shipped in v0.6.0.* A tool's logic and its
  presentation were one body: each closure resolved names, scrubbed, sorted, and
  wrapped the result in the LLM-shaped top-N envelope in a single pass. They are
  now split — the domain logic lives on a first-class `Engine` facade
  (`blue_prism_v7_mcp.Engine`), one method per Tier 1/Tier 2 read, returning the
  full ranked records (a `Ranked` for list tools, a dict for single reads/
  composites) with no truncation; the envelope is one representation adapter
  (`to_envelope`, with `rank` as the domain sort) and the MCP tool layer is a
  thin set of closures over the engine. A host embedding the engine in-process
  consumes `Ranked.records` and applies its own representation instead of
  re-deriving the logic. The cache got the same treatment: a `Cache` protocol
  with a **thread-safe, injectable** `TTLCache` default (`blue_prism_v7_mcp.cache`),
  injected via `BPClient(config, cache=...)`, for a long-lived multi-threaded
  host sharing a client across workers. No tool gained or lost behaviour; the
  v0.1 surface is unchanged.
- **Phase 9 — Fuller v7 read coverage.** *Shipped in v0.7.0.* Pushes the
  filtering the v7 API already supports down into the reads the envelope capped
  client-side. `get_session_log` gained an `errors_only` filter (server-side
  `stageType=Exception,Recover,Resume`) and a `start_date`/`end_date` window
  (`resourceStartTime` range), ordered newest-stage-first server-side, so a long
  run is no longer dragged back in full to surface its failure. `list_schedules`
  gained last-outcome enrichment from `GET /scheduleLogs/{scheduleId}` — the
  run-history read v1 deferred — so a schedule carries its last result
  (status, start/end, duration), folded in only where it has run.
  `estate_exception_summary` is the estate-wide grouping across every queue, so
  the dominant failure mode is one call rather than a loop over each queue.
- **Phase 10 — `stop_session` (Tier 3).** *Pulled forward into v0.2.0 — see
  Post-v0.1.0 releases above.* The missing control sibling of
  `start_process`: `PATCH /sessions/{id}` with `{status: Stopped}`, the same
  endpoint `start_process` already drives for `{status: Running}`. Capability-
  gated, audited, and dry-run by default like every Tier 3 tool. It carries
  `start_process`'s permission clause (a process permission **and** `Control
  Resource`); the Stopped transition is a day-one live-verification item — the
  same posture as the v1 writes the spec underdocuments.
- **Phase 11 — `actor` on the audit line.** The Tier 3 audit records the tool,
  its arguments, and the event status; identity — *who* invoked it — is a
  generic governance concern (it belongs here, not in a consumer). A first
  pass threaded `actor` as a **build-time** parameter (`register_tools` →
  `build_tier3_tools` → `_run` → `AuditLog.record`, fixed once per
  registration) but that shape cannot fit a host like the Custera console:
  its `ControlFacade` builds the engine's tool closures **once**, long-lived
  and shared, while `ControlService.propose`/`.execute` take a *different*
  `Actor` **per call** on that same shared facade. A build-time actor can
  only express one identity per built tool set.
  The shape that fits: `bind_actor(actor, scrub_text)` in `governance.py`, a
  context manager over an ambient `contextvars.ContextVar` that
  `AuditLog.record` reads — never a tool parameter, so identity never enters
  the model-facing schema, and `build_tier3_tools`/`register_tools` need no
  `actor` param at all. `scrub_text` is the same cached scrub function every
  other tool-boundary field already goes through (`make_cached_scrub`, built
  once — e.g. an engine's `scrub_text`), not a raw `Scrubber`: code review on
  the first draft surfaced that an actor identity recurs across a session's
  calls at least as often as row text does, so it belongs behind the same
  cache rather than re-scrubbing on every bind. A host wraps each dispatch in
  it (e.g. Custera's `ControlService` wraps each
  `self._facade.dry_run(...)`/`.execute(...)` call in
  `with bind_actor(actor.sub, engine.scrub_text): ...`) and every audit line
  written inside picks up that identity, scrubbed like any other name.
  `ContextVar` propagates through `asyncio.create_task` and
  `asyncio.to_thread`/anyio's `to_thread.run_sync` (the path FastAPI's
  sync-route dispatch uses) — verified, not assumed, since both explicitly
  copy the calling context before handing off. It does **not** propagate into
  a bare `loop.run_in_executor` call, which submits to the executor with no
  context copy; a host dispatching that way must route through
  `asyncio.to_thread` instead or lose the bound actor silently. The
  standalone server never binds one, so its audit lines are unaffected.
  Consuming this from Custera's `ControlService` (wiring `bind_actor` around
  each dispatch, retiring the `proposed_by`/`executed_by` store workaround or
  keeping it alongside) is a Custera-side change, tracked there.

## Conventions
- The stdio transport speaks JSON-RPC over stdout; nothing else may write there.
  Silence noisy library loggers before and after importing heavy dependencies.
- PII audit logging records entity *types* only, never raw content, and writes
  to files, never stdout.
- Lint clean, tests passing, and the coverage gate met before each phase lands.

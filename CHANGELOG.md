# Changelog

All notable changes to **blue-prism-v7-mcp** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, every
additive endpoint is a minor bump.

## [Unreleased]

## [0.15.0] — 2026-07-16

### Added
- **`max_records` on the queue-items domain read (embeddable-core only).**
  `Engine.list_queue_items` (and the underlying `BPClient.get_queue_items`)
  gain an optional fetch-time cap that stops paging as soon as that many
  rows are collected, instead of always walking a queue's full token-paged
  history. Paired with `sort_by="loadedDate asc"`, `max_records=1` answers
  "the single oldest item in this queue" in one page instead of an
  unbounded sweep. Deliberately not exposed on the MCP tool surface:
  early-stop is only correctness-safe alongside a server-side sort that
  puts the wanted rows first, a pairing rule an embeddable-core caller
  (e.g. a console reading its own Overview aggregate) can hold but an MCP
  caller has no way to know to apply.

### Fixed
- **Two schedule-trigger mock tests used a hardcoded calendar date/time as
  the "most recent run" expectation.** `get_last_schedule_run` picks the
  run with the latest `startTime` across a schedule's *entire* history,
  fixture rows included, and the demo/mock estate's own seeded schedule
  runs are anchored relative to real wall-clock time (`_ts(1, ...)` —
  "yesterday"). A fixed past/future literal in the test eventually drifts
  behind (or, for the future one, into) that moving anchor and the
  assertion starts reading the seeded fixture row instead of the one the
  test just triggered. Both tests now derive their `start_time` from the
  injected clock instead.

## [0.14.0] — 2026-07-12

### Added
- **Mock write-fidelity — governed writes now visibly mutate the demo
  estate.** `MockBPClient` gained injectable time (`now_fn`, `settle_after`)
  so tests can drive the clock deterministically, and a lazy `_settle` pass
  that auto-completes in-flight runs and schedule logs on read without
  touching the seeded stale/Running fixtures the severity demos rely on.
  Every write method now produces an observable state change: `start_process`
  occupies the worker, bumps usage, and seeds a session log; `stop_session`
  releases the worker and closes the log; `retry_queue_item` and
  `defer_queue_item` keep queue counts consistent and grow attempt history;
  `trigger_schedule` and `stop_schedule` append and close schedule-log rows.
  Also fixed a cross-instance bug where the default fixture lists were
  shallow-copied, letting one client instance's writes mutate another's data.

### Fixed
- **`trigger_schedule` no longer poisons the mock on non-canonical
  `start_time` input.** The caller's value was stored verbatim, but settling
  reads re-parse it with a strict format — any valid ISO variant other than
  the exact canonical form (an offset, a date-only string, fractional
  seconds) made every later settling read raise `ValueError` permanently,
  with no recovery path. Now normalised to the canonical form once, at
  ingest; duration calculations also floor at zero so a future-dated run
  stopped early can't report a negative duration.
- **`_validate_collection_rows` now canonicalises queue-item data rows in
  place**, not just session parameters — a lowercase `valueType` inside a
  queue item's `data` (including nested Collections) is normalised before it
  reaches the POST body and the audit shape, matching this changelog's own
  0.13.0 claim that canonicalisation applies identically to both paths.

## [0.13.0] — 2026-07-11

### Added
- **`create_queue_items` — batch work injection (Tier 3).** A new governed
  action tool that injects items into a work queue for digital workers to
  process. Batch-first (the API body is always an array; a single create is a
  batch of one). Carries the same governance contract as every Tier 3 tool:
  dry-run by default, capability-gated on `Full Access to Queue Management`,
  and audit-logged with field names/types only (never data values — a field
  can be a Password). Validates the full item shape locally before reaching
  the API: unknown keys fail loudly naming the typo, data rows are validated
  recursively (same rules as session parameters), and each typed field is
  checked with an indexed error message.

### Changed
- **`validate_session_parameters` refactored.** The per-value validation loop
  is extracted into a shared `validate_data_value` helper that both session
  parameters and queue-item data rows call — zero behaviour change on the
  session-parameter path (existing tests pass untouched), but the
  canonicalisation and validation now apply identically to queue-item
  DataCollection payloads including nested Collection recursion.

## [0.12.0] — 2026-07-10

### Added
- **`list_queue_items` gains SLA-aware narrowing and an exact-oldest sort.**
  Three new optional filters/sorts on top of the existing state + date-window
  read:
  - `within_sla` (bool) sends the API's computed `withinSla[eq]` filter —
    `within_sla=false` answers "every currently-breached item in this
    queue" without also requiring a date window, since the SLA filter is
    itself sufficient scope. `within_sla=true` narrows to items still ahead
    of their deadline.
  - `sla_before` (ISO) sends `slaDateTime[lte]`, an approaching-SLA upper
    bound — also scope enough on its own, so the date window relaxes here
    too.
  - `sort_by="loadedDate asc"` asks the API to sort server-side
    (`sortBy=LoadedDateAsc`) and re-ranks the result by `loadedDate`
    ascending instead of the default `lastUpdated desc` — the exact oldest
    pending item, not a `lastUpdated`-desc truncated approximation.
  - The mandatory date window on `list_queue_items` now relaxes to optional
    (still validated if given) whenever `within_sla` and/or `sla_before` are
    passed; `state` stays a required filter either way.
  - `mock.py`'s `get_queue_items` implements the same narrowing/sort
    deterministically against the existing SLA-shaped fixture rows (no new
    fixtures needed — several default/demo-estate items were already
    breached or ahead of their SLA).

## [0.11.1] — 2026-07-09

### Fixed
- **Queue-item response models were field-incomplete in the mock fixtures,
  docstrings, and DESIGN.md.** A spec audit against the real 7.5.1 (and
  7.2.0) `WorkQueueItemNoData`/`WorkQueueItem` schemas found the mock only
  ever fixtured about half their fields — most notably **`sessionId`** (the
  item→session/resource correlation, present since at least 7.2.0), plus
  `sla`/`slaDatetime`, `loadedDate`, `deferredDate`, `lockedDate`, `ident`,
  `tags`, `processName`, `isSuggested`, and `attemptWorkTimeInSeconds`. The
  client/tool pipeline already passed every field through raw — a real
  estate was never missing anything — but the mock is the de facto contract
  for anyone developing against this package without a live estate, so an
  unfixtured field was effectively an invisible one.
  - `mock.py`'s three item-fixture sites (the default queue items, the
    default item-attempt history, and the demo estate's queue items) now
    carry the full field set, with `sessionId` correlated to a same-resource
    session fixture in the same estate (`None` on never-worked items).
  - `get_queue_item` now renames the list/attempt shape's `slaDatetime`
    (the API's own typo) to `slaDateTime` when it composes the single-item
    read, matching the real `WorkQueueItem` schema exactly.
  - `client.py`'s `get_queue_items`/`get_queue_item`/`get_item_attempts`
    docstrings now document `sessionId` and the rest of the field group, and
    DESIGN.md's item-shape bullet names them and the `slaDatetime`/
    `slaDateTime` spelling split.
  - **New CI guard** (`tests/test_fixture_parity.py`): every fixture row's
    keys must be a subset of the verified `WorkQueueItemNoData`/
    `WorkQueueItem` field lists (plus the known mock-internal `queue` key),
    and every schema field must appear in at least one row — so a future
    field this file forgets to fixture fails CI instead of waiting for
    another manual spec audit. Scoped to these two models only; other
    response shapes don't have a verified field list banked yet.

## [0.11.0] — 2026-07-02

### Added
- **`get_schedule`** — one schedule's full definition by name or id: the
  complete interval definition (start/end dates, time zone, DST flag, and the
  per-interval detail objects carrying their calendar ids) that the list row
  does not carry — the whole input for reasoning about when a schedule should
  run (`GET /schedules/{id}`, 7.1+).
- **`list_schedule_tasks`** — one schedule's task chain in execution order,
  walked from its initial task (success path first, failure branches after,
  unreachable tasks last), each task folding in its sessions — the process it
  runs and the worker it targets (`GET /schedules/{id}/tasks` +
  `GET /schedules/tasks/{taskId}/sessions`, both 7.0+). A denied/failed
  session read degrades visibly via `meta.sessions_unavailable`.
- **`list_schedule_logs`** — schedule run history, newest first: estate-wide
  in one call or scoped to one schedule, filtered by outcome status and a
  start-time window (the plural `GET /scheduleLogs`, 7.1+ — the current log
  family, not the spec-deprecated `/schedules/logs`).
- **Task-chain fixtures in the mock and demo estates** — the demo's failed
  Nightly Payment Run is a branching chain with an on-failure alert task, and
  the compliance task fans out across two workers, so the chain walk and
  session fold are exercisable offline.
- **`SECURITY.md`** — the security model in one place (trust boundary, what
  leaves the process, the three action-surface layers, deployer
  responsibilities) plus a private vulnerability-reporting channel and the
  pre-1.0 support statement. Operational detail stays in DEPLOYMENT.md.
- **PEP 561 `py.typed` marker** — the package ships its inline type hints, so
  embedders' type checkers see real signatures instead of `Any`.
- **mypy type-check gate in CI** (`mypy` on `src/`, pinned version in the dev
  extras); the codebase is mypy-clean. Mixin attributes provided by the
  composing `Engine` (`client`, `scrub_text`) are now declared structurally on
  the tier mixins rather than by docstring convention.
- **CI matrix: Python 3.11, 3.12, and 3.13** — every version `requires-python`
  admits is now tested, not just 3.11.
- **`ruff format --check` in CI** — formatting drift was previously invisible
  to CI (it runs `ruff check` only) and had to be caught by hand; swept the
  outstanding drift (verified formatting-only via AST diff) in the same change.

### Fixed
- **`BPConfig.client_secret` no longer appears in the config's `repr`** — a
  host that logged or debug-printed its config object would have echoed the
  credential.

### Changed
- **`list_schedules`' last-run fold is now one sweep, not N requests** — a
  single newest-first read of the plural schedule log covers every recently
  run schedule (the first row seen per schedule is its last run), with a
  bounded per-schedule fallback only for long-dormant stragglers. The worst
  case is the previous behaviour; the common case is one request per refresh.
  This was the polling bottleneck at hundreds of schedules.
- **Relicensed from proprietary to Apache-2.0** ahead of public distribution:
  canonical licence text in `LICENSE`, copyright in `NOTICE`, and PEP 639
  SPDX licence metadata in the package (`license = "Apache-2.0"`,
  `license-files`; build backend floored at setuptools 77 accordingly).
  Permissive with an explicit patent grant — the right posture for an
  enterprise-facing distributable.
- **Renamed the project `blue-prism-mcp` → `blue-prism-v7-mcp`** (distribution,
  console script, and import package `blue_prism_mcp` → `blue_prism_v7_mcp`),
  matching the repository and stating the actual scope: this server targets the
  v7 Enterprise REST API specifically, not Blue Prism generally. Breaking for
  embedders (update the dependency name and imports); the environment contract
  (`BP_*`) and the tool surface are unchanged. Done pre-publication, so no
  released artifact carries the old name.

## [0.10.0] — 2026-07-01
Utilisation insight: the `resourceUtilization` heat-map, aggregated into a
per-worker duty cycle. Closes out the deferred estate-insight item from
v0.4.0. No control-plane change.

### Added
- `resource_utilization(start_date, end_date)`: aggregates the
  `resourceUtilization` heat-map (one row per worker per day, 24 hourly
  minutes) into per-worker daily and windowed worked-minutes/wall-clock-
  minutes/utilisation percentages, plus an estate-wide duty cycle (total
  worked over total wall-clock — not a mean of per-worker percentages, which
  would diverge when workers' reporting spans differ). An idle day counts as
  0% against the full window rather than being excluded from the denominator,
  so the figure reflects true availability. Mechanical L1 aggregation only —
  no thresholds or "saturated" opinion; that stays a consumer's L2 call.
  Degrades to an `unavailable` note if the read is denied or fails, like the
  other `/dashboards` reads.
- `BPClient._get_paged_by_number`: a generic page-NUMBER-paged fetch helper
  (`pageNumber`/`pageSize`), backing `get_resource_utilization` —
  `resourceUtilization` is the API's only endpoint on this scheme; everywhere
  else is token-paged.

## [0.9.0] — 2026-07-01
Governance hardening for long-lived embedding hosts: identity on the audit
line and a memory bound on the cache. No tool surface or control-plane change.

### Added
- `bind_actor(actor, scrub_text)` in `governance.py`: a context manager that
  binds a per-call identity onto every Tier 3 audit line written inside it,
  via an ambient `contextvars.ContextVar` that `AuditLog.record` reads. An
  embedding host wraps each dispatch to its (long-lived, shared) tool set in
  this — `actor` is never a tool parameter, so identity never enters the
  model-facing schema and a single built tool set can attribute a different
  identity to each call. `scrub_text` is a cached scrub function
  (`make_cached_scrub`, e.g. an engine's `scrub_text`), the same one every
  other tool-boundary field already goes through, not a raw `Scrubber` — an
  actor identity recurs across a session at least as often as row text does.
  Propagates through `asyncio.create_task` and `asyncio.to_thread`/anyio's
  `to_thread.run_sync` (the path FastAPI's sync-route dispatch uses); does
  **not** propagate into a bare `loop.run_in_executor` call. The standalone
  server never binds one, so its audit lines are unaffected.

### Fixed
- `TTLCache` now purges expired entries on every `set()`, not just when the
  same key is re-read — a key written once and never requested again used to
  sit in the store forever. Bounds memory growth for a long-lived embedded
  host doing many distinct per-id reads over days or weeks. No API or
  behaviour change for reads.

## [0.8.0] — 2026-07-01
A populated demo estate, so the server (and a downstream console) can be
evaluated end-to-end without a live Blue Prism estate. No tool surface or
control-plane change — fixtures and mock-mode behaviour only.

### Added
- `BP_DATA_SOURCE=demo` (and `demo_estate()`): a populated offline estate built
  on the same `MockBPClient` — pooled workers across departments (working/idle/
  offline), queues in varied health (an SLA-breaching backlog, a degrading one, a
  stalled-but-not-empty one, a paused one, plus a healthy flowing bulk),
  in-flight and silently-stale `Running` sessions, and a failed schedule. Lets
  the server be evaluated end-to-end against a lively estate, while the lean
  default fixtures stay the minimal substrate the unit tests assert against.
  Behind the foreground sessions the estate also carries a deterministic
  ~180-day backlog of finished runs — weekdays busier than weekends, a
  termination fraction that worsens over the most recent fortnight, and a
  genuine mix of process/internal error reasons — so a downstream throughput
  history and STP-rate trend have real, varied shape rather than a flat recent
  week.

### Changed
- The mock estate now seeds one in-flight (`Running`) session, so mock mode
  exercises the live-session reads (worker `current_sessions`, in-flight
  staleness severity) and the `start_process` → `stop_session` workflow against a
  standing target rather than only a freshly started one.
- Mock fixture timestamps are now anchored relative to "now" (a module-level
  anchor captured at import, offset by day) rather than hardcoded to a fixed
  calendar date, so the estate always reads as the current day/week instead of
  drifting stale. Date-dependent tests compute their expectations off the same
  anchor helpers, so they stay green as time passes.
- The demo estate's queue fixtures now distinguish a deep-but-flowing backlog
  (items locked and being worked) from a stalled one (pending work, nothing
  locked) — a pending count alone isn't a problem, an undrained one is.

### Fixed
- The demo history's terminated runs always carried the same failure reason
  (`ProcessError`) — an always-true guard made `InternalError` unreachable, so
  `throughput_summary`'s process-vs-internal error split was silently always
  zero on the backlog. The reason now varies by day, giving the backlog a real
  mix of both.

## [0.7.0] — 2026-06-16
Deeper reads — pushing the filtering the v7 API already supports down into the
reads the envelope capped client-side, so a diagnosis fetches only what it needs.
No new estate concepts, no change to the control surface.

### Added
- `estate_exception_summary` — the estate-wide sibling of `exception_summary`:
  it groups exceptions across **every** queue in one call (grouped by scrubbed
  reason, so messages differing only in personal data fold into one bucket),
  each reason group also recording which `queues` exhibit it. The dominant
  failure mode across the estate is now one tool call rather than a loop over
  each queue. Window-scoped and required, like the per-queue summary.
- `get_session_log` gains two optional server-side filters: `errors_only=True`
  returns just the exception-handling stages (Exception/Recover/Resume — the
  Blue Prism error markers, since the API exposes no boolean error flag), and
  `start_date`/`end_date` bound the stages' execution time
  (`resourceStartTime` range). The read is ordered newest-stage-first
  server-side, so a long run is no longer dragged back in full to surface its
  failure.

### Changed
- `list_schedules` now folds each schedule's **last run** into its row — status
  (completed/terminated/running/pending/partExceptioned), start and end times,
  and duration — read from `GET /scheduleLogs/{scheduleId}` (the current
  endpoint; `/schedules/logs` is the spec's deprecated variant), newest-first
  and capped to one row. The outcome is added only where a schedule has actually
  run (never a fabricated one); if the schedule-log read is denied or fails the
  listing still stands and sets `meta.last_run_unavailable`. The API still
  exposes no next-run field.
- New client reads `BPClient.get_last_schedule_run` and the filter arguments on
  `BPClient.get_session_log`; `MockBPClient` mirrors both, and its session-log
  and schedule-log fixtures gain the stage types, per-stage times, and run
  history the filters exercise. These are additive — embedders on the v0.6.0
  engine surface keep their existing calls.

## [0.6.0] — 2026-06-15
Embeddable core — an internal architecture release, no new endpoints and **no
change to the tool surface or behaviour**. It splits each read tool's logic from
its presentation and makes the cache injectable, so a host can embed the engine
in-process (consuming ranked records to apply its own representation) and share
one client safely across worker threads.

### Added
- `Engine` — a first-class facade over the read surface (`blue_prism_v7_mcp.Engine`)
  with one method per Tier 1 visibility and Tier 2 insight tool. Each returns the
  *domain* result — the full relevance-sorted records, already scrubbed at the
  PII boundaries, with **no top-N truncation** — as a `Ranked` (list tools) or a
  plain dict (single reads / composites). Name resolution and loud input
  validation live in the domain methods, so an embedder gets them too.
- `Ranked` — the domain result type (`records`, `sorted_by`, `meta`); `rank()`
  (sort → `Ranked`) and `to_envelope()` (the one representation adapter: top-N +
  meta) in `tools.common`. The existing `envelope()` is retained as the
  `to_envelope(rank(...))` composition.
- `Cache` protocol and a thread-safe `TTLCache` default in a new
  `blue_prism_v7_mcp.cache` module. `BPClient` accepts an injected `cache=`
  (defaulting to a per-instance `TTLCache`), so a long-lived multi-threaded host
  can supply a shared/Redis-backed store.
- Public package exports for embedding: `Engine`, `Ranked`, `Cache`, `TTLCache`,
  `BPClient`, `MockBPClient`, `Scrubber`, `build_scrubber`, `BPConfig`.

### Changed
- The MCP tool layer (`build_tier1_tools` / `build_tier2_tools`) is now a thin
  adapter over an `Engine`: it keeps every tool's exact signature, docstring,
  and envelope output, delegating the logic to the engine and applying
  `to_envelope`. `TTLCache` moved from `client.py` to `cache.py` and is now
  lock-guarded.
- `list_queues`'s `deferred` fold-in now enriches the full ranked result set
  rather than only the returned page — a consequence of moving the enrichment
  into the domain (an embedder consuming the records gets `deferred` on every
  queue). The MCP `limit` remains representation-only; the items an LLM client
  sees are unchanged.

## [0.5.0] — 2026-06-15
Context & topology — four read-only primitives that explain how the estate is
wired: which process drains a queue, how workers are pooled, what shared
configuration processes depend on, and how the process catalogue is organised.
Each endpoint was spec-pinned field-by-field against the 7.5.1, 7.4.0, and 7.2.0
APIs before any code.

### Added
- `list_queue_configurations` (Tier 1) — `GET /workqueues/configurations`, the
  active queues' process→queue map: each active queue's assigned process and
  resource group plus its live activity (active sessions, available resources,
  time/ETA to clear). Needs Blue Prism 7.4+; on an older estate (or a denied
  read) it returns an empty envelope with a `meta.unavailable` note rather than
  failing the read surface.
- `list_resource_pools` (Tier 1) — `GET /resources/pools`, the resource pools
  (groupings of digital workers), their member counts and database status.
- `list_environment_variables` (Tier 1) — `GET /environmentvariables`, the
  shared process-configuration variables. The `value` is scrubbed type-aware on
  the variable's Blue Prism data type — the same fail-closed policy as the
  queue-item payload: a Password variable is redacted, free text is scrubbed,
  binary/image is dropped, and numbers/flags/dates pass through.
- `list_process_groups` (Tier 1) — `GET /processgroups/root/descendants`, the
  process tree (folders and published processes) so an agent can see how the
  catalogue is organised.

### Notes
- `list_queue_configurations` is a separate tool rather than a fold into
  `list_queues`: it covers only active queues and carries live stats, a
  different population and shape. The opinionated queue→process *name* mapping
  stays a consumer concern; the tool exposes the generic id-based primitive.
- The `environmentvariables` `value` is typed `object` with no inner shape in
  the spec, so its exact form is a day-one live-verification item; the
  type-aware scrub keys on the sibling `dataType` and holds either way.

## [0.4.0] — 2026-06-15
Estate insight — surface the licence entitlement picture and the one queue
count the summary row omits. The dashboard aggregates were spec-pinned
field-by-field against the 7.5.1 API; only the genuinely net-new data entered
the reusable surface.

### Added
- `license_entitlement` (Tier 2 insight) — `GET /dashboards/licensesEntitlement`,
  the entitlement side that complements `estate_health`'s limits-vs-usage:
  `active_license_types` plus per-tier ceilings split `enterprise`/`desktop`
  (published processes, concurrent sessions, runtime resources, process-alert
  machines). Needs the `System - License` permission; degrades to an
  `unavailable` note if the read is denied.

### Changed
- `list_queues` rows now include the per-queue `deferred` count, folded in from
  `GET /dashboards/workQueueCompositions` (the one state count `WorkQueueSummary`
  does not carry). The aggregate is fetched only for the queues being returned,
  and `deferred` is omitted rather than failing the listing if the read is denied.

### Notes
- `resourceUtilization` / `resourcesSummaryUtilization` were assessed and left
  out **as raw reads** — their shape is a chart feed (a 24-hour heat-map row per
  worker per day; an unlabelled aggregate time-series), not the point facts an
  LLM tool wants. The useful form, a derived per-worker utilisation tool, is a
  real value-add for any consumer and is deferred to its own release (it needs
  page-number paging support — the API's only such read — and an aggregation
  design).

## [0.3.0] — 2026-06-14
Incident response & diagnostics — drill from the list surface into the one
failing thing, and stop a schedule running in error.

### Added
- `stop_schedule` (Tier 3 control) — `DELETE /schedules/{id}/runs/active`,
  `trigger_schedule`'s incident sibling, sharing its `Edit Schedule` permission.
- `get_queue_item` (Tier 1) — `GET /workqueues/items/{id}`, the only read that
  returns the item `data` payload. The nested Blue Prism `DataCollection` is
  scrubbed **fail-closed** by value type: free text (and any unknown or miscased
  type) through the PII scrubber, passwords redacted, binary/image dropped,
  scalars kept, nested collections recursed, and `additionalParameters` scrubbed.
- `list_item_attempts` (Tier 1) — `GET /workqueues/{id}/items/{itemId}/attempts`,
  an item's attempt history.
- `get_session` (Tier 1) — `GET /sessions/{id}`, single-session detail.

## [0.2.0] — 2026-06-14
Process-control completion.

### Added
- `start_process` accepts optional typed start-up `parameters`
  (`POST /sessions` → `PUT .../parameters` → `PATCH` to `Running`); the audit
  records parameter names and types only, never values.
- `stop_session` (Tier 3 control) — `PATCH /sessions/{id}` `{status: Stopped}`,
  `start_process`'s control sibling, sharing its permissions.

## [0.1.1] — 2026-06-14
### Fixed
- The OAuth2 token request now carries the `bp-api bpserver` scope pair the v7
  API requires on every endpoint; v0.1.0 requested only `bp-api`, so as released
  it could not reach a live estate.

## [0.1.0] — 2026-06-13
First runnable release — the foundation, built in eight phases (see
[DESIGN.md](DESIGN.md)).

### Added
- `BPClient` — the v7 REST client (OAuth2 client-credentials, token paging,
  deepObject filters, per-instance TTL cache), with a drop-in offline
  `MockBPClient`.
- Pluggable PII scrubbing — a `Scrubber` protocol with null, zero-dependency
  regex (UK FS patterns), and Presidio backends; fail-loud backend selection.
- Tier 1 visibility and Tier 2 insight tools over the v7 entities, returning a
  relevance-sorted, honestly-truncated envelope.
- Tier 3 control surface, shipped disabled behind `enable_actions`, with
  capability gating (`GET /user/permissions`), an append-only audit log, and a
  dry-run default on every action.
- FastMCP stdio server, a first-class mock run mode, console entrypoint, and
  deployment / day-one verification docs.

[Unreleased]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.15.0...HEAD
[0.15.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.14.0...v0.15.0
[0.14.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.13.0...v0.14.0
[0.13.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.11.1...v0.12.0
[0.11.1]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.9.0...v0.10.0
[0.9.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.8.0...v0.9.0
[0.8.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.7.0...v0.8.0
[0.7.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/releases/tag/v0.1.0

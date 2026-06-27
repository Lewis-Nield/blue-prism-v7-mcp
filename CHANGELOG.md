# Changelog

All notable changes to **blue-prism-mcp** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, every
additive endpoint is a minor bump.

## [Unreleased]

### Added
- `BP_DATA_SOURCE=demo` (and `demo_estate()`): a populated offline estate built
  on the same `MockBPClient` — pooled workers across departments (working/idle/
  offline), queues in varied health (an SLA-breaching backlog, a degrading one, a
  paused one, plus a healthy bulk), in-flight and silently-stale `Running`
  sessions, and a failed schedule. Lets the server (and a downstream console) be
  evaluated end-to-end against a lively estate, while the lean default fixtures
  stay the minimal substrate the unit tests assert against. Behind the foreground
  sessions the estate now also carries a deterministic ~180-day backlog of
  finished runs — weekdays busier than weekends, a termination fraction that
  worsens over the most recent fortnight — so a downstream throughput history and
  STP-rate trend have real, varied shape rather than a flat recent week.

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
- `Engine` — a first-class facade over the read surface (`blue_prism_mcp.Engine`)
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
  `blue_prism_mcp.cache` module. `BPClient` accepts an injected `cache=`
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

[Unreleased]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.7.0...HEAD
[0.7.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.6.0...v0.7.0
[0.6.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/releases/tag/v0.1.0

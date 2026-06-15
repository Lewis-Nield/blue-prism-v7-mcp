# Changelog

All notable changes to **blue-prism-mcp** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, every
additive endpoint is a minor bump.

## [Unreleased]

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

[Unreleased]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/releases/tag/v0.1.0

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
- **Tool surface designed as estate primitives**, not a copy of the dashboard's
  MI-oriented tools. Read-only management information is what a dashboard does;
  the differentiated value of an MCP server is a **governed action surface** on
  v7 Enterprise.
- **Pluggable PII scrubbing.** A `Scrubber` protocol with a no-op default; a
  Presidio-backed implementation behind the optional `[pii]` extra so the base
  install stays light and teams can supply their own redaction.
- **Insight views are separate tools**, not parameters on the primitives — tight,
  single-purpose tool descriptions drive better model tool-selection.
- **Session-log read is in v1.** `get_session_log` is the highest-value agentic
  read ("why did this run fail?"). It is a second PII vector beyond exception
  messages (stage data can carry item payloads) and therefore routes through the
  scrubber.
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
- `retry_queue_item` / `defer_queue_item` / `mark_exception_resolved`
- `start_process`
- `set_schedule_enabled` / `trigger_schedule`

**Shared contract.** List tools return an envelope —
`{"items": [...], "meta": {total, returned, truncated, sorted_by}}` — with
server-side relevance sort and a `limit`, so a large estate can never blow the
client's context and the model always knows how much it did not see. Dated
parameters are ISO-validated and fail loudly. Per-message PII scrubbing is
cached.

---

## Architecture (layers, bottom-up)
1. **`BPClient`** (`client.py`) — the v7 REST client as a stateful object: the
   bearer token, a per-instance TTL cache, and the injected config all live on
   the instance, so two estates / two server instances never collide.
2. **Config** (`config.py`) — a per-deployment object built from the environment
   (credentials, base URL, pagination, SSL verification, feature flags including
   `enable_actions`).
3. **PII** (`pii.py`) — the `Scrubber` protocol, the no-op default, and the
   optional Presidio backend. Applied at the exception-message and session-log
   boundaries.
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
  session stage-log; stub the Tier 3 write endpoints.
- **Phase 3 — Pluggable PII**: the `Scrubber` protocol and Presidio backend,
  wired at the exception-message and session-log boundaries.
- **Phase 4 — Tier 1 + 2 tools**: visibility primitives and the three insight
  tools, with the envelope, ISO validation, and cached scrub; tool tests.
- **Phase 5 — Governance scaffold + Tier 3 (disabled)**: the `enable_actions`
  flag, capability resolver, audit log, and dry-run; action tools registered
  only when enabled.
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

# Changelog

All notable changes to **blue-prism-v7-mcp** are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Pre-1.0, every
additive endpoint is a minor bump.

## [Unreleased]

Demo-estate coherence: the bundled demo estate now behaves like an estate rather
than a set of numbers that happen to sit next to each other. Every figure it
reports traces to something in the fixture, and every governed write visibly
moves the figures downstream of it.

The trigger was an investigation that chased "the worker statuses don't make
sense" through a consuming console's scoring rules and found the rules correct in
every case — the fixture was contradicting itself, and the console was faithfully
reporting the contradiction. A mock whose numbers cannot move is not a cheaper
estate; it is a different thing wearing the same interface, and it silently
invalidates every act-then-observe loop built against it.

**This release touches `mock.py` only.** The live-estate code path is byte-for-byte
unchanged, so an upgrade cannot alter what the server does against a real Blue
Prism instance. It is a minor rather than a patch bump because the demo estate is
a published part of the artifact: almost every number it returns is different, and
`MockBPClient` gains a constructor keyword.

### Added
- **`MockBPClient(drain_tick=...)`** — how long one queue item takes to work while
  a session drains its queue, defaulting to 5 seconds. Draining the 64-item
  Payments backlog at that queue's declared 1:48 average work time is about two
  hours of wall clock: true to life, useless to demonstrate. The tick compresses
  *when* things happen while leaving *what* happens honest, and it is a parameter
  rather than a constant so tests can pin it.
- **~3,200 real queue items behind the demo queues.** The summaries declared
  thousands while six items existed in total, so any drill-in returned almost
  nothing at any lookback — the window was never the cause. A deterministic
  generator (a pure function of an index, no RNG, so reads reproduce run to run)
  tops up to each queue's declared counts, and those counts are now derived from
  the items rather than hand-written. The six original hand-written items stay as
  the foreground, because they carry the deliberate narrative: the SLA-breached
  PAY-5001, the exception rows, and the item linked to a terminated session.
  `Deferred` items are generated to match the deferred map, which was another
  free-standing number.
- **A utilization row for every worker, every day.** Only three of the eight had
  one. A worker that reports no data is a case the roll-up must handle, and it
  still is — but in a demo an absent worker reads as broken, so the two offline
  bots now get real rows that are simply zero-filled.

### Changed
- **A worker's status now agrees with its sessions.** BOT-F01 read `Working` with
  `activeSessionCount: 1` while every one of its sessions was finished, so a
  consumer joining the two correctly reported a worker working on nothing. The
  invariant is written into `demo_estate()`'s docstring and pinned by tests, so it
  cannot drift back silently: every busy worker holds a matching in-flight
  session, `activeSessionCount` equals that count, and any run meant to read stale
  says so. BOT-F02's five-day stuck run is the one deliberate exception and stays.
- **A session drains its queue instead of waiting out a timer.** Starting a
  queue-backed process locks that queue's oldest pending item; each tick completes
  it and locks the next; the session ends by itself when nothing is left pending.
  This is what makes a "stalled queue" signal — a running queue with a backlog and
  nothing locked — able to clear, which previously no operator action could
  achieve. Stopping mid-drain returns the held item to `Pending` with the counts
  restored. Queue-less sessions keep the old timer behaviour untouched.
- **Licence usage is derived from the estate it describes.** `concurrentSessionsUsed`
  and `runtimeResourcesUsed` were hand-picked literals; the latter had already
  drifted, claiming 5 against 6 non-offline workers.
- **Utilization responds to work done.** Completed sessions and drained items
  contribute worked minutes to the heat map, so the duty cycle moves when the
  estate does instead of standing as a static seed.
- **Triggering a schedule starts its tasks' sessions**, occupying their workers
  and locking their queue items exactly as a manual start does — so the whole
  chain, from trigger through worker and queue to the duty cycle, is observable
  from one governed action. Deliberately no `onSuccessTaskId` chaining,
  `delayAfterEnd` or `failFastOnError`: that is scheduler semantics well beyond
  what a fixture needs, and it would pull product opinion into a generic layer.
- **A future `start_time` on `trigger_schedule` now defers the run** rather than
  starting it immediately. The tool documents the parameter as running the
  schedule once *at* that time; the sessions started the instant the call was
  made. The pending start is held and fired once the clock reaches it, so the
  path stays coherent instead of going inert.
- **`stop_schedule` stops what its trigger started.** It terminated the log row
  and left the sessions running, their workers occupied, their items locked and
  their licences consumed. It now stops exactly the sessions that run started,
  leaving pre-existing in-flight runs on the same workers untouched, and cancels a
  deferred trigger that has not fired.
- **Dependabot no longer opens a PR for every setuptools release.**
  `build-system.requires` is a floor, not a pin — raising it only narrows who can
  build from the sdist, and when a setuptools advisory landed it raised the floor
  rather than the version actually installed. The alert still shows in the
  security tab and the fix still belongs in `uv.lock`, which is where the resolved
  version lives.

### Fixed
- **Reads that skipped settling returned order-dependent nonsense.** Only
  `get_queues()` settled, which was harmless while item states were
  time-invariant and incoherent the moment they were not: an items-first read
  showed one set of counts and the very next queue read showed another. Every
  queue read settles now, as does `get_resource_utilization`.
- **Writes that skipped settling rewound the estate.** `stop_session` mutated
  without settling, so a stop with no intervening read discarded every tick the
  session had worked and handed its item back to `Pending` — start, advance two
  hours, stop, and the queue was exactly as it had been before starting.
- **A drain paced itself to the reader rather than to the tick.** Each pass
  re-stamped the next lock at the read time, so a polling cadence that was not a
  multiple of the tick silently shed work, and a drained batch collapsed onto a
  single instant. Items are now stamped at their own tick boundary and the next
  lock inherits that stamp, so the remainder carries and completions read as a run
  spaced one tick apart.
- **The utilization seed was shared between clients.** Its `usages` list was
  shallow-copied, so every `MockBPClient` built from the default held the same
  list object — invisible until contributions began mutating it in place, at which
  point one client's worked minutes would have leaked into every other client and
  test built from the same untouched default.
- **`get_resource_utilization` handed callers its live lists.** The same aliasing
  one boundary further out: rows were shallow-copied, so an already-returned
  snapshot silently changed under whoever held it as later work contributed
  minutes, and a caller writing into its own copy wrote straight back into the
  fixture.
- **A long session's utilization was truncated into a single hour.** A whole run's
  elapsed time went into one bucket, where the 60-minute clamp discarded the rest:
  a three-hour run read as thirty minutes, and how much survived depended on when
  the caller happened to read. Elapsed time is now spread across the hours it
  actually spans, day rollover included.

## [0.19.0] — 2026-07-26

### Added
- **A Verification status section in the README, and `docs/VERIFICATION.md`.**
  Nothing in the repository said that this has never been run against a live
  estate — true, material, and something a reader would otherwise reasonably
  assume the other way from "built against the 7.5.1 specification". The README
  now states it plainly and grades it by tier: a wrong read assumption surfaces
  as an error, while the control surface writes to a real estate, which is why
  it ships disabled behind a capability gate, an audit, and a dry-run default.
  `docs/VERIFICATION.md` is the fill-in report that closes the gap — 20 items
  compiled from every "needs day-one verification" note accumulated since the
  design, each with the exact call that settles it, ordered reads-first so the
  half that touches nothing can be run on its own. It records the graduation
  rule too: v1.0.0 ships when every item is confirmed or has a shipped
  workaround. Linked from DEPLOYMENT.md, SECURITY.md and DESIGN.md.
- **`scripts/release.py`** — the release tail (version bump → CHANGELOG roll →
  tag → GitHub release) as a stdlib-only script with `prepare` and `publish`
  subcommands, both supporting `--dry-run`. The tail is five files that have to
  agree on one number plus a tag that has to point at the right commit; each is
  individually valid and only ever wrong *relative to the others*, so CI cannot
  see the drift. It has bitten twice — a CHANGELOG section that never landed
  before 0.8.0, and a tag left pointing at a pre-merge commit at 0.14.0.
  `prepare` refuses on a dirty tree, a version that does not advance, or an
  empty `[Unreleased]`, and re-reads every site afterwards rather than trusting
  its own writes. `publish` reads the version from `HEAD` rather than the
  working tree, since reading the working tree would pass on precisely the
  mistake it exists to catch. `uv.lock` is hand-edited rather than regenerated,
  because a version bump should touch one line and `uv lock` re-resolves the
  whole graph.
- **Trademark notice** in `README.md` (near the top and again under Licence) and
  in `NOTICE`. "Blue Prism" is an SS&C mark and it sits in the package name, the
  repository name, and the description, while Apache-2.0 §6 grants no trademark
  rights — so the project has to say plainly, where a reader lands, that it is
  independent, unofficial, and uses the name only to describe what it
  interoperates with.
- **Package-index metadata**: `[project.urls]` (homepage, repository,
  documentation, changelog, issues) and `classifiers`, neither of which existed,
  leaving a bare index page with no search facets and no sidebar. The Python
  classifiers are exactly the CI matrix, so a claimed version is a tested one,
  and there is deliberately no `License ::` classifier — PEP 639
  `license`/`license-files` are already in use and setuptools>=77 rejects the
  pair. `keywords` gains `automation`.
- **`.github/dependabot.yml`** — weekly `pip` and `github-actions` updates. The
  runtime deps are ranges, so this mostly watches the exact dev pins and the
  action pins; the latter is what makes pinning actions by SHA sustainable,
  since a pinned SHA never updates itself.
- **`.github/workflows/release.yml`** — publishes to PyPI from a pushed `v*` tag
  using Trusted Publishing (OIDC), so there is no long-lived API token stored as
  a repository secret. `scripts/release.py` owns the repository side of the tail
  and this workflow owns the index side; the tag that script pushes is what
  triggers it. The build job fails unless the tag matches the version read back
  out of the **built wheel's metadata** rather than out of `pyproject.toml` —
  the build is what gets uploaded, so the build is what has to agree with the
  tag, and a source-file check would pass on exactly the mismatch this exists to
  catch. A `workflow_dispatch` trigger selects the index so the first real
  publish is never the first run; because the environment name forms part of the
  OIDC claim, TestPyPI requires its own `testpypi` environment rather than
  reusing `pypi`.
- **A `lockfile` CI job** running `uv lock --check`. The test job installs with
  pip and never reads `uv.lock`, so a fully green matrix said nothing about
  whether the lock still matched `pyproject.toml` — and Dependabot only edits
  manifests, so every dev-pin bump left the lock a little further behind. It is
  its own job rather than a fourth step on each matrix leg because the lockfile
  is not interpreter-specific. `uv` is deliberately unpinned there: a newer `uv`
  re-resolving and failing the check is worth knowing about, and unlike the
  formatting gate a failure blocks nothing that a single `uv lock` cannot clear.
- **A non-blocking `pip-audit` CI step**, running against the versions actually
  resolved on each matrix leg. Dependabot reads manifests; nothing else in the
  pipeline looked at what a fresh install would really pull. Non-blocking on
  purpose — an advisory against a transitive dependency is information, not a
  reason a release cannot be cut.

### Changed
- **The audit log is created owner-only (0600)**, and the mode is re-applied on
  every startup. It was created with `touch()`, i.e. 0666 masked by the umask —
  typically world-readable, on a file carrying queue, process, session and
  resource names, actor identity, and the item metadata SECURITY.md itself calls
  audit-visible free text. The chmod is unconditional rather than create-only
  because `touch(mode=...)` applies its mode only when it creates the file,
  which would have left every already-deployed log exactly as it found it.
  SECURITY.md records the override path for a deployer who wants it wider.
- **CI declares `permissions: contents: read`** and pins its actions by commit
  SHA rather than by mutable tag (`actions/checkout` v7.0.1,
  `actions/setup-python` v7.0.0). Both matter the moment the repository is
  public and a fork's pull request can trigger the workflow.
- **README is now the package landing page**, and is written as one. The status
  line said `v0.16.0` two releases after the fact; it now reads `v0.18.0`
  (`scripts/release.py` owns that line from here). All seven relative document
  links are absolute `https://github.com/...` URLs, because a package index does
  not rewrite relative links against the repository — every one of them would
  have rendered as a 404 for anyone reading the description away from GitHub.
- **`uv.lock` re-resolved against the current dev pins.** Five Dependabot bumps
  had landed without it, so `uv lock --check` failed outright: the lock still
  resolved pytest 8.3.5 and pytest-cov 6.1.1 against a manifest pinning 9.1.1
  and 7.1.0, and carried no entry at all for mypy or types-requests. Anyone
  following the documented `uv sync --all-extras --frozen` was getting a dev
  toolchain that no longer matched. No runtime dependency moved. One practical
  consequence for contributors: mypy now resolves through the lock and installs
  as `.venv/bin/mypy`, so running the type gate no longer needs a throwaway
  environment.
- The CI coverage gate now measures `scripts/` alongside the package. A bug in
  the release automation lands a wrong version or a misplaced tag, which is the
  class of mistake it was written to prevent, so it is held to the same 100%
  bar as the shipped code.

## [0.18.0] — 2026-07-22

Transport governance: the client gains a ceiling on the load it puts on the
Blue Prism application server, and a way to report the load it actually emitted.
That host also serves interactive Control Room clients, the scheduler, and the
runtime resources' own connections, so a tool that degrades the estate it
monitors is a worse outcome than a tool that is simply down.

**Every setting below defaults to off.** An unconfigured server emits exactly
what it did in 0.17.0 — a distributable artifact should not change its posture
on upgrade, and the right numbers depend on a deployment's concurrent users,
poll cadence, and application-server headroom, none of which the engine can see.
The engine ships the mechanism; the deployment owns the policy.

### Added
- **New `transport.py`**: a `RateLimiter` protocol with a thread-safe, FIFO
  `TokenBucket` default (`BP_API_MAX_REQUESTS_PER_SECOND`, `BP_API_MAX_BURST`),
  a `RetryPolicy`, and `RequestCounters`. FIFO is deliberate — a plain lock lets
  a busy caller starve one that has been waiting, which under contention is the
  request most likely to be a person waiting on a page. The limiter is
  injectable (`BPClient(config, limiter=...)`) on the `Cache` protocol's
  precedent, because a host running several processes against one estate needs a
  budget they *share*, which a per-instance bucket cannot express.
- **A concurrency ceiling** (`BP_API_MAX_CONCURRENCY`), enforced by a bounded
  semaphore. A token bucket bounds *rate* and says nothing about how many
  requests are in flight; a wide host thread pool fanning out is what actually
  melts an application server. The connection pool is mounted at no less than
  that ceiling (`BP_API_POOL_MAXSIZE`), since above `pool_maxsize` `requests`
  opens and discards connections — TLS re-handshakes against the estate at
  exactly its busiest moment.
- **One end-to-end wait budget** (`BP_API_LIMITER_TIMEOUT`) covering the
  concurrency slot and the rate token together, rather than each getting the
  full allowance and the configured number silently becoming two stacked
  timeouts. When it is spent the call raises `TransportBudgetExceeded` —
  deliberately not a `requests.RequestException`, since nothing was sent. Block,
  then fail visibly: never drop a request, never queue unboundedly.
- **Bounded retry of transient failures** (`BP_API_MAX_RETRIES`,
  `BP_API_RETRY_BASE_DELAY`): 429 honouring `Retry-After` in either RFC 7231
  form, and 502/503/504 with equal-jitter exponential backoff. Not 500 — as
  likely a bad request as a blip.
- **A ceiling on every retry wait** (`BP_API_RETRY_MAX_DELAY`, default 60s),
  whatever its source. The estate gets to ask us to wait; it does not get to
  decide how long we hang. This is what makes the absolute `Retry-After` date
  safe to honour — converting an instant into a wait means subtracting our clock
  from the estate's, and while ordinary NTP drift is seconds, an unsynced host
  or a gateway writing local time as GMT is hours out. The same ceiling binds a
  plain `Retry-After: 3600` (the identical unbounded wait, expressed as a
  number) and a doubling backoff window, so raising `max_retries` never requires
  recomputing the worst case by hand.
- **`BPClient.transport_stats()`** — requests sent, retries, bytes received,
  errors bucketed (`rate_limited`, `client_error`, `server_error`, `timeout`,
  `connection_error`, `limiter_exhausted`), and a per-endpoint tally with
  id-shaped path segments collapsed to `{id}` so it counts endpoints rather than
  entities. A published contract: stable keys, every error bucket always
  present, and a fresh copy per call. It exists so a load budget can be
  *measured* rather than asserted. Token fetches are counted separately — the
  Authentication Server is a different service from the API host, so token
  traffic must not inflate the estate's budget, but must not vanish from the
  report either.
- **Single-flight on cache miss.** Concurrent callers missing the same key now
  share one upstream read instead of each issuing it; a TTL expiry under load
  was previously a thundering herd. It lives in `BPClient._cached` rather than
  in `TTLCache` because `Cache` is a published protocol — putting it in the
  default implementation would mean an injected shared store silently *loses*
  it, and the herd is worse across processes, which is exactly when a host would
  inject one.
- **Single-flight on the token fetch**, by the same shape and for the same
  reason: concurrent callers arriving on an absent or expired token now cost the
  Authentication Server one POST between them rather than one each. The token
  fetch is the one send deliberately exempt from the limiter, the semaphore and
  the retry layer — a different host with a different budget — which made it the
  one path where a wide thread pool could still burst unbounded.

### Changed
- **A 401 now invalidates the token that attempt actually carried**, not
  whatever is cached by the time the 401 is handled. Under concurrency the 401s
  from a single expiry keep arriving after another caller has already refreshed,
  and clearing unconditionally discarded that fresh token and re-fetched — one
  wasted auth round-trip per in-flight request, an expiry storm feeding itself.
  Single-threaded behaviour is unchanged.
- A negative `BP_API_RETRY_MAX_DELAY` or `BP_API_RETRY_BASE_DELAY` now degrades
  to no pause rather than reaching `time.sleep` and raising: a mistyped knob
  should not surface as an unrelated `ValueError` out of a read.
- Reads opt into the retry layer and writes do not, via a new keyword-only
  `retriable` on the internal request path that defaults to **False**. A retried
  write is a duplicate estate mutation — a second `start_process` is a second
  live run — so that is now a property of the construction rather than a
  convention someone has to remember. The single 401 re-auth is orthogonal,
  unchanged, and not counted as a retry.

## [0.17.0] — 2026-07-22

### Added
- **`list_sessions` filters narrow server-side.** `process`, `resource`, and
  `status` now go to the API as query filters instead of being applied to a
  whole-window payload after it arrives, so a scoped question costs a scoped
  read. Verified against the 7.5.1 spec and unchanged on 7.1.0/7.2.0, the
  endpoint mixes two encodings in the one call: `status` is an array with
  `style=form explode=false` (comma-joined into a single param), while
  `processName`/`resourceName` are `BasicStringFilter` deepObject bounds sent
  as `[eq]`.
- **A whole SET of statuses is one request.** The domain facade
  (`Engine.list_sessions`) accepts `status` as a sequence as well as a single
  value, normalised to a sorted tuple for the cache key so the same set in a
  different order shares one entry. A caller wanting several in-flight
  statuses passes them together rather than looping — which would have cost a
  full window read each. The MCP tool keeps its single-value schema.
- `_get_collection` gains a keyword-only `page_size` override, spelled and
  placed to match the one `_get_paged_by_number` already carries.
  `get_queue_items` uses it to shrink the page to `max_records` — but **only**
  when a server-side `sort_by` is also present, since without a known ordering
  an early-stopped fetch is an arbitrary subset and a smaller page only makes
  it a smaller one. Same guard as the v0.15.0 fetch-time cap it pairs with.

### Changed
- Process and resource names are canonicalised against the (already cached)
  process and resource catalogues before being sent upstream. The v7 name
  filters are exact — `BasicStringFilter` offers `eq`/`gte`/`lte`/`strtw` and
  no `ctn` — whereas this tool surface has always matched names
  case-insensitively, so `process="invoice processing"` still finds
  `Invoice Processing`. Unlike name→id resolution, an unrecognised name is
  passed through unchanged and simply returns zero rows rather than raising.
- The local filters are retained as a defensive second pass: the gateway
  ignores an unrecognised query parameter rather than rejecting it, so a
  filter that silently failed to narrow upstream must not widen the answer.

## [0.16.0] — 2026-07-17

### Added
- **`throughput_summary` gains completed-run duration statistics.** Each
  per-process row now also carries `duration_runs`,
  `duration_p50_minutes`, `duration_p95_minutes`, and `duration_max_minutes`
  — nearest-rank percentiles computed from the same session read, over ONLY
  the `Completed` runs with a parseable, non-negative `startTime`/`endTime`
  span (Terminated/Stopped runs don't shape it). All four are `None` when no
  run qualifies. Mechanical aggregation only — no thresholds, same posture
  as `resource_utilization` — so a consumer can derive its own per-process
  staleness baseline instead of judging every process against one flat
  ceiling.
- The demo estate's historical backlog now gives each process its own
  typical completed-run length (`_DEMO_HISTORY_BASE_MINUTES` in `mock.py`)
  instead of one flat 11 minutes, with Payment Run as a genuinely long
  batch (~90min) — so the new duration statistics have real per-process
  shape, and the foreground in-flight sessions demonstrate both a
  long-running-but-healthy process and a short-baseline process truthfully
  stuck past its own typical duration.

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
- **`max_records` was a docstring-only contract, enforced nowhere.**
  `Engine.list_queue_items` now rejects `max_records` unless paired with
  `sort_by="loadedDate asc"` (without a server-side sort putting the wanted
  rows first, an early-stopped fetch is an arbitrary subset, not a top-N) and
  rejects non-positive values. Separately, `BPClient.get_queue_items` was
  capping only the *fetch* — the page that satisfies `max_records` could
  still overshoot it, so `max_records=1` could return up to a full page,
  diverging from `MockBPClient`'s exact truncation. It now slices its result
  down to exactly `max_records` after the capped fetch.

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

[Unreleased]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.19.0...HEAD
[0.19.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.18.0...v0.19.0
[0.18.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.17.0...v0.18.0
[0.17.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.16.0...v0.17.0
[0.16.0]: https://github.com/Lewis-Nield/blue-prism-v7-mcp/compare/v0.15.0...v0.16.0
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

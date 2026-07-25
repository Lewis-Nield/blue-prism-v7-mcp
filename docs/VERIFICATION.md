# Live verification report

This server was built against the official Blue Prism v7 OpenAPI specifications
(7.5.1, cross-checked on 7.2.0 and 7.1.0) and is exercised end-to-end against
in-memory clients. It has **not** been run against a live estate. This document
is the checklist that closes that gap: a fill-in report a person with access to
a Blue Prism environment can work through and hand back.

Copy this file, fill in the header and every **Observed** / **Verdict** line,
and return it (a pull request, an issue, or the file itself — whatever suits).
Partial reports are welcome: Part A alone is useful, and it is the part that
touches nothing.

**Graduation rule.** v1.0.0 ships when every item below is `CONFIRMED`, or has a
`DIFFERS` verdict with a shipped fix or a documented workaround. Until then the
project stays on 0.x, and the README says so.

---

## Before you start

**Use a development or test estate.** Not production, not on the first pass.
The read surface only reads, but the control surface issues real writes, and
three of its endpoint behaviours are inferred from an underdocumented
specification — which is precisely why this document exists.

Prepare, on that estate:

| What | Why |
|------|-----|
| A dedicated service account (OAuth2 client-credentials) | Never an interactive user's credentials. Permissions per the tables in [DEPLOYMENT.md](../DEPLOYMENT.md). |
| A scratch work queue with a few items, at least one Exceptioned | B1, B2 |
| A second queue that is application-server-encrypted, if the estate has one | A8, B3 |
| A harmless published process (a no-op utility), and one with declared input parameters | B4 |
| A disposable schedule, not attached to anything that matters | B6, B7, B8 |

Start with `BP_ENABLE_ACTIONS=false` and work Part A. Only enable actions once
the reads look right, and treat the first `dry_run=false` as a deliberate step.

**How to drive the calls.** Point an MCP client at the server as in
[DEPLOYMENT.md](../DEPLOYMENT.md) and ask for each tool by name — that is how it
will really be used, and it exercises the transport too. Reads can also be
driven straight from Python if that is quicker for you:

```python
from blue_prism_v7_mcp import Engine, BPClient, BPConfig, build_scrubber

config = BPConfig.from_env()
engine = Engine(BPClient(config), build_scrubber(config))
print(engine.list_queues().records)
```

**What "narrows server-side" means, and why several items check it.** An
unrecognised query parameter is *ignored* by the v7 API, not rejected. A filter
encoded wrongly therefore returns unfiltered rows and looks perfectly fine —
including against the mock client, which is why the test suite cannot catch it.
Every filter item below is checked the same way: run the filtered call, and
confirm the rows that came back are actually narrower than the unfiltered call.
Where you can see the estate's own API logs, checking the emitted query string
is the stronger evidence.

---

## Report header

| Field | Value |
|-------|-------|
| Tester | |
| Date | |
| Blue Prism version (e.g. 7.2.0) | |
| API base URL form (e.g. `https://<host>/api/v7`) | |
| Authentication Server reachable at | |
| Estate type (dev / test / other) | |
| `blue-prism-v7-mcp` version | |
| `BP_PII_BACKEND` | |
| MCP client used | |

## Summary

Fill the verdict column as you go: `CONFIRMED` / `DIFFERS` / `BLOCKED` / `SKIPPED`.

| Item | Subject | Verdict |
|------|---------|---------|
| A1 | Auth and startup handshake | |
| A2 | Permission strings vs the capability resolver | |
| A3 | Queue-item filters narrow server-side | |
| A4 | `workQueueIds` array encoding (deferred fold) | |
| A5 | Session filters narrow server-side | |
| A6 | Session-log stage-type and stage-time filters | |
| A7 | Environment-variable value shape | |
| A8 | Queue-item `data` on an encrypted queue | |
| A9 | Paging and `max_records` | |
| A10 | Transport counters and throttling behaviour | |
| B1 | `retry_queue_item` | |
| B2 | `defer_queue_item` JSON-Patch path | |
| B3 | `create_queue_items` | |
| B4 | `start_process` | |
| B5 | `stop_session` | |
| B6 | `set_schedule_enabled` retire body | |
| B7 | `trigger_schedule` | |
| B8 | `stop_schedule` | |
| C1 | Audit trail | |
| C2 | PII scrubbing on real text | |

---

# Part A — reads

Nothing in this part changes the estate.

### A1 — Auth and startup handshake

**Concern.** The token request sends both scopes (`bp-api bpserver`); the global
security requirement in the spec demands both, and an early release that sent
only `bp-api` could not reach a live estate at all. Startup then resolves the
account's permissions and registers tools accordingly.

**How.** Start the server in live mode against the estate and complete an MCP
`initialize` + `tools/list`. Read the startup line on stderr.

**Expected.** A token is obtained; the visibility, context and insight tools are
registered; with actions off, no control tool appears. Startup fails loudly
(rather than serving a degraded surface) on a bad base URL, bad credentials, or
an unloadable PII backend — worth provoking once deliberately.

**Observed:**

**Verdict:**

**Follow-up:**

### A2 — Permission strings vs the capability resolver

**Concern.** `GET /user/permissions` returns permission *names*, and no
published example of a real response exists. The capability resolver matches the
documented display names case-insensitively, so a live estate that spells them
differently would withhold tools the account can actually run (or, worse,
register ones it cannot).

**How.** With `BP_ENABLE_ACTIONS=true`, read the startup audit line's
registered/withheld split, and compare it against what the account can really do
in Hub / Interact. Paste the raw permission list if you can get it.

**Expected.** The split matches reality. Any mismatch is a spelling delta worth
recording exactly.

**Observed (raw permission names, verbatim):**

**Verdict:**

**Follow-up:**

### A3 — Queue-item filters narrow server-side

**Concern.** `list_queue_items` pushes its state, date-window, status and SLA
filters to the server using deepObject-encoded parameters inferred from the
spec. Silent-ignore applies (see above).

**How.**

```text
list_queue_items(queue="<scratch queue>", state="Completed",
                 start_date="<a narrow window>", end_date="<…>")
```

Then repeat with a wide window, and with `state="Exceptioned"`.

**Expected.** Row counts and contents differ between the narrow and wide calls,
and the state filter genuinely excludes other states.

**Observed:**

**Verdict:**

**Follow-up:**

### A4 — `workQueueIds` array encoding (deferred fold)

**Concern.** `list_queues` folds a deferred count in from the queue-compositions
aggregate, which takes an array of queue ids. The array is comma-joined into a
single parameter (form style, `explode=false`), not repeated. A wrong encoding
here degrades quietly: the listing still returns, and signals the gap via
`meta.deferred_unavailable`.

**How.** `list_queues()` on an estate with at least two queues, one holding
deferred items.

**Expected.** Deferred counts are present and correct for the queues that have
them, and `meta.deferred_unavailable` is absent.

**Observed:**

**Verdict:**

**Follow-up:**

### A5 — Session filters narrow server-side

**Concern.** `/sessions` mixes two encodings in one call: `status` is
comma-joined (form, `explode=false`), while `processName` and `resourceName` are
deepObject `[eq]` — exact match, with no substring operator available. The
date window uses `resourceStartTime[gte]/[lte]`. Because `[eq]` is exact, the
tool canonicalises a name against the catalogue first so a case-insensitive
request still works.

**How.**

```text
list_sessions(start_date="<…>", end_date="<…>")
list_sessions(start_date="<…>", end_date="<…>", status=["Running", "Warning"])
list_sessions(start_date="<…>", end_date="<…>", process="<a process, deliberately mis-cased>")
list_sessions(start_date="<…>", end_date="<…>", resource="<a resource>")
```

**Expected.** Each filtered call returns a strict subset of the unfiltered one;
the multi-status call returns both statuses from a single request; the mis-cased
process name still matches.

**Observed:**

**Verdict:**

**Follow-up:**

### A6 — Session-log stage-type and stage-time filters

**Concern.** `get_session_log(errors_only=True)` pushes a form-array of stage
types (`Exception,Recover,Resume`) — the same unannotated-array class as A4 —
and the optional window bounds each stage's execution time server-side.

**How.** Pick a session that failed. Run `get_session_log(session_id=…)`, then
the same call with `errors_only=True`, then with a narrow window.

**Expected.** `errors_only` returns a strict subset containing the failure
stages; the window narrows further.

**Observed:**

**Verdict:**

**Follow-up:**

### A7 — Environment-variable value shape

**Concern.** The spec types an environment variable's `value` as a nullable
`object` with no inner shape, so whether a live estate returns the raw typed
value or a `DataValue` wrapper is unknown. The scrub keys on the variable's
sibling `dataType` and works either way, but the shape should be confirmed —
and, more importantly, so should the outcome: nothing sensitive reaching the
model.

**How.** `list_environment_variables()` on an estate that has a Password-typed
variable and at least one free-text one.

**Expected.** Password values are redacted; free text is scrubbed; scalars pass
through. Record the raw shape of one non-sensitive value.

**Observed (shape of one non-sensitive value):**

**Verdict:**

**Follow-up:**

### A8 — Queue-item `data` on an encrypted queue

**Concern.** `get_queue_item` returns the item's `data` collection. On a queue
encrypted with an application-server key the API is expected to refuse (4xx);
on unencrypted or database-key queues it should return.

**How.** `get_queue_item(item_id=…)` against each queue type the estate runs.

**Expected.** A clear error on the application-server-encrypted queue (recorded
verbatim, including status code), a scrubbed payload on the others.

**Observed:**

**Verdict:**

**Follow-up:**

### A9 — Paging and `max_records`

**Concern.** The client defaults to token paging (`BP_API_PAGING_MODE=token`)
and follows continuation tokens until exhausted. `max_records` caps a queue-item
read at fetch time so a large queue cannot drag the whole set back.

**How.** Read a queue with more items than one page (`list_queue_items` without
`max_records`), then repeat with `max_records=25`.

**Expected.** The uncapped read spans pages and returns a complete, non-repeating
set; the capped read stops early and says so rather than silently truncating.

**Observed (approximate item count, page count if visible):**

**Verdict:**

**Follow-up:**

### A10 — Transport counters and throttling behaviour

**Concern.** Observational rather than pass/fail. `BPClient.transport_stats()`
reports requests, retries, bytes and bucketed errors; the retry path honours
`Retry-After` on 429 in either RFC 7231 form, capped by `BP_API_RETRY_MAX_DELAY`.
Whether a real estate ever emits 429 — and in which form — has never been seen.

**How.** After working through Part A, call `transport_stats()` and paste it.
Note any 429 or 5xx encountered and what the estate sent back.

**Observed:**

**Verdict:**

**Follow-up:**

---

# Part B — control actions

Requires `BP_ENABLE_ACTIONS=true` and an audit log path. **Run every item as a
dry run first**, read the echoed write, and only then repeat with
`dry_run=false` against the scratch objects.

### B1 — `retry_queue_item`

**How.** `retry_queue_item(queue="<scratch>", item_id="<an exceptioned item>")`
— dry run, then for real.

**Expected.** The dry run echoes the exact write. The real call creates a new
attempt and the item returns to Pending; a worker can pick it up again.

**Observed:**

**Verdict:**

**Follow-up:**

### B2 — `defer_queue_item` JSON-Patch path

**Concern.** This is one of the three genuinely underdocumented behaviours. The
call sends an RFC 6902 patch to
`PATCH /workqueues/{id}/items/{itemId}/attempts/{n}` with
`[{"op": "replace", "path": "/deferredDate", "value": …}]`, sent as
`application/json-patch+json`. The spec never enumerates the accepted paths;
`/deferredDate` mirrors the item schema's field name. A lowercase
`/deferreddate` is the plausible alternative (an existing worked example
elsewhere in the API docs uses lowercase paths).

**How.** Dry run first. Then for real against a scratch item, with a
`defer_until` a few minutes out.

**Expected.** The patch is accepted and the deferral is visible on the item.
If it is rejected, record the **status code and response body verbatim** — that
response is the answer to the whole question.

**Observed:**

**Verdict:**

**Follow-up:**

### B3 — `create_queue_items`

**Concern.** Three sub-questions. (i) The spec's example shows data rows as bare
`[{valueType, value}]` objects while the `DataRow` schema is a dict keyed by
field name; the tool trusts the schema, so a named-field row must round-trip.
(ii) `POST /items` and `POST /items/batch` take the identical body; the tool
uses plain `/items`, and only the batch variant documents an encryption caveat.
(iii) Creating against an *active* queue is documented to need extra
server-side permissions beyond queue management.

**How.** Dry run first (note that item `data` **values** are deliberately absent
from the echo and the audit — only field names and value types appear). Then:

1. Create one item with two named fields of different types; read it back with
   `get_queue_item` and compare.
2. Create two items in one call.
3. Attempt a create against the application-server-encrypted queue.
4. Attempt a create against an active queue with a queue-management-only
   account.

**Expected.** (1) Field names and values survive the round trip. (2) Both items
land, ids returned. (3) and (4) fail clearly — record status codes and bodies.

**Observed:**

**Verdict:**

**Follow-up:**

### B4 — `start_process`

**Concern.** Starting is a create-then-run flow: `POST /sessions`, then
`PATCH /sessions/{id}` to `Running`. That the PATCH actually *launches*
execution (rather than only recording a status) is inferred. With `parameters`,
a `PUT /sessions/{id}/parameters` is issued between the two, while the session
is Pending, and the values must land before the run begins.

**How.** Dry run. Then start the harmless process on a worker; then start the
parameterised one with values supplied.

**Expected.** The process actually runs; the parameterised run sees its inputs.
Note: a session can be created and started against a logged-out worker — the
*process* then fails at run time if it needs an interactive session, which is
not a failure of the start call. Record which case you saw.

**Observed:**

**Verdict:**

**Follow-up:**

### B5 — `stop_session`

**Concern.** Drives the same endpoint with `{status: "Stopped"}`. It is a
request: the stop takes effect when the process next yields.

**How.** Start something long-running, then `stop_session(session_id=…)`.

**Expected.** The request is accepted and the run winds down.

**Observed:**

**Verdict:**

**Follow-up:**

### B6 — `set_schedule_enabled` retire body

**Concern.** The sharpest of the three underdocumented behaviours, and a real
contradiction in the specification: `PUT /schedules/{id}` documents Edit /
Retire / Create-Schedule permission tiers for retire and unretire, yet its
published request schema is the full schedule definition with **no `isRetired`
field anywhere**, while `ScheduleSummary` (the response) carries one. No worked
example exists. The tool currently sends the minimal body `{"isRetired": true}`.

**How.** Against the disposable schedule, dry run, then:

1. `set_schedule_enabled(schedule=…, enabled=false)` as it stands (minimal
   body). Record the outcome exactly.
2. If it is rejected: retry by hand (curl or equivalent) with the **full
   schedule definition plus `isRetired`**, and then with the full definition
   **without** the flag, and record which the server accepts and what it does.
3. Unretire (`enabled=true`) and confirm the schedule runs again.

**Expected.** Unknown — this item exists to find out. The valuable output is the
exact accepted body and the response to each rejected attempt.

**Observed:**

**Verdict:**

**Follow-up:**

### B7 — `trigger_schedule`

**Concern.** An empty body means "run now"; a `startTime` schedules one extra
run without changing the timetable.

**How.** Dry run, then trigger the disposable schedule with no `start_time`, and
again with one a few minutes out.

**Expected.** The immediate trigger runs it now; the timed one runs once at the
given time; neither alters the schedule's own timetable.

**Observed:**

**Verdict:**

**Follow-up:**

### B8 — `stop_schedule`

**Concern.** Issues `DELETE /schedules/{id}/runs/active` — a request, like B5.

**How.** Trigger the disposable schedule so it is running, then
`stop_schedule(schedule=…)`.

**Expected.** The active run is cancelled.

**Observed:**

**Verdict:**

**Follow-up:**

---

# Part C — governance and handling

### C1 — Audit trail

**How.** After Part B, read the audit file at `BP_AUDIT_LOG_PATH` and check its
permissions (`ls -l`).

**Expected.** One JSON line per event (startup, dry_run, attempt, success,
error); ids, names and dates present; **no** item `data` values and no exception
message text anywhere; the file is `0600`.

**Observed:**

**Verdict:**

**Follow-up:**

### C2 — PII scrubbing on real text

**Concern.** The scrub boundary is exercised heavily in tests against synthetic
text. Real estate exception messages and session logs are the interesting input.

**How.** With `BP_PII_BACKEND` set to whatever you would deploy (`regex` or
`presidio`), read exception summaries and a session log carrying real free text.

**Expected.** Personal data is redacted with entity types visible, and the text
is still useful. Record anything that leaked, and anything over-scrubbed to the
point of uselessness — both are findings.

**Observed:**

**Verdict:**

**Follow-up:**

---

## Anything else

Free text: surprises, papercuts, error messages that did not explain
themselves, anything that made the tool harder to use against a real estate than
it should be. That section is often the most valuable part of the report.

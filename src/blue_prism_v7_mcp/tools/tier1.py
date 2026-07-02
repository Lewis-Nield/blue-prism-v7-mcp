"""Tier 1 — visibility tools: read-only primitives over the v7 entities.

The domain logic lives on ``_Tier1ReadsMixin`` (composed into the embeddable
``Engine``): each method resolves names, scrubs, and returns the FULL ranked
records (a ``Ranked`` for list tools, a dict for single reads) — no truncation.
``build_tier1_tools`` is the thin MCP adapter: each closure keeps the public
signature and the docstring an LLM client sees, calls the engine method, and —
for list tools — caps it to top-N via ``to_envelope``.

PII boundaries here: ``exceptionReason`` on queue-item rows (lists and attempt
history), ``exceptionMessage`` on session rows (lists and single-session
detail), stage ``result`` text in session logs, and — only in the single-item
read — the item's ``data`` DataCollection, scrubbed type-aware (free text
through the scrubber, passwords redacted, binary/image dropped, collections
recursed).
"""

from __future__ import annotations

from typing import Any, Callable

import requests

from .common import (
    DEFAULT_LIMIT,
    Ranked,
    rank,
    require_window,
    resolve_id,
    resource_urgency,
    to_envelope,
    validate_choice,
    validate_optional_window,
    validate_uuid,
)

# Queue items have no unscoped listing to resolve a name against, so the id
# tools take the item's UUID directly — the same hint the Tier 3 item tools give.
_ITEM_ID_HINT = "Use list_queue_items to find the item's id (not its key value)."

# The WorkQueueItemNoData lifecycle enum, minus its "None" placeholder (a
# filter on None is meaningless). The API's `state` is what an operator calls
# an item's status; the API's `status` field is free user-supplied text.
ITEM_STATES = frozenset({"Pending", "Locked", "Deferred", "Completed", "Exceptioned"})

# SessionStatus, verified against the 7.5.1 spec.
SESSION_STATUSES = frozenset(
    {"Pending", "Running", "Terminated", "Stopped", "Completed", "Stopping", "Warning"}
)

# DataValue value-types (casefolded) whose value is a non-text scalar — a
# number, boolean, or an ISO date/time string. The item-data scrubber keeps
# these untouched; every other type (including unknown ones) has its string
# content scrubbed. Date/time stay here so an NER backend can't turn a real
# date into a redaction marker.
_SCALAR_VALUE_TYPES = frozenset({"number", "flag", "date", "datetime", "time", "timespan"})


# The ScheduleLogSummary fields kept when folding a schedule's last run into its
# row — the outcome and timing, not the estate plumbing (serverName, the log id).
_LAST_RUN_FIELDS = ("status", "startTime", "endTime", "duration")


def _last_runs(client, schedule_ids: list) -> dict | None:
    """Map schedule id → its last-run summary via the schedule-log endpoint.

    Returns None when the schedule-log read *fails* (denied/timeout) so the
    caller can signal that distinctly rather than silently dropping the field —
    the same degrade contract as _deferred_counts. On success returns
    {id: {status, startTime, endTime, duration}} built only for schedules that
    have actually run (a schedule with no runs answers None and is omitted, so
    the caller never fabricates an outcome). One bounded call per schedule
    (latest row only); schedules are a small set, and the reads are cached.
    """
    runs: dict = {}
    for sid in schedule_ids:
        if sid is None:
            continue
        try:
            run = client.get_last_schedule_run(sid)
        except requests.RequestException:
            return None
        if isinstance(run, dict):
            runs[sid] = {k: run.get(k) for k in _LAST_RUN_FIELDS}
    return runs


def _deferred_counts(client, queue_ids: list) -> dict | None:
    """Map queue id → deferred item count via the workQueueCompositions aggregate.

    Returns None when the aggregate read *fails* (denied/timeout), so the caller
    can signal that distinctly rather than silently dropping the field. On
    success returns {id: deferred} built only from well-formed rows carrying a
    non-null count: an id absent from the map is genuinely unknown (a malformed
    or short/paged response, or a null count) and the caller omits the field
    rather than fabricating a zero. The row filter also makes the function
    robust to a non-list body (a 204 already coerces to [] in the client; a
    proxy that reshapes the array into a dict/string yields no usable rows here
    instead of crashing the listing).
    """
    ids = [qid for qid in queue_ids if qid]
    if not ids:
        return {}
    try:
        compositions = client.get_queue_compositions(ids)
    except requests.RequestException:
        return None
    if not isinstance(compositions, list):
        return {}  # a gateway/proxy reshaped the array — treat as no usable data
    return {
        row["id"]: row["deferred"]
        for row in compositions
        if isinstance(row, dict) and row.get("id") and row.get("deferred") is not None
    }


class _Tier1ReadsMixin:
    """Domain methods for the visibility reads (mixed into ``Engine``).

    Expects ``self.client`` (live or mock) and ``self.scrub_text`` (the cached,
    None-safe text scrub) from the composing class. List methods return the full
    ``Ranked`` records; single reads return the record dict.
    """

    client: Any
    scrub_text: Callable[[str | None], str | None]

    def _scrubbed_item(self, item: dict) -> dict:
        return {**item, "exceptionReason": self.scrub_text(item.get("exceptionReason"))}

    def _scrubbed_session(self, session: dict) -> dict:
        return {**session, "exceptionMessage": self.scrub_text(session.get("exceptionMessage"))}

    def _scrubbed_stage(self, entry: dict) -> dict:
        return {**entry, "result": self.scrub_text(entry.get("result"))}

    def _scrubbed_collection(self, collection):
        """Scrub a Blue Prism DataCollection in place-by-value (recurses rows)."""
        if not isinstance(collection, dict) or not isinstance(collection.get("rows"), list):
            return collection
        rows = [
            {
                field: self._scrubbed_value(cell) if isinstance(cell, dict) else cell
                for field, cell in row.items()
            }
            if isinstance(row, dict)
            else row
            for row in collection["rows"]
        ]
        return {**collection, "rows": rows}

    def _scrub_payload(self, value):
        """Scrub any DataValue payload shape, recursing nested collections.

        Strings go through the PII scrubber; lists (e.g. a RadioButtonsArray)
        scrub element-wise; a dict is a nested DataCollection and recurses;
        anything else (a number, boolean, or None) is already safe.
        """
        if isinstance(value, str):
            return self.scrub_text(value)
        if isinstance(value, list):
            return [self._scrub_payload(v) for v in value]
        if isinstance(value, dict):
            return self._scrubbed_collection(value)
        return value

    def _scrub_typed_value(self, value_type, value):
        """Apply the FAIL-CLOSED, type-aware scrub policy to one typed value.

        A diagnostic read must never leak a secret or a binary blob, so the
        policy errs toward scrubbing: Password is redacted wholesale;
        Binary/Image drop the base64 payload (keeping a marker); the scalar
        types (Number/Flag/Date/DateTime/Time/TimeSpan) keep their value —
        scrubbing them is wasted work and, under an NER backend, would mangle
        a legitimate date into a redaction marker. EVERY other type — Text,
        RadioButtons, Collection, AND any unknown or miscased type — has its
        string content scrubbed and nested collections recursed, so a value
        type the server spells differently fails closed rather than passing
        through verbatim. Matching is case-insensitive, like the start-up
        parameter validator. Shared by the queue-item DataValue scrub and the
        environment-variable value scrub (same Blue Prism type vocabulary).
        """
        vt = str(value_type or "").strip().casefold()
        if vt == "password":
            return "[PASSWORD]"
        if vt in ("binary", "image"):
            return f"[{vt.upper()} omitted]"
        if vt in _SCALAR_VALUE_TYPES:
            return value
        return self._scrub_payload(value)

    def _scrubbed_value(self, cell: dict) -> dict:
        """Scrub one DataValue cell, keyed on its ``valueType``.

        additionalParameters (a Binary's file path, etc.) is free text too, so
        it is scrubbed at the same boundary.
        """
        new = dict(cell)
        new["value"] = self._scrub_typed_value(cell.get("valueType"), cell.get("value"))
        extra = cell.get("additionalParameters")
        if isinstance(extra, list):
            new["additionalParameters"] = [
                self.scrub_text(x) if isinstance(x, str) else x for x in extra
            ]
        return new

    def _scrubbed_env_var(self, var: dict) -> dict:
        """Scrub one environment variable type-aware, keyed on dataType.

        The value is a configuration payload carrying the same Blue Prism data
        types as a queue-item DataValue, so it runs through the identical
        fail-closed policy: a Password variable is redacted, Binary/Image
        dropped, scalars kept, and every text/unknown type scrubbed. The
        description is admin-authored free text, so it is scrubbed at the same
        boundary — an environment variable is treated as a PII/secret surface.
        """
        return {
            **var,
            "description": self.scrub_text(var.get("description")),
            "value": self._scrub_typed_value(var.get("dataType"), var.get("value")),
        }

    def list_queues(self) -> Ranked:
        """Rank every work queue by backlog, folding in the deferred count.

        WorkQueueSummary carries every state count except deferred; that one is
        folded in from the workQueueCompositions aggregate across the whole
        result set, added only for queues the aggregate actually reported a
        count for (an unknown count is omitted, never a fabricated zero). If the
        whole aggregate read is denied or fails, the listing still stands and
        signals it via ``meta.deferred_unavailable``.
        """
        ranked = rank(
            self.client.get_queues(),
            sort_key=lambda q: q.get("pendingItemCount", 0),
            sorted_by="pendingItemCount desc",
            reverse=True,
        )
        deferred = _deferred_counts(self.client, [q.get("id") for q in ranked.records])
        if deferred is None:
            return Ranked(ranked.records, ranked.sorted_by, {"deferred_unavailable": True})
        records = [
            {**q, "deferred": deferred[q["id"]]} if q.get("id") in deferred else q
            for q in ranked.records
        ]
        return Ranked(records, ranked.sorted_by)

    def get_queue(self, queue: str) -> dict:
        """Return one work queue's full detail by name or id."""
        queue_id = resolve_id(queue, self.client.get_queues(), entity="queue")
        return self.client.get_queue(queue_id)

    def list_queue_items(
        self,
        queue: str,
        state: str,
        start_date: str,
        end_date: str,
        status: str | None = None,
    ) -> Ranked:
        """Rank one queue's items by recency, filtered by state and date window."""
        state = validate_choice(state, "state", ITEM_STATES)
        require_window(start_date, end_date)
        queue_id = resolve_id(queue, self.client.get_queues(), entity="queue")
        items = self.client.get_queue_items(
            queue_id, state=state, status=status, start_date=start_date, end_date=end_date
        )
        return rank(
            [self._scrubbed_item(i) for i in items],
            sort_key=lambda i: i.get("lastUpdated") or "",
            sorted_by="lastUpdated desc",
            reverse=True,
        )

    def get_queue_item(self, item_id: str) -> dict:
        """Return one queue item in full, with its ``data`` collection scrubbed."""
        item_id = validate_uuid(item_id, "item_id", hint=_ITEM_ID_HINT)
        item = self.client.get_queue_item(item_id)
        scrubbed = {**item, "exceptionReason": self.scrub_text(item.get("exceptionReason"))}
        if "data" in scrubbed:
            scrubbed["data"] = self._scrubbed_collection(scrubbed["data"])
        return scrubbed

    def list_item_attempts(self, queue: str, item_id: str) -> Ranked:
        """Rank one item's attempt history, latest attempt first."""
        queue_id = resolve_id(queue, self.client.get_queues(), entity="queue")
        item_id = validate_uuid(item_id, "item_id", hint=_ITEM_ID_HINT)
        attempts = self.client.get_item_attempts(queue_id, item_id)
        return rank(
            [self._scrubbed_item(a) for a in attempts],
            sort_key=lambda a: a.get("attemptNumber") or 0,
            sorted_by="attemptNumber desc (latest attempt first)",
            reverse=True,
        )

    def list_sessions(
        self,
        start_date: str,
        end_date: str,
        process: str | None = None,
        resource: str | None = None,
        status: str | None = None,
    ) -> Ranked:
        """Rank run history (sessions) by recency within a date window, filtered."""
        require_window(start_date, end_date)
        if status is not None:
            status = validate_choice(status, "status", SESSION_STATUSES)
        sessions = self.client.get_sessions(start_date, end_date)
        if process:
            wanted = process.strip().casefold()
            sessions = [s for s in sessions if str(s.get("processName", "")).casefold() == wanted]
        if resource:
            wanted = resource.strip().casefold()
            sessions = [s for s in sessions if str(s.get("resourceName", "")).casefold() == wanted]
        if status:
            sessions = [s for s in sessions if s.get("status") == status]
        return rank(
            [self._scrubbed_session(s) for s in sessions],
            sort_key=lambda s: s.get("startTime") or "",
            sorted_by="startTime desc",
            reverse=True,
        )

    def get_session(self, session_id: str) -> dict:
        """Return one process run (session) in full by its id."""
        session_id = validate_uuid(
            session_id, "session_id", hint="Use the sessionId from list_sessions."
        )
        return self._scrubbed_session(self.client.get_session(session_id))

    def get_session_log(
        self,
        session_id: str,
        errors_only: bool = False,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> Ranked:
        """Rank one session's stage log latest-stage-first (failures end a log).

        ``errors_only`` and the optional start_date/end_date window are pushed
        to the v7 server (the exception-handling stage types, and a range on the
        per-stage execution time) so a long run is not dragged back in full just
        to surface its failure.
        """
        validate_optional_window(start_date, end_date)
        entries = self.client.get_session_log(
            session_id, errors_only=errors_only, start_date=start_date, end_date=end_date
        )
        return rank(
            [self._scrubbed_stage(e) for e in entries],
            sort_key=lambda e: e.get("logNumber") or 0,
            sorted_by="logNumber desc (latest stage first — failures end a log)",
            reverse=True,
        )

    def list_resources(self) -> Ranked:
        """Rank digital workers most-urgent-first (Missing/Offline/Warning lead)."""
        return rank(
            self.client.get_resources(),
            sort_key=resource_urgency,
            sorted_by="displayStatus urgency (Missing/Offline/Warning first), name",
        )

    def list_schedules(self) -> Ranked:
        """Rank schedules active-first then alphabetical, folding in the last run.

        The schedule definition carries no run history; each schedule's last
        outcome (status, start/end, duration) is folded in from the schedule-log
        endpoint, added only for schedules that have actually run (one that never
        has carries no ``last_run`` field, never a fabricated one). If the whole
        schedule-log read is denied or fails, the listing still stands and
        signals it via ``meta.last_run_unavailable``.
        """
        ranked = rank(
            self.client.get_schedules(),
            sort_key=lambda s: (bool(s.get("isRetired")), str(s.get("name") or "")),
            sorted_by="active first, then name (retired last)",
        )
        runs = _last_runs(self.client, [s.get("id") for s in ranked.records])
        if runs is None:
            return Ranked(ranked.records, ranked.sorted_by, {"last_run_unavailable": True})
        records = [
            {**s, "last_run": runs[s["id"]]} if s.get("id") in runs else s for s in ranked.records
        ]
        return Ranked(records, ranked.sorted_by)

    def list_processes(self) -> Ranked:
        """Rank the published process catalogue alphabetically by name."""
        return rank(
            self.client.get_processes(),
            sort_key=lambda p: str(p.get("processName") or ""),
            sorted_by="processName",
        )

    def list_queue_configurations(self) -> Ranked:
        """Rank active-queue configurations busiest-first; degrade below 7.4.

        On a transport/HTTP failure (older estate without the 7.4 endpoint, or a
        denied Queue Management read) returns an empty ``Ranked`` carrying an
        ``unavailable`` note in its meta, rather than raising — so the tool
        degrades visibly instead of failing the surface.
        """
        try:
            configurations = self.client.get_queue_configurations()
        except requests.RequestException as exc:
            return Ranked(
                [],
                "activeQueueStats.activeSessions desc",
                {
                    "unavailable": (
                        "queue configurations unavailable (requires Blue Prism 7.4+ "
                        f"and Queue Management read access): {exc}"
                    )
                },
            )
        return rank(
            configurations,
            sort_key=lambda c: (c.get("activeQueueStats") or {}).get("activeSessions") or 0,
            sorted_by="activeQueueStats.activeSessions desc",
            reverse=True,
        )

    def list_resource_pools(self) -> Ranked:
        """Rank resource pools largest-first (most members)."""
        return rank(
            self.client.get_resource_pools(),
            sort_key=lambda p: p.get("members") or 0,
            sorted_by="members desc",
            reverse=True,
        )

    def list_environment_variables(self) -> Ranked:
        """Rank environment variables alphabetically, scrubbed type-aware."""
        return rank(
            [self._scrubbed_env_var(v) for v in self.client.get_environment_variables()],
            sort_key=lambda v: str(v.get("name") or ""),
            sorted_by="name",
        )

    def list_process_groups(self) -> Ranked:
        """Rank the process tree folders-first then alphabetical."""
        return rank(
            self.client.get_process_groups(),
            sort_key=lambda n: (n.get("nodeType") != "Group", str(n.get("name") or "")),
            sorted_by="folders first, then name",
        )


def build_tier1_tools(engine) -> list[Callable]:
    """Build the visibility tools as thin adapters over *engine*'s domain methods."""

    def list_queues(limit: int = DEFAULT_LIMIT) -> dict:
        """List every work queue with its health counts.

        Each item gives the queue's name and id, its status (Running/Paused),
        and item counts by state — pending, locked, completed, exceptioned,
        total — plus the average work time per item. A per-queue `deferred`
        count is folded in where available; if a queue carries no `deferred`
        field that count was unknown for it, and if the whole estate's read was
        denied `meta.deferred_unavailable` is set. Use it to spot backlogs and
        queues accumulating exceptions, and to find a queue's name for the other
        queue tools.

        Results come back as {"items": [...], "meta": {...}}, sorted by pending
        count (biggest backlog first) and capped at `limit` (default 50).
        meta.truncated tells you whether you saw every queue.
        """
        return to_envelope(engine.list_queues(), limit)

    def get_queue(queue: str) -> dict:
        """Return one work queue's full detail by name or id.

        Gives the queue's status, max attempts, encryption flag, and item
        counts by state (pending, locked, completed, exceptioned, total) plus
        average work time. `queue` is the queue name as shown in list_queues
        (case-insensitive), or its UUID. (The per-queue `deferred` count is not
        included here — list_queues folds that in across its result set.)
        """
        return engine.get_queue(queue)

    def list_queue_items(
        queue: str,
        state: str,
        start_date: str,
        end_date: str,
        status: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """List items in one work queue, filtered by state and a date window.

        All three filters are REQUIRED — queues run to millions of items, so
        there is no unscoped listing. `queue` is a queue name (case-insensitive)
        or id; `state` is the item lifecycle state (Pending, Locked, Deferred,
        Completed, or Exceptioned); `start_date`/`end_date` (ISO) bound the
        items' last-updated time. Optional `status` matches the free-text tag
        processes attach to items.

        Each item carries its key value, priority, attempt number, timing
        fields, and — for exceptioned items — the exception reason, with
        personal data already removed. Item payload data is never included
        (the v7 list endpoint excludes it by design).

        Results come back as {"items": [...], "meta": {...}}, most recently
        updated first, capped at `limit` (default 50); meta.truncated tells
        you whether you saw everything in the window.
        """
        return to_envelope(
            engine.list_queue_items(queue, state, start_date, end_date, status), limit
        )

    def get_queue_item(item_id: str) -> dict:
        """Return one work queue item in full, INCLUDING its payload data.

        `item_id` is the item's UUID from list_queue_items (not its key value).
        This is the only read that returns the item's `data` — the Blue Prism
        collection the process was working — so use it to see exactly what a
        failed item was carrying. Personal data is removed before you see it:
        free-text fields are scrubbed, passwords are redacted, and binary/image
        fields are dropped (a marker is left in their place); numbers, flags and
        dates pass through.

        Returns the single item object (state, key value, attempt number,
        timing, the scrubbed exception reason, and the scrubbed `data`
        collection) — not a list envelope.

        Note: Blue Prism cannot return item data for queues encrypted with an
        application-server key; for those queues this call fails, and the
        other (no-data) item tools must be used instead.
        """
        return engine.get_queue_item(item_id)

    def list_item_attempts(queue: str, item_id: str, limit: int = DEFAULT_LIMIT) -> dict:
        """List the attempt history for one work queue item.

        `queue` is the queue name (case-insensitive, as shown in list_queues)
        or its UUID; `item_id` is the item's UUID from list_queue_items. Each
        attempt row gives its attempt number, the state and exception reason at
        that attempt (personal data already removed), and timing — so you can
        see how many times an item has been retried and why each attempt failed.
        Payload data is not included here (use get_queue_item for that).

        Results come back as {"items": [...], "meta": {...}}, latest attempt
        first, capped at `limit` (default 50); meta.truncated tells you whether
        every attempt is shown.
        """
        return to_envelope(engine.list_item_attempts(queue, item_id), limit)

    def list_sessions(
        start_date: str,
        end_date: str,
        process: str | None = None,
        resource: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """List process run history (sessions) within a required date window.

        `start_date`/`end_date` (ISO) are REQUIRED and bound the sessions'
        start time. Optionally narrow by `process` name, `resource` (digital
        worker) name — both case-insensitive — or `status` (Pending, Running,
        Completed, Stopped, Stopping, Terminated, or Warning).

        Each session gives the process and resource it ran on, status, start
        and end times, and for failed runs the termination reason and the
        exception type and message (personal data already removed). Use
        get_session_log with a sessionId to see why a specific run failed.

        Results come back as {"items": [...], "meta": {...}}, most recent
        first, capped at `limit` (default 50); meta.truncated tells you
        whether you saw every session in the window.
        """
        return to_envelope(
            engine.list_sessions(start_date, end_date, process, resource, status), limit
        )

    def get_session(session_id: str) -> dict:
        """Return one process run (session) in full by its id.

        `session_id` is the sessionId from list_sessions (a UUID) — fetch a
        single run directly without a date window. Gives the process and
        resource it ran on, status, start and end times, the latest stage
        reached, and for a failed run the termination reason and the exception
        type and message (personal data already removed).

        Returns the single session object, not a list envelope. Follow it with
        get_session_log on the same id to see the stage-by-stage detail of why
        the run failed.
        """
        return engine.get_session(session_id)

    def get_session_log(
        session_id: str,
        errors_only: bool = False,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """Return the stage-level execution log for one session — why a run failed.

        `session_id` is the sessionId from list_sessions. Each entry gives the
        stage name and type, its result text (personal data already removed),
        and timing. Failures sit at the END of a log, so entries come back
        most-recent-stage-first — the first items you see are the failure and
        what led to it.

        Two optional filters narrow a long run server-side, so you fetch only
        what you need to diagnose it: set `errors_only=True` to return just the
        exception-handling stages (where an exception was raised, caught, or
        resumed past) instead of every stage; and pass `start_date`/`end_date`
        (ISO) to bound the stages' execution time. Both default to off (the full
        log).

        Results come back as {"items": [...], "meta": {...}}, capped at `limit`
        (default 50) because long runs log thousands of stages; meta.truncated
        tells you whether earlier stages were cut. Raise `limit`, or use
        `errors_only`, to see the failure without paging the whole run.
        """
        return to_envelope(
            engine.get_session_log(session_id, errors_only, start_date, end_date), limit
        )

    def list_resources(limit: int = DEFAULT_LIMIT) -> dict:
        """List the digital workers (runtime resources) and their status.

        Each item gives the worker's name, pool and group, its display status
        (Working, Idle, Warning, Offline, Missing, ...), database status, and
        active/pending session counts. Use it to spot workers that are down or
        struggling.

        Results come back as {"items": [...], "meta": {...}}, most urgent
        first (Missing, then Offline, then Warning, then the rest), capped at
        `limit` (default 50); meta.truncated tells you whether you saw every
        worker.
        """
        return to_envelope(engine.list_resources(), limit)

    def list_schedules(limit: int = DEFAULT_LIMIT) -> dict:
        """List the schedules that run processes automatically.

        Each item gives the schedule's name, description, whether it is
        retired (disabled), its interval (Once/Minute/Hour/Day/Week/Month/
        Year), task count, and calendar. Where a schedule has run, a `last_run`
        object is folded in with its most recent outcome — status (completed,
        terminated, running, pending, or partExceptioned), start and end times,
        and duration; a schedule that has never run carries no `last_run`. (The
        API still exposes no next-run time.) If the schedule-log read is denied
        or fails, `meta.last_run_unavailable` is set and no schedule carries a
        `last_run`.

        Results come back as {"items": [...], "meta": {...}}, active schedules
        first then alphabetical (retired ones sort last), capped at `limit`
        (default 50); meta.truncated tells you whether you saw every schedule.
        """
        return to_envelope(engine.list_schedules(), limit)

    def list_processes(limit: int = DEFAULT_LIMIT) -> dict:
        """List the published automation processes (the process catalogue).

        Each item gives the process name, description, group, and attributes.
        Use it to find the exact process name for filtering sessions or
        summaries.

        Results come back as {"items": [...], "meta": {...}}, alphabetical by
        name, capped at `limit` (default 50); meta.truncated tells you whether
        you saw the whole catalogue.
        """
        return to_envelope(engine.list_processes(), limit)

    def list_queue_configurations(limit: int = DEFAULT_LIMIT) -> dict:
        """Map each active work queue to the process and resources that drain it.

        Each item gives an active queue's name and id, the id of the process
        assigned to work it (`assignedProcessId`) and of its assigned resource
        group (`assignedResourceGroupId`), and live activity — how many sessions
        are working it now, how many workers are available, and the estimated
        time/ETA to clear the backlog. Use it to see which process handles a
        queue and whether enough workers are on it. (Only active queues appear,
        so this is a smaller set than list_queues; cross-reference
        assignedProcessId against list_processes for the process name.)

        Results come back as {"items": [...], "meta": {...}}, busiest first
        (most active sessions), capped at `limit` (default 50).

        Needs Blue Prism 7.4 or later; against an older estate this returns no
        items with `meta.unavailable` explaining why, rather than failing.
        """
        return to_envelope(engine.list_queue_configurations(), limit)

    def list_resource_pools(limit: int = DEFAULT_LIMIT) -> dict:
        """List the resource pools (groupings of digital workers) and their size.

        Each item gives the pool's name and id, the number of member resources,
        and its reported database status (Ready/Offline/Pending/Unknown). Use it
        to see how the estate's workers are pooled and whether a pool is online.

        Results come back as {"items": [...], "meta": {...}}, largest pool
        first (most members), capped at `limit` (default 50).
        """
        return to_envelope(engine.list_resource_pools(), limit)

    def list_environment_variables(limit: int = DEFAULT_LIMIT) -> dict:
        """List the estate's environment variables (shared process configuration).

        Each item gives the variable's name, description, Blue Prism data type,
        and value. Personal data is removed before you see it: a Password
        variable is redacted, binary/image values are dropped (a marker is
        left), the free-text value and description are scrubbed, and
        numbers/flags/dates pass through. Use it to see the shared configuration
        processes depend on.

        Results come back as {"items": [...], "meta": {...}}, alphabetical by
        name, capped at `limit` (default 50); meta.truncated tells you whether
        you saw every variable.
        """
        return to_envelope(engine.list_environment_variables(), limit)

    def list_process_groups(limit: int = DEFAULT_LIMIT) -> dict:
        """List the process tree — the folders and processes in the catalogue.

        Each item is a node in the process group tree: either a folder
        (`nodeType` "Group") or a published process (`nodeType` "Item"), with
        its name, id, and last-modified time (a placeholder date for folders).
        Use it to see how the process catalogue is organised into groups.

        Results come back as {"items": [...], "meta": {...}}, folders first then
        processes, alphabetical within each, capped at `limit` (default 50);
        meta.truncated tells you whether you saw the whole tree.
        """
        return to_envelope(engine.list_process_groups(), limit)

    return [
        list_queues,
        get_queue,
        list_queue_items,
        get_queue_item,
        list_item_attempts,
        list_sessions,
        get_session,
        get_session_log,
        list_resources,
        list_schedules,
        list_processes,
        list_queue_configurations,
        list_resource_pools,
        list_environment_variables,
        list_process_groups,
    ]

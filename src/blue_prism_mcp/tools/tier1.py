"""Tier 1 — visibility tools: read-only primitives over the v7 entities.

Each tool closure's docstring IS the tool description an LLM client sees, so
they spell out the envelope contract, the required scoping, and what was
scrubbed — tight, single-purpose descriptions drive better tool selection.

The builder takes a client (live or mock — same surface) and a Scrubber; the
returned closures carry clean signatures for FastMCP's schema introspection.
PII boundaries here: ``exceptionReason`` on queue-item rows (lists and attempt
history), ``exceptionMessage`` on session rows (lists and single-session
detail), stage ``result`` text in session logs, and — only in the single-item
read — the item's ``data`` DataCollection, scrubbed type-aware (free text
through the scrubber, passwords redacted, binary/image dropped, collections
recursed).
"""

from __future__ import annotations

from typing import Callable

import requests

from ..pii import Scrubber
from .common import (
    DEFAULT_LIMIT,
    envelope,
    make_cached_scrub,
    require_window,
    resolve_id,
    resource_urgency,
    validate_choice,
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


def build_tier1_tools(client, scrubber: Scrubber) -> list[Callable]:
    """Build the eight visibility tools over *client*, scrubbing with *scrubber*."""
    scrub_text = make_cached_scrub(scrubber)

    def _scrubbed_item(item: dict) -> dict:
        return {**item, "exceptionReason": scrub_text(item.get("exceptionReason"))}

    def _scrubbed_session(session: dict) -> dict:
        return {**session, "exceptionMessage": scrub_text(session.get("exceptionMessage"))}

    def _scrubbed_stage(entry: dict) -> dict:
        return {**entry, "result": scrub_text(entry.get("result"))}

    def _scrubbed_collection(collection):
        """Scrub a Blue Prism DataCollection in place-by-value (recurses rows)."""
        if not isinstance(collection, dict) or not isinstance(collection.get("rows"), list):
            return collection
        rows = [
            {
                field: _scrubbed_value(cell) if isinstance(cell, dict) else cell
                for field, cell in row.items()
            }
            if isinstance(row, dict)
            else row
            for row in collection["rows"]
        ]
        return {**collection, "rows": rows}

    def _scrub_payload(value):
        """Scrub any DataValue payload shape, recursing nested collections.

        Strings go through the PII scrubber; lists (e.g. a RadioButtonsArray)
        scrub element-wise; a dict is a nested DataCollection and recurses;
        anything else (a number, boolean, or None) is already safe.
        """
        if isinstance(value, str):
            return scrub_text(value)
        if isinstance(value, list):
            return [_scrub_payload(v) for v in value]
        if isinstance(value, dict):
            return _scrubbed_collection(value)
        return value

    def _scrubbed_value(cell: dict) -> dict:
        """Scrub one DataValue, FAIL-CLOSED by Blue Prism value type.

        A diagnostic read must never leak a secret or a binary blob, so the
        policy errs toward scrubbing: Password is redacted wholesale;
        Binary/Image drop the base64 payload (keeping a marker); the scalar
        types (Number/Flag/Date/DateTime/Time/TimeSpan) keep their value —
        scrubbing them is wasted work and, under an NER backend, would mangle
        a legitimate date into a redaction marker. EVERY other type — Text,
        RadioButtons, Collection, AND any unknown or miscased type — has its
        string content scrubbed and nested collections recursed, so a value
        type the server spells differently fails closed rather than passing
        through verbatim. additionalParameters (a Binary's file path, etc.) is
        free text too, so it is scrubbed at the same boundary. Matching is
        case-insensitive, like the start-up parameter validator.
        """
        value_type = str(cell.get("valueType") or "").strip().casefold()
        new = dict(cell)
        if value_type == "password":
            new["value"] = "[PASSWORD]"
        elif value_type in ("binary", "image"):
            new["value"] = f"[{value_type.upper()} omitted]"
        elif value_type not in _SCALAR_VALUE_TYPES:
            new["value"] = _scrub_payload(cell.get("value"))
        extra = cell.get("additionalParameters")
        if isinstance(extra, list):
            new["additionalParameters"] = [
                scrub_text(x) if isinstance(x, str) else x for x in extra
            ]
        return new

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
        result = envelope(
            client.get_queues(),
            sort_key=lambda q: q.get("pendingItemCount", 0),
            sorted_by="pendingItemCount desc",
            limit=limit,
            reverse=True,
        )
        # WorkQueueSummary carries every state count except deferred; fold that
        # one in from the workQueueCompositions aggregate, but only for the
        # queues actually returned (a cheaper request than the full estate). The
        # field is added only for queues the aggregate actually reported a count
        # for — an unknown count is omitted, never a fabricated zero. If the
        # whole read is denied or fails, the listing still stands and degrades
        # visibly via meta.deferred_unavailable.
        deferred = _deferred_counts(client, [q.get("id") for q in result["items"]])
        if deferred is None:
            result["meta"]["deferred_unavailable"] = True
        else:
            result["items"] = [
                {**q, "deferred": deferred[q["id"]]} if q.get("id") in deferred else q
                for q in result["items"]
            ]
        return result

    def get_queue(queue: str) -> dict:
        """Return one work queue's full detail by name or id.

        Gives the queue's status, max attempts, encryption flag, and item
        counts by state (pending, locked, completed, exceptioned, total) plus
        average work time. `queue` is the queue name as shown in list_queues
        (case-insensitive), or its UUID. (The per-queue `deferred` count is not
        included here — list_queues folds that in across its result set.)
        """
        queue_id = resolve_id(queue, client.get_queues(), entity="queue")
        return client.get_queue(queue_id)

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
        state = validate_choice(state, "state", ITEM_STATES)
        require_window(start_date, end_date)
        queue_id = resolve_id(queue, client.get_queues(), entity="queue")
        items = client.get_queue_items(
            queue_id, state=state, status=status, start_date=start_date, end_date=end_date
        )
        return envelope(
            [_scrubbed_item(i) for i in items],
            sort_key=lambda i: i.get("lastUpdated") or "",
            sorted_by="lastUpdated desc",
            limit=limit,
            reverse=True,
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
        item_id = validate_uuid(item_id, "item_id", hint=_ITEM_ID_HINT)
        item = client.get_queue_item(item_id)
        scrubbed = {**item, "exceptionReason": scrub_text(item.get("exceptionReason"))}
        if "data" in scrubbed:
            scrubbed["data"] = _scrubbed_collection(scrubbed["data"])
        return scrubbed

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
        queue_id = resolve_id(queue, client.get_queues(), entity="queue")
        item_id = validate_uuid(item_id, "item_id", hint=_ITEM_ID_HINT)
        attempts = client.get_item_attempts(queue_id, item_id)
        return envelope(
            [_scrubbed_item(a) for a in attempts],
            sort_key=lambda a: a.get("attemptNumber") or 0,
            sorted_by="attemptNumber desc (latest attempt first)",
            limit=limit,
            reverse=True,
        )

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
        require_window(start_date, end_date)
        if status is not None:
            status = validate_choice(status, "status", SESSION_STATUSES)
        sessions = client.get_sessions(start_date, end_date)
        if process:
            wanted = process.strip().casefold()
            sessions = [s for s in sessions if str(s.get("processName", "")).casefold() == wanted]
        if resource:
            wanted = resource.strip().casefold()
            sessions = [s for s in sessions if str(s.get("resourceName", "")).casefold() == wanted]
        if status:
            sessions = [s for s in sessions if s.get("status") == status]
        return envelope(
            [_scrubbed_session(s) for s in sessions],
            sort_key=lambda s: s.get("startTime") or "",
            sorted_by="startTime desc",
            limit=limit,
            reverse=True,
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
        session_id = validate_uuid(
            session_id, "session_id", hint="Use the sessionId from list_sessions."
        )
        return _scrubbed_session(client.get_session(session_id))

    def get_session_log(session_id: str, limit: int = DEFAULT_LIMIT) -> dict:
        """Return the stage-level execution log for one session — why a run failed.

        `session_id` is the sessionId from list_sessions. Each entry gives the
        stage name and type, its result text (personal data already removed),
        and timing. Failures sit at the END of a log, so entries come back
        most-recent-stage-first — the first items you see are the failure and
        what led to it.

        Results come back as {"items": [...], "meta": {...}}, capped at `limit`
        (default 50) because long runs log thousands of stages; meta.truncated
        tells you whether earlier stages were cut. Raise `limit` to see more
        of the run's history.
        """
        entries = client.get_session_log(session_id)
        return envelope(
            [_scrubbed_stage(e) for e in entries],
            sort_key=lambda e: e.get("logNumber") or 0,
            sorted_by="logNumber desc (latest stage first — failures end a log)",
            limit=limit,
            reverse=True,
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
        return envelope(
            client.get_resources(),
            sort_key=resource_urgency,
            sorted_by="displayStatus urgency (Missing/Offline/Warning first), name",
            limit=limit,
        )

    def list_schedules(limit: int = DEFAULT_LIMIT) -> dict:
        """List the schedules that run processes automatically.

        Each item gives the schedule's name, description, whether it is
        retired (disabled), its interval (Once/Minute/Hour/Day/Week/Month/
        Year), task count, and calendar. The API exposes no next-run time;
        run outcomes live in the schedule logs (not yet exposed here).

        Results come back as {"items": [...], "meta": {...}}, active schedules
        first then alphabetical (retired ones sort last), capped at `limit`
        (default 50); meta.truncated tells you whether you saw every schedule.
        """
        return envelope(
            client.get_schedules(),
            sort_key=lambda s: (bool(s.get("isRetired")), str(s.get("name") or "")),
            sorted_by="active first, then name (retired last)",
            limit=limit,
        )

    def list_processes(limit: int = DEFAULT_LIMIT) -> dict:
        """List the published automation processes (the process catalogue).

        Each item gives the process name, description, group, and attributes.
        Use it to find the exact process name for filtering sessions or
        summaries.

        Results come back as {"items": [...], "meta": {...}}, alphabetical by
        name, capped at `limit` (default 50); meta.truncated tells you whether
        you saw the whole catalogue.
        """
        return envelope(
            client.get_processes(),
            sort_key=lambda p: str(p.get("processName") or ""),
            sorted_by="processName",
            limit=limit,
        )

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
    ]

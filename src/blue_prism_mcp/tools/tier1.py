"""Tier 1 — visibility tools: read-only primitives over the v7 entities.

Each tool closure's docstring IS the tool description an LLM client sees, so
they spell out the envelope contract, the required scoping, and what was
scrubbed — tight, single-purpose descriptions drive better tool selection.

The builder takes a client (live or mock — same surface) and a Scrubber; the
returned closures carry clean signatures for FastMCP's schema introspection.
PII boundaries here: ``exceptionReason`` on queue-item rows,
``exceptionMessage`` on session rows, and stage ``result`` text in session
logs (stage results can carry item payloads).
"""

from __future__ import annotations

from typing import Callable

from ..pii import Scrubber
from .common import (
    DEFAULT_LIMIT,
    envelope,
    make_cached_scrub,
    require_window,
    resolve_id,
    resource_urgency,
    validate_choice,
)

# The WorkQueueItemNoData lifecycle enum, minus its "None" placeholder (a
# filter on None is meaningless). The API's `state` is what an operator calls
# an item's status; the API's `status` field is free user-supplied text.
ITEM_STATES = frozenset({"Pending", "Locked", "Deferred", "Completed", "Exceptioned"})

# SessionStatus, verified against the 7.5.1 spec.
SESSION_STATUSES = frozenset(
    {"Pending", "Running", "Terminated", "Stopped", "Completed", "Stopping", "Warning"}
)


def build_tier1_tools(client, scrubber: Scrubber) -> list[Callable]:
    """Build the eight visibility tools over *client*, scrubbing with *scrubber*."""
    scrub_text = make_cached_scrub(scrubber)

    def _scrubbed_item(item: dict) -> dict:
        return {**item, "exceptionReason": scrub_text(item.get("exceptionReason"))}

    def _scrubbed_session(session: dict) -> dict:
        return {**session, "exceptionMessage": scrub_text(session.get("exceptionMessage"))}

    def _scrubbed_stage(entry: dict) -> dict:
        return {**entry, "result": scrub_text(entry.get("result"))}

    def list_queues(limit: int = DEFAULT_LIMIT) -> dict:
        """List every work queue with its health counts.

        Each item gives the queue's name and id, its status (Running/Paused),
        and item counts by state — pending, locked, completed, exceptioned,
        total — plus the average work time per item. Use it to spot backlogs
        and queues accumulating exceptions, and to find a queue's name for the
        other queue tools.

        Results come back as {"items": [...], "meta": {...}}, sorted by pending
        count (biggest backlog first) and capped at `limit` (default 50).
        meta.truncated tells you whether you saw every queue.
        """
        return envelope(
            client.get_queues(),
            sort_key=lambda q: q.get("pendingItemCount", 0),
            sorted_by="pendingItemCount desc",
            limit=limit,
            reverse=True,
        )

    def get_queue(queue: str) -> dict:
        """Return one work queue's full detail by name or id.

        Gives the queue's status, max attempts, encryption flag, and item
        counts by state (pending, locked, completed, exceptioned, total) plus
        average work time. `queue` is the queue name as shown in list_queues
        (case-insensitive), or its UUID.
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
        list_sessions,
        get_session_log,
        list_resources,
        list_schedules,
        list_processes,
    ]

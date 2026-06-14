"""Tier 3 — control tools: the governed action surface (Phase 5).

These mutate the estate, so three governance layers sit between the model and
the API (see governance.py and DESIGN.md):

* They are built only when ``enable_actions`` is true — the shipped default
  registers nothing here.
* Each tool is registered only when the service account's permissions (from
  ``GET /user/permissions``) satisfy what its endpoint documents — a tool the
  account cannot execute does not exist as far as the model is concerned.
* Every call defaults to ``dry_run=True``: names are resolved, inputs are
  validated, and the exact write that WOULD be issued comes back — but
  nothing is sent. The model must pass ``dry_run=False`` explicitly, and
  every invocation (dry or live) lands in the audit log, with the attempt
  line written before the write is issued.

Two writes carry verify-live caveats from the spec (flagged in their
docstrings): the JSON-Patch path for deferral and the schedule retire body
are underdocumented, which shipping-disabled tolerates — verify both against
a live estate before ever enabling actions.

Audit args carry ids, names, and dates only — never item payloads or
exception text (the audit log records entity types, not content); error
lines record the exception class and HTTP status via audit_detail, never
the message.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from ..governance import (
    TOOL_PERMISSIONS,
    UNRETIRE_EXTRA_PERMISSION,
    AuditLog,
    audit_detail,
    holds,
    unsatisfied_clauses,
)
from .common import (
    resolve_id,
    validate_iso,
    validate_positive_int,
    validate_session_parameters,
    validate_uuid,
)

_log = logging.getLogger("blue_prism_mcp.tier3")

_ITEM_ID_HINT = "Use list_queue_items to find the item's id (not its key value)."


def build_tier3_tools(
    client, audit: AuditLog, permissions: list[str]
) -> tuple[list[Callable], dict[str, list[str]]]:
    """Build the action tools *permissions* allow, audited via *audit*.

    Returns the allowed tools plus the withheld map — every withheld tool's
    unsatisfied permission clauses. The split is derived once, here, so what
    registers and what the startup audit line reports can never disagree.
    """
    clauses_missing = {tool: unsatisfied_clauses(tool, permissions) for tool in TOOL_PERMISSIONS}
    allowed = {tool for tool, missing in clauses_missing.items() if not missing}
    withheld = {tool: missing for tool, missing in sorted(clauses_missing.items()) if missing}
    # Resolved once at build time: a tool's behavior is fixed at registration,
    # never coupled to later mutation of the shared permissions list.
    can_unretire = holds(permissions, UNRETIRE_EXTRA_PERMISSION)

    def _run(
        action: str,
        args: dict[str, Any],
        dry_run: bool,
        execute: Callable[[], Any],
    ) -> dict:
        """Audit-then-act: dry runs return the validated call unsent."""
        if dry_run:
            audit.record(action, args, status="dry_run")
            return {"action": action, "dry_run": True, "would": args}
        # The attempt line lands before the write, so no estate mutation can
        # outrun its audit record; an unwritable audit file blocks the action.
        # Once the write is issued the calculus inverts: the audit can no
        # longer prevent anything, only misreport it, so post-write audit
        # failures are logged (stderr — stdout is JSON-RPC) and surfaced
        # without masking what actually happened to the estate.
        audit.record(action, args, status="attempt")
        try:
            result = execute()
        except Exception as exc:
            try:
                audit.record(action, args, status="error", detail=audit_detail(exc))
            except Exception:
                _log.exception("audit error line failed for %s", action)
            raise
        response = {"action": action, "dry_run": False, "result": result}
        try:
            audit.record(action, args, status="success")
        except Exception:
            # The mutation already happened; raising here would make a
            # completed action look failed and invite an unsafe retry.
            _log.exception("audit success line failed for %s", action)
            response["audit_status"] = "success_line_failed"
        return response

    def retry_queue_item(queue: str, item_id: str, dry_run: bool = True) -> dict:
        """Retry a failed (exceptioned) work queue item by creating a new attempt.

        `queue` is the queue name (case-insensitive, as shown in list_queues)
        or its UUID; `item_id` is the item's UUID from list_queue_items. The
        item re-enters the queue as Pending and a digital worker will pick it
        up again.

        By default this is a DRY RUN: it validates and returns the exact call
        it would make without changing anything. Pass dry_run=false to
        actually retry the item; the result then carries the new attempt id.
        Every invocation is audit-logged.
        """
        queue_id = resolve_id(queue, client.get_queues(), entity="queue")
        item_id = validate_uuid(item_id, "item_id", hint=_ITEM_ID_HINT)
        args = {"queue": queue, "queue_id": queue_id, "item_id": item_id}
        return _run(
            "retry_queue_item",
            args,
            dry_run,
            lambda: client.retry_queue_item(queue_id, item_id),
        )

    def defer_queue_item(
        queue: str,
        item_id: str,
        attempt_number: int,
        defer_until: str,
        dry_run: bool = True,
    ) -> dict:
        """Defer a work queue item's current attempt until a future time.

        The item is held and no digital worker will work it before
        `defer_until` (ISO datetime, e.g. 2026-03-01T09:00:00). `queue` is the
        queue name (case-insensitive) or UUID; `item_id` is the item's UUID
        and `attempt_number` its current attempt number, both from
        list_queue_items — deferral is attempt-scoped, so a stale attempt
        number is rejected by the API rather than deferring the wrong work.

        By default this is a DRY RUN: it validates and returns the exact call
        it would make without changing anything. Pass dry_run=false to
        actually defer the item. Every invocation is audit-logged.

        CAUTION: the v7 spec does not enumerate the patchable fields for this
        endpoint; the /deferredDate patch path is inferred from the item
        schema and must be verified against a live estate before this tool is
        relied on (see DESIGN.md "Needs day-one verification").
        """
        validate_iso(defer_until, "defer_until", required=True)
        attempt_number = validate_positive_int(
            attempt_number,
            "attempt_number",
            hint="Use the item's current attempt number from list_queue_items.",
        )
        queue_id = resolve_id(queue, client.get_queues(), entity="queue")
        item_id = validate_uuid(item_id, "item_id", hint=_ITEM_ID_HINT)
        args = {
            "queue": queue,
            "queue_id": queue_id,
            "item_id": item_id,
            "attempt_number": attempt_number,
            "defer_until": defer_until,
        }
        return _run(
            "defer_queue_item",
            args,
            dry_run,
            lambda: client.defer_queue_item(queue_id, item_id, attempt_number, defer_until),
        )

    def start_process(
        process: str,
        resource: str,
        parameters: dict | None = None,
        dry_run: bool = True,
    ) -> dict:
        """Start a published process on a digital worker, now.

        `process` is the process name (case-insensitive, as shown in
        list_processes) or its UUID; `resource` is the digital worker's name
        (as shown in list_resources) or UUID.

        `parameters` (optional) sets the process's start-up inputs: a mapping of
        parameter name to {"valueType": <type>, "value": <value>}, e.g.
        {"InvoiceDate": {"valueType": "Date", "value": "2026-03-01"}}. valueType
        is one of Text, Number, Flag, Date, DateTime, Time, TimeSpan, Password,
        Collection, Image, Binary, RadioButtons. Omit it for a process that
        takes no inputs.

        Creates a session, applies any parameters, and requests it run; the
        result carries the new sessionId — follow it with list_sessions or
        get_session_log.

        By default this is a DRY RUN: it validates and returns the exact call
        it would make without changing anything. Pass dry_run=false to
        actually start the process. Every invocation is audit-logged — parameter
        names and types only, never their values.
        """
        process_id = resolve_id(
            process,
            client.get_processes(),
            entity="process",
            id_key="processId",
            name_key="processName",
        )
        resource_id = resolve_id(resource, client.get_resources(), entity="resource")
        params = validate_session_parameters(parameters)
        args = {
            "process": process,
            "process_id": process_id,
            "resource": resource,
            "resource_id": resource_id,
        }
        if params:
            # Audit and dry-run echo carry parameter NAMES and TYPES only — a
            # value can be a Password or other sensitive payload, so values
            # never enter the audit log or the returned would-be call.
            args["parameter_types"] = {n: spec["valueType"] for n, spec in params.items()}
        return _run(
            "start_process",
            args,
            dry_run,
            lambda: client.start_process(process_id, resource_id, parameters=params),
        )

    def stop_session(session_id: str, dry_run: bool = True) -> dict:
        """Request a running session stop — start_process's control sibling.

        `session_id` is the sessionId from list_sessions (a UUID). Blue Prism
        is asked to stop that session; the stop takes effect when the process
        next yields, so it is a request, not an instant kill. Use this to halt
        a run that is stuck, looping, or was started in error.

        By default this is a DRY RUN: it validates and returns the exact call
        it would make without changing anything. Pass dry_run=false to actually
        request the stop. Every invocation is audit-logged.
        """
        session_id = validate_uuid(
            session_id,
            "session_id",
            hint="Use the sessionId from list_sessions.",
        )
        args = {"session_id": session_id}
        return _run(
            "stop_session",
            args,
            dry_run,
            lambda: client.stop_session(session_id),
        )

    def set_schedule_enabled(schedule: str, enabled: bool, dry_run: bool = True) -> dict:
        """Enable (unretire) or disable (retire) a schedule.

        `schedule` is the schedule name (case-insensitive, as shown in
        list_schedules) or its id. enabled=false retires the schedule so it
        stops running automatically; enabled=true brings a retired schedule
        back. Re-enabling needs the Create Schedule permission on top of the
        retire permissions — without it this tool can only disable.

        By default this is a DRY RUN: it validates and returns the exact call
        it would make without changing anything. Pass dry_run=false to
        actually change the schedule. Every invocation is audit-logged.

        CAUTION: the v7 spec omits the retire flag from this endpoint's
        published request schema; the {"isRetired": ...} body mirrors the
        schedule's read schema and must be verified against a live estate
        before this tool is relied on (see DESIGN.md "Needs day-one
        verification").
        """
        if enabled and not can_unretire:
            raise ValueError(
                "Unretiring a schedule requires the Create Schedule permission, "
                "which this service account does not hold — set_schedule_enabled "
                "can only disable (enabled=false) here."
            )
        schedule_id = resolve_id(schedule, client.get_schedules(), entity="schedule")
        args = {"schedule": schedule, "schedule_id": schedule_id, "enabled": enabled}
        return _run(
            "set_schedule_enabled",
            args,
            dry_run,
            lambda: client.set_schedule_enabled(schedule_id, enabled),
        )

    def trigger_schedule(
        schedule: str, start_time: str | None = None, dry_run: bool = True
    ) -> dict:
        """Run a schedule immediately, or at a specific time.

        `schedule` is the schedule name (case-insensitive, as shown in
        list_schedules) or its id. With no `start_time` the schedule runs
        now; pass an ISO datetime (e.g. 2026-03-01T09:00:00) to run it once
        at that time instead. This is one extra run — it does not change the
        schedule's own timetable.

        By default this is a DRY RUN: it validates and returns the exact call
        it would make without changing anything. Pass dry_run=false to
        actually trigger the run. Every invocation is audit-logged.
        """
        validate_iso(start_time, "start_time")
        schedule_id = resolve_id(schedule, client.get_schedules(), entity="schedule")
        args = {"schedule": schedule, "schedule_id": schedule_id, "start_time": start_time}
        return _run(
            "trigger_schedule",
            args,
            dry_run,
            lambda: client.trigger_schedule(schedule_id, start_time),
        )

    tools = [
        retry_queue_item,
        defer_queue_item,
        start_process,
        stop_session,
        set_schedule_enabled,
        trigger_schedule,
    ]
    return [tool for tool in tools if tool.__name__ in allowed], withheld

"""MockBPClient — an offline, in-memory stand-in for BPClient.

The dashboard's mock_provider read JSON fixtures so the UI could run with no
live estate. Here the same idea becomes a drop-in client: it exposes the exact
surface of BPClient — the Tier 1 reads plus the Phase 2 extensions (queue items,
processes, the session stage-log) and the Tier 3 writes — but serves data held
in memory, so tool tests (Phase 4) and local runs need neither a Blue Prism
server nor mocked HTTP. The writes mutate the in-memory fixtures, so a test can
call retry/defer/trigger and then observe the effect on a subsequent read.

Fixture records mirror the verified v7 response schemas (see DESIGN.md's ground
truth): queues are WorkQueueSummary (per-state item counts on the queue row),
sessions are SessionSummary (startTime/endTime, the status enum,
exceptionMessage/terminationReason), items are WorkQueueItemNoData (no payload
`data`, exceptionReason present), processes are Process (processId/processName
— the one entity not keyed id/name), log entries are SessionLogSummary, and
schedule ids are integers (the one non-UUID id in the API). The `queue` key on
item fixtures is mock-internal plumbing (which queue holds the item), not an
API field — the single-item read drops it and attaches the WorkQueueItem `data`
payload (a DataCollection, held per-item in _DEFAULT_ITEM_DATA), which the list
read never carries. Attempt history (_DEFAULT_ITEM_ATTEMPTS) is NoData rows.

Seed it with your own data, or accept the small built-in fixtures below.
"""

from __future__ import annotations

_DEFAULT_RESOURCES: list[dict] = [
    {
        "id": "5d2c8e0a-71b4-4a8e-9f30-000000000001",
        "name": "BOT-01",
        "poolName": None,
        "groupName": "Production",
        "attributes": [],
        "activeSessionCount": 0,
        "warningSessionCount": 0,
        "pendingSessionCount": 0,
        "databaseStatus": "Ready",
        "displayStatus": "Idle",
        "resourceType": "Enterprise",
    },
    {
        "id": "5d2c8e0a-71b4-4a8e-9f30-000000000002",
        "name": "BOT-02",
        "poolName": None,
        "groupName": "Production",
        "attributes": [],
        "activeSessionCount": 1,
        "warningSessionCount": 0,
        "pendingSessionCount": 0,
        "databaseStatus": "Ready",
        "displayStatus": "Working",
        "resourceType": "Enterprise",
    },
    {
        "id": "5d2c8e0a-71b4-4a8e-9f30-000000000003",
        "name": "BOT-03",
        "poolName": None,
        "groupName": "Production",
        "attributes": [],
        "activeSessionCount": 0,
        "warningSessionCount": 0,
        "pendingSessionCount": 0,
        "databaseStatus": "Offline",
        "displayStatus": "Offline",
        "resourceType": "Enterprise",
    },
]

_DEFAULT_QUEUES: list[dict] = [
    {
        "id": "9b6f3a1c-2e45-4d07-8c11-000000000101",
        "name": "Invoices",
        "keyField": "Invoice Number",
        "status": "Running",
        "isEncrypted": False,
        "maxAttempts": 3,
        "pendingItemCount": 12,
        "completedItemCount": 340,
        "lockedItemCount": 1,
        "exceptionedItemCount": 5,
        "totalItemCount": 358,
        "averageWorkTime": "00:01:24",
        "groupName": "Finance",
    },
    {
        "id": "9b6f3a1c-2e45-4d07-8c11-000000000102",
        "name": "Onboarding",
        "keyField": "Customer Id",
        "status": "Running",
        "isEncrypted": False,
        "maxAttempts": 3,
        "pendingItemCount": 0,
        "completedItemCount": 88,
        "lockedItemCount": 0,
        "exceptionedItemCount": 0,
        "totalItemCount": 88,
        "averageWorkTime": "00:03:02",
        "groupName": "Operations",
    },
]

_DEFAULT_SCHEDULES: list[dict] = [
    {
        "id": 1,
        "name": "Daily Invoice Run",
        "description": "Runs Invoice Processing every weekday morning",
        "isRetired": False,
        "tasksCount": 1,
        "intervalType": "Day",
        "calendarName": "Working Week",
    },
    {
        "id": 2,
        "name": "Weekly Reconciliation",
        "description": "Legacy reconciliation run",
        "isRetired": True,
        "tasksCount": 2,
        "intervalType": "Week",
        "calendarName": "Working Week",
    },
]

_PROC_INVOICES = "7c0e4f2b-93d1-4b66-a2af-000000000201"
_PROC_ONBOARDING = "7c0e4f2b-93d1-4b66-a2af-000000000202"

_DEFAULT_PROCESSES: list[dict] = [
    {
        "processId": _PROC_INVOICES,
        "processName": "Invoice Processing",
        "processDescription": "Three-way match and ledger posting",
        "groupName": "Finance",
        "attributes": ["Published"],
    },
    {
        "processId": _PROC_ONBOARDING,
        "processName": "Customer Onboarding",
        "processDescription": "KYC checks and account creation",
        "groupName": "Operations",
        "attributes": ["Published"],
    },
]

_DEFAULT_SESSIONS: list[dict] = [
    {
        "sessionId": "e8a9d7c2-5f10-4b3e-bd64-000000000301",
        "sessionNumber": 1,
        "processId": _PROC_INVOICES,
        "processName": "Invoice Processing",
        "resourceId": "5d2c8e0a-71b4-4a8e-9f30-000000000001",
        "resourceName": "BOT-01",
        "status": "Completed",
        "startTime": "2026-03-01T09:00:00Z",
        "endTime": "2026-03-01T09:09:00Z",
        "terminationReason": "None",
        "exceptionType": None,
        "exceptionMessage": None,
    },
    {
        "sessionId": "e8a9d7c2-5f10-4b3e-bd64-000000000302",
        "sessionNumber": 2,
        "processId": _PROC_ONBOARDING,
        "processName": "Customer Onboarding",
        "resourceId": "5d2c8e0a-71b4-4a8e-9f30-000000000002",
        "resourceName": "BOT-02",
        "status": "Terminated",
        "startTime": "2026-03-02T10:00:00Z",
        "endTime": "2026-03-02T10:01:35Z",
        "terminationReason": "ProcessError",
        "exceptionType": "System Exception",
        "exceptionMessage": "Customer record not found for ref 4929 1234 5678 9012",
    },
    {
        "sessionId": "e8a9d7c2-5f10-4b3e-bd64-000000000303",
        "sessionNumber": 3,
        "processId": _PROC_INVOICES,
        "processName": "Invoice Processing",
        "resourceId": "5d2c8e0a-71b4-4a8e-9f30-000000000001",
        "resourceName": "BOT-01",
        "status": "Completed",
        "startTime": "2026-03-05T09:00:00Z",
        "endTime": "2026-03-05T09:14:40Z",
        "terminationReason": "None",
        "exceptionType": None,
        "exceptionMessage": None,
    },
]

_QUEUE_INVOICES = "9b6f3a1c-2e45-4d07-8c11-000000000101"
_QUEUE_ONBOARDING = "9b6f3a1c-2e45-4d07-8c11-000000000102"

_DEFAULT_QUEUE_ITEMS: list[dict] = [
    {
        "queue": _QUEUE_INVOICES,  # mock-internal: which queue holds the item
        "id": "f3b2a190-8c47-4e2d-9b55-000000000401",
        "priority": 1,
        "state": "Completed",
        "keyValue": "INV-1001",
        "status": "",
        "attemptNumber": 1,
        "lastUpdated": "2026-03-01T09:05:00Z",
        "completedDate": "2026-03-01T09:05:00Z",
        "workTimeInSeconds": 84,
        "exceptionReason": None,
        "resource": "BOT-01",
    },
    {
        "queue": _QUEUE_INVOICES,
        "id": "f3b2a190-8c47-4e2d-9b55-000000000402",
        "priority": 1,
        "state": "Exceptioned",
        "keyValue": "INV-1002",
        "status": "",
        "attemptNumber": 1,
        "lastUpdated": "2026-03-02T11:20:00Z",
        "exceptionedDate": "2026-03-02T11:20:00Z",
        "workTimeInSeconds": 40,
        "exceptionReason": "Invoice total did not match purchase order",
        "resource": "BOT-01",
    },
    {
        "queue": _QUEUE_ONBOARDING,
        "id": "f3b2a190-8c47-4e2d-9b55-000000000403",
        "priority": 2,
        "state": "Pending",
        "keyValue": "CUST-0042",
        "status": "",
        "attemptNumber": 1,
        "lastUpdated": "2026-03-03T08:00:00Z",
        "workTimeInSeconds": 0,
        "exceptionReason": None,
        "resource": None,
    },
]

_ITEM_INVOICE_EXCEPTION = "f3b2a190-8c47-4e2d-9b55-000000000402"

# The payload `data` for the single-item read (WorkQueueItem). Only the
# single-item GET carries this; lists and attempt history are WorkQueueItemNoData.
# The exceptioned invoice item carries personal data across every value type the
# scrubber must handle: free Text (scrubbed), a Password (redacted), a Binary
# blob (dropped), scalars (kept), and a nested Collection (recursed).
_DEFAULT_ITEM_DATA: dict[str, dict] = {
    _ITEM_INVOICE_EXCEPTION: {
        "rows": [
            {
                "Supplier": {"valueType": "Text", "value": "Acme Trading Ltd"},
                "Contact": {
                    "valueType": "Text",
                    "value": "Chase supplier on 07700 900123 before re-running",
                },
                "Amount": {"valueType": "Number", "value": 1499.99},
                "Approved": {"valueType": "Flag", "value": False},
                "VaultPassword": {"valueType": "Password", "value": "s3cret-Pa55word"},
                "Scan": {
                    "valueType": "Binary",
                    "value": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQ==",
                    "additionalParameters": ["invoice.pdf"],
                },
                "Logo": {
                    "valueType": "Image",
                    "value": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQ==",
                },
                "LineItems": {
                    "valueType": "Collection",
                    "value": {
                        "rows": [
                            {
                                "Desc": {
                                    "valueType": "Text",
                                    "value": "Reconcile and call back on 07700 900456",
                                },
                                "Net": {"valueType": "Number", "value": 1249.99},
                            }
                        ]
                    },
                },
            }
        ]
    },
}

# Attempt history for the single-item attempts read (WorkQueueItemNoData rows,
# newest carrying the live state). exceptionReason is the scrub target.
_DEFAULT_ITEM_ATTEMPTS: dict[str, list[dict]] = {
    _ITEM_INVOICE_EXCEPTION: [
        {
            "id": _ITEM_INVOICE_EXCEPTION,
            "state": "Exceptioned",
            "keyValue": "INV-1002",
            "attemptNumber": 1,
            "lastUpdated": "2026-03-02T11:20:00Z",
            "exceptionedDate": "2026-03-02T11:20:00Z",
            "workTimeInSeconds": 40,
            "exceptionReason": "Invoice total did not match PO; query raised by 07700 900123",
            "resource": "BOT-01",
        },
        {
            "id": _ITEM_INVOICE_EXCEPTION,
            "state": "Pending",
            "keyValue": "INV-1002",
            "attemptNumber": 2,
            "lastUpdated": "2026-03-02T12:00:00Z",
            "workTimeInSeconds": 0,
            "exceptionReason": None,
            "resource": None,
        },
    ],
}

_DEFAULT_SESSION_LOGS: dict[str, list[dict]] = {
    "e8a9d7c2-5f10-4b3e-bd64-000000000301": [
        {"logNumber": 1, "stageName": "Start", "stageType": "Start", "result": ""},
        {"logNumber": 2, "stageName": "Read Invoice", "stageType": "Action", "result": "OK"},
        {"logNumber": 3, "stageName": "Post to Ledger", "stageType": "Action", "result": "OK"},
    ],
    "e8a9d7c2-5f10-4b3e-bd64-000000000302": [
        {"logNumber": 1, "stageName": "Start", "stageType": "Start", "result": ""},
        {
            "logNumber": 2,
            "stageName": "Validate Customer",
            "stageType": "Action",
            "result": "ERROR: Customer record not found for ref 4929 1234 5678 9012",
            "resultType": "Text",
        },
    ],
}

# The documented permission names for every Tier 3 write (see DESIGN.md's
# ground truth). The default account can do everything, so tool tests exercise
# the full action surface; pass a narrower list to test capability gating.
_DEFAULT_PERMISSIONS: list[str] = [
    "Full Access to Queue Management",
    "Execute Process",
    "Control Resource",
    "Edit Schedule",
    "Retire Schedule",
    "Create Schedule",
]

_DEFAULT_LIMITS_AND_USAGE: dict = {
    "publishedProcessesLimit": None,  # null = unlimited, per the spec
    "publishedProcessesUsed": 2,
    "concurrentSessionsLimit": 10,
    "concurrentSessionsUsed": 1,
    "runtimeResourcesLimit": 5,
    "runtimeResourcesUsed": 3,
    "processAlertMachinesLimit": None,
    "processAlertMachinesUsed": 0,
}


class MockBPClient:
    """Offline BPClient: same read methods, in-memory data, no HTTP."""

    def __init__(
        self,
        resources: list[dict] | None = None,
        queues: list[dict] | None = None,
        schedules: list[dict] | None = None,
        sessions: list[dict] | None = None,
        processes: list[dict] | None = None,
        queue_items: list[dict] | None = None,
        item_data: dict[str, dict] | None = None,
        item_attempts: dict[str, list[dict]] | None = None,
        session_logs: dict[str, list[dict]] | None = None,
        limits_and_usage: dict | None = None,
        permissions: list[str] | None = None,
    ) -> None:
        self._resources = resources if resources is not None else list(_DEFAULT_RESOURCES)
        self._queues = queues if queues is not None else [dict(q) for q in _DEFAULT_QUEUES]
        self._schedules = (
            schedules if schedules is not None else [dict(s) for s in _DEFAULT_SCHEDULES]
        )
        self._sessions = sessions if sessions is not None else [dict(s) for s in _DEFAULT_SESSIONS]
        self._processes = processes if processes is not None else list(_DEFAULT_PROCESSES)
        self._queue_items = (
            queue_items if queue_items is not None else [dict(i) for i in _DEFAULT_QUEUE_ITEMS]
        )
        self._item_data = (
            item_data
            if item_data is not None
            else {k: dict(v) for k, v in _DEFAULT_ITEM_DATA.items()}
        )
        self._item_attempts = (
            item_attempts
            if item_attempts is not None
            else {k: [dict(a) for a in v] for k, v in _DEFAULT_ITEM_ATTEMPTS.items()}
        )
        self._session_logs = (
            session_logs
            if session_logs is not None
            else {k: list(v) for k, v in _DEFAULT_SESSION_LOGS.items()}
        )
        self._limits_and_usage = (
            limits_and_usage if limits_and_usage is not None else dict(_DEFAULT_LIMITS_AND_USAGE)
        )
        self._permissions = permissions if permissions is not None else list(_DEFAULT_PERMISSIONS)
        self._session_counter = 0
        # Start-up parameters applied per session id (kept out of the session
        # rows so they don't leak into list_sessions output).
        self._session_parameters: dict[str, dict] = {}

    def clear_cache(self) -> None:
        """No-op — the mock has no cache, but keeps the interface identical."""

    # --- Tier 1 reads -------------------------------------------------------

    def get_resources(self) -> list[dict]:
        return [dict(r) for r in self._resources]

    def get_queues(self) -> list[dict]:
        return [dict(q) for q in self._queues]

    def get_queue(self, queue_id: str) -> dict:
        # Strict like the live endpoint: id only (names resolve at the tool
        # layer), and an unknown id raises rather than returning None — the
        # live client surfaces a 404 HTTPError here.
        for queue in self._queues:
            if queue.get("id") == queue_id:
                return dict(queue)
        raise LookupError(f"No queue with id {queue_id!r}")

    def get_schedules(self) -> list[dict]:
        return [dict(s) for s in self._schedules]

    def get_sessions(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict]:
        sessions = self._sessions
        if start_date:
            sessions = [s for s in sessions if (s.get("startTime") or "") >= start_date]
        if end_date:
            sessions = [s for s in sessions if _at_or_before(s.get("startTime"), end_date)]
        return [dict(s) for s in sessions]

    def get_session(self, session_id: str) -> dict:
        # Single-session detail; strict like the live 404 → unknown id raises.
        session = self._find_session(session_id)
        if session is None:
            raise LookupError(f"No session with id {session_id!r}")
        return dict(session)

    def get_processes(self) -> list[dict]:
        return [dict(p) for p in self._processes]

    def get_queue_items(
        self,
        queue_id: str,
        state: str | None = None,
        status: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        items = [i for i in self._queue_items if i.get("queue") == queue_id]
        if state:
            items = [i for i in items if i.get("state") == state]
        if status:
            items = [i for i in items if i.get("status") == status]
        if start_date:
            items = [i for i in items if (i.get("lastUpdated") or "") >= start_date]
        if end_date:
            items = [i for i in items if _at_or_before(i.get("lastUpdated"), end_date)]
        return [dict(i) for i in items]

    def get_queue_item(self, item_id: str) -> dict:
        # The single-item read returns WorkQueueItem (WITH `data`); the list
        # read returns NoData. Item ids are globally unique, so this matches the
        # queue-less live path. The mock-internal `queue` key is dropped (it is
        # not an API field), and `data` always present like the live schema —
        # an empty collection when no payload fixture exists for the item.
        for item in self._queue_items:
            if item.get("id") == item_id:
                row = {k: v for k, v in item.items() if k != "queue"}
                row["data"] = dict(self._item_data.get(item_id, {"rows": []}))
                return row
        raise LookupError(f"No work queue item with id {item_id!r}")

    def get_item_attempts(self, queue_id: str, item_id: str) -> list[dict]:
        # Queue-scoped like the live path: an item not in this queue answers an
        # empty history (the live endpoint 404s on a mismatch; an empty list is
        # the benign tool-visible equivalent). Rows are WorkQueueItemNoData.
        if self._find_item(queue_id, item_id) is None:
            return []
        return [dict(a) for a in self._item_attempts.get(item_id, [])]

    def get_session_log(self, session_id: str) -> list[dict]:
        return [dict(e) for e in self._session_logs.get(session_id, [])]

    def get_current_limits_and_usage(self) -> dict:
        return dict(self._limits_and_usage)

    def get_user_permissions(self) -> list[str]:
        return list(self._permissions)

    # --- Tier 3 writes (mutate the in-memory fixtures) ----------------------
    # Return shapes mirror the live client: retry answers {"attemptId": n},
    # defer and set_schedule_enabled answer None (the API's 204/empty),
    # start_process answers the composed {"sessionId", "status"} dict.

    def retry_queue_item(self, queue_id: str, item_id: str) -> dict | None:
        item = self._find_item(queue_id, item_id)
        if item is None:
            return None
        item["state"] = "Pending"
        item["attemptNumber"] = int(item.get("attemptNumber", 1)) + 1
        return {"attemptId": item["attemptNumber"]}

    def defer_queue_item(
        self, queue_id: str, item_id: str, attempt_id: int, defer_until: str
    ) -> None:
        item = self._find_item(queue_id, item_id)
        if item is None:
            return None
        # Attempt-scoped like the live endpoint (.../attempts/{attemptId}):
        # a wrong attempt id must not mutate the item, so tests catch callers
        # passing a stale one. A fixture without attemptNumber means attempt 1
        # (the same default retry_queue_item uses); an unparsable value never
        # matches anything.
        try:
            current = int(item.get("attemptNumber", 1))
        except (TypeError, ValueError):
            return None
        if attempt_id == current:
            item["state"] = "Deferred"
            item["deferredDate"] = defer_until
        return None

    def start_process(
        self, process_id: str, resource_id: str, parameters: dict | None = None
    ) -> dict:
        self._session_counter += 1
        # Live v7 always answers a bare session UUID, and stop_session validates
        # its argument as one — so the mock mints UUID-shaped ids too (in a range
        # clear of the seeded fixtures), keeping the start_process → stop_session
        # workflow exercisable under mock run mode.
        session_id = f"e8a9d7c2-5f10-4b3e-bd64-{self._session_counter:012d}"
        if parameters:
            self._session_parameters[session_id] = parameters
        self._sessions.append(
            {
                "sessionId": session_id,
                "sessionNumber": len(self._sessions) + 1,
                "processId": process_id,
                "processName": process_id,
                "resourceId": resource_id,
                "resourceName": resource_id,
                "status": "Running",
                "startTime": "",
                "endTime": None,
                "terminationReason": "None",
                "exceptionType": None,
                "exceptionMessage": None,
            }
        )
        return {"sessionId": session_id, "status": "Running"}

    def stop_session(self, session_id: str) -> dict:
        session = self._find_session(session_id)
        if session is not None:
            session["status"] = "Stopped"
        return {"sessionId": session_id, "status": "Stopped"}

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> None:
        schedule = self._find_schedule(schedule_id)
        if schedule is not None:
            schedule["isRetired"] = not enabled
        return None

    def trigger_schedule(self, schedule_id: str, start_time: str | None = None) -> dict | None:
        schedule = self._find_schedule(schedule_id)
        if schedule is None:
            return None
        schedule["lastOutcome"] = "Triggered"
        return {"schedule": schedule_id, "status": "Triggered"}

    def stop_schedule(self, schedule_id: str) -> None:
        # Cancels active runs; the live endpoint answers 202 with no body, so
        # the mock returns None too. Records the outcome on the fixture so a
        # test can observe the effect after the write.
        schedule = self._find_schedule(schedule_id)
        if schedule is not None:
            schedule["lastOutcome"] = "Stopped"
        return None

    # --- Lookup helpers -----------------------------------------------------

    def _find_item(self, queue_id: str, item_id: str) -> dict | None:
        for item in self._queue_items:
            if item.get("queue") == queue_id and item.get("id") == item_id:
                return item
        return None

    def _find_session(self, session_id: str) -> dict | None:
        for session in self._sessions:
            if session.get("sessionId") == session_id:
                return session
        return None

    def _find_schedule(self, schedule_id) -> dict | None:
        # Schedule ids are integers (the one non-UUID id in the API), but the
        # live client takes schedule_id as a str and interpolates it into the
        # URL — so callers naturally pass "1". Compare ids as strings so the
        # mock doesn't silently no-op on a type mismatch; name matches stay
        # exact.
        for schedule in self._schedules:
            if str(schedule.get("id")) == str(schedule_id) or schedule.get("name") == schedule_id:
                return schedule
        return None


def _at_or_before(timestamp: str | None, bound: str) -> bool:
    """True when an ISO timestamp falls at or before an end bound.

    A date-only bound must include the whole day ("2026-03-02T10:00" is within
    end_date "2026-03-02"), but lexicographically "2026-03-02T10:00" >
    "2026-03-02" — so compare only the prefix the bound actually specifies.
    The live API applies the same day-inclusive semantics server-side.
    """
    return (timestamp or "")[: len(bound)] <= bound

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

from datetime import datetime, time, timedelta, timezone

# Fixtures are anchored to a "now" captured once at import, so the mock estate
# always reads as the current day/week rather than drifting stale against a
# hardcoded calendar date. The helpers build the ISO-8601 "...Z" timestamps the
# v7 schemas use, offset back (or forward, for an ETA) from this anchor. Tests
# that assert against the defaults import these same helpers, so they stay green
# as time passes instead of pinning literals.
_NOW = datetime.now(timezone.utc)
_TODAY = _NOW.date()


def _ts(days_ago: int, hhmmss: str = "09:00:00") -> str:
    """An ISO-8601 ``...Z`` timestamp ``days_ago`` before today at ``hhmmss``."""
    return f"{(_TODAY - timedelta(days=days_ago)).isoformat()}T{hhmmss}Z"


def _date(days_ago: int) -> str:
    """A bare ISO date ``days_ago`` before today (negative = in the future)."""
    return (_TODAY - timedelta(days=days_ago)).isoformat()


def _recent(minutes_ago: int) -> str:
    """An ISO-8601 ``...Z`` timestamp ``minutes_ago`` before the captured now.

    For sessions that should read as genuinely in-flight or just-finished:
    anchoring to the wall-clock *now* (not a fixed time of day like ``_ts``)
    keeps them in the past whatever hour the process starts — a fixed
    ``HH:MM`` would read as the future when the process boots earlier in the
    UTC day than that time.
    """
    return (_NOW - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


# The exception-handling stage types the errors-only session-log filter keeps
# (mirrors the live client's _ERROR_STAGE_TYPES, held here so the mock stays
# import-free of the client).
_ERROR_STAGE_TYPES = frozenset({"Exception", "Recover", "Resume"})

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
        # No items locked: a backlog with nothing in progress — the stalled
        # case the L2 severity scorer flags (a Running queue with no resource
        # draining it), as distinct from a deep-but-flowing backlog.
        "lockedItemCount": 0,
        "exceptionedItemCount": 5,
        "totalItemCount": 357,
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
        "startTime": _ts(8),
        "endTime": _ts(8, "09:09:00"),
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
        "startTime": _ts(7, "10:00:00"),
        "endTime": _ts(7, "10:01:35"),
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
        "startTime": _ts(4),
        "endTime": _ts(4, "09:14:40"),
        "terminationReason": "None",
        "exceptionType": None,
        "exceptionMessage": None,
    },
    {
        # An in-flight run: a mock estate is never all-finished, and the live-session
        # reads (worker current_sessions, in-flight severity) and the stop_session
        # workflow need a Running target to exercise. No endTime — it is still going.
        "sessionId": "e8a9d7c2-5f10-4b3e-bd64-000000000304",
        "sessionNumber": 4,
        "processId": _PROC_ONBOARDING,
        "processName": "Customer Onboarding",
        "resourceId": "5d2c8e0a-71b4-4a8e-9f30-000000000002",
        "resourceName": "BOT-02",
        "status": "Running",
        "startTime": _ts(0, "08:30:00"),
        "endTime": None,
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
        "lastUpdated": _ts(8, "09:05:00"),
        "completedDate": _ts(8, "09:05:00"),
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
        "lastUpdated": _ts(7, "11:20:00"),
        "exceptionedDate": _ts(7, "11:20:00"),
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
        "lastUpdated": _ts(6, "08:00:00"),
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
            "lastUpdated": _ts(7, "11:20:00"),
            "exceptionedDate": _ts(7, "11:20:00"),
            "workTimeInSeconds": 40,
            "exceptionReason": "Invoice total did not match PO; query raised by 07700 900123",
            "resource": "BOT-01",
        },
        {
            "id": _ITEM_INVOICE_EXCEPTION,
            "state": "Pending",
            "keyValue": "INV-1002",
            "attemptNumber": 2,
            "lastUpdated": _ts(7, "12:00:00"),
            "workTimeInSeconds": 0,
            "exceptionReason": None,
            "resource": None,
        },
    ],
}

# SessionLogSummary rows. stageType drives the errors-only filter (the
# exception-handling stages Exception/Recover/Resume) and resourceStartTime is
# the per-stage execution timestamp the time-window filter bounds.
_DEFAULT_SESSION_LOGS: dict[str, list[dict]] = {
    "e8a9d7c2-5f10-4b3e-bd64-000000000301": [
        {
            "logNumber": 1,
            "stageName": "Start",
            "stageType": "Start",
            "result": "",
            "resourceStartTime": _ts(8, "09:00:00"),
        },
        {
            "logNumber": 2,
            "stageName": "Read Invoice",
            "stageType": "Action",
            "result": "OK",
            "resourceStartTime": _ts(8, "09:04:00"),
        },
        {
            "logNumber": 3,
            "stageName": "Post to Ledger",
            "stageType": "Action",
            "result": "OK",
            "resourceStartTime": _ts(8, "09:08:30"),
        },
    ],
    "e8a9d7c2-5f10-4b3e-bd64-000000000302": [
        {
            "logNumber": 1,
            "stageName": "Start",
            "stageType": "Start",
            "result": "",
            "resourceStartTime": _ts(7, "10:00:00"),
        },
        {
            "logNumber": 2,
            "stageName": "Validate Customer",
            "stageType": "Action",
            "result": "Lookup returned no match",
            "resultType": "Text",
            "resourceStartTime": _ts(7, "10:00:35"),
        },
        {
            "logNumber": 3,
            "stageName": "Raise Not Found",
            "stageType": "Exception",
            "result": "Customer record not found for ref 4929 1234 5678 9012",
            "resultType": "Text",
            "resourceStartTime": _ts(7, "10:01:05"),
        },
        {
            "logNumber": 4,
            "stageName": "Handle Exception",
            "stageType": "Recover",
            "result": "",
            "resourceStartTime": _ts(7, "10:01:20"),
        },
    ],
}

# ScheduleLogSummary rows keyed by schedule id (an integer in the API). The
# newest run (by startTime) is a schedule's last outcome; list_schedules folds
# it in. The active schedule has a clean recent run; the retired one's last run
# terminated. A schedule absent here (or with an empty list) has never run.
_DEFAULT_SCHEDULE_LOGS: dict[str, list[dict]] = {
    "1": [
        {
            "scheduleLogId": 11,
            "scheduleId": 1,
            "scheduleName": "Daily Invoice Run",
            "startTime": _ts(1, "06:00:00"),
            "endTime": _ts(1, "06:12:40"),
            "duration": "00:12:40",
            "status": "completed",
            "serverName": "BP-APP-01",
        },
        {
            "scheduleLogId": 9,
            "scheduleId": 1,
            "scheduleName": "Daily Invoice Run",
            "startTime": _ts(4, "06:00:00"),
            "endTime": _ts(4, "06:11:02"),
            "duration": "00:11:02",
            "status": "completed",
            "serverName": "BP-APP-01",
        },
    ],
    "2": [
        {
            "scheduleLogId": 4,
            "scheduleId": 2,
            "scheduleName": "Weekly Reconciliation",
            "startTime": _ts(20, "07:00:00"),
            "endTime": _ts(20, "07:03:18"),
            "duration": "00:03:18",
            "status": "terminated",
            "serverName": "BP-APP-02",
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

_DEFAULT_LICENSE_ENTITLEMENT: dict = {
    "activeLicenseTypes": ["Enterprise"],
    "enterpriseEntitlement": {
        "publishedprocesseslimit": 0,
        "concurrentsessionslimit": 10,
        "runtimeresourceslimit": 5,
        "processalertmachineslimit": 0,
    },
    "desktopEntitlement": {
        "publishedprocesseslimit": 0,
        "concurrentsessionslimit": 0,
        "runtimeresourceslimit": 0,
        "processalertmachineslimit": 0,
    },
}

# Per-queue deferred counts the workQueueCompositions aggregate adds on top of
# WorkQueueSummary (keyed by queue id; queues without an entry report deferred 0).
_DEFAULT_DEFERRED_BY_QUEUE: dict[str, int] = {
    "9b6f3a1c-2e45-4d07-8c11-000000000101": 3,  # Invoices
}

_RESOURCE_GROUP_PROD = "1f8b6c4d-0a23-4e91-9d77-000000000401"

# WorkQueueConfigurationSummary rows: the ACTIVE queues only, each linking a
# queue to its assigned process and resource group, plus live activity stats.
_DEFAULT_QUEUE_CONFIGURATIONS: list[dict] = [
    {
        "id": "9b6f3a1c-2e45-4d07-8c11-000000000101",  # Invoices
        "name": "Invoices",
        "activeWorkQueueConfiguration": {
            "assignedProcessId": _PROC_INVOICES,
            "assignedResourceGroupId": _RESOURCE_GROUP_PROD,
        },
        "activeQueueStats": {
            "activeSessions": 1,
            "availableResources": 2,
            "timeRemaining": "00:16:48",
            "elapsedRemaining": "00:02:00",
            "ETA": _ts(0, "23:30:00"),
        },
    },
]

# ResourcePool rows — a BARE array on the live endpoint (no paging envelope).
_DEFAULT_RESOURCE_POOLS: list[dict] = [
    {
        "id": "3a5e7c9d-1b46-4f82-8e10-000000000501",
        "name": "Production Pool",
        "members": 2,
        "databaseStatus": "Ready",
    },
]

# EnvironmentVariable rows — value is a typed configuration payload scrubbed
# type-aware at the tool boundary: the Text value carries PII to exercise the
# scrubber, the Password value is redacted wholesale, the Number scalar passes
# through untouched.
_DEFAULT_ENVIRONMENT_VARIABLES: list[dict] = [
    {
        "id": "6d8f0a2c-3e57-4912-bd83-000000000601",
        "name": "Finance Mailbox",
        "description": "Inbox the invoice bot reads from",
        "dataType": "Text",
        "value": "ap-team@contoso.example and 07700 900123",
    },
    {
        "id": "6d8f0a2c-3e57-4912-bd83-000000000602",
        "name": "Ledger API Key",
        "description": "Credential for the ledger posting API",
        "dataType": "Password",
        "value": "s3cr3t-token-value",
    },
    {
        "id": "6d8f0a2c-3e57-4912-bd83-000000000603",
        "name": "Retry Limit",
        "description": "Maximum retries before manual review",
        "dataType": "Number",
        "value": 3,
    },
]

def _shift_usages(active_hours: range, minutes_per_hour: int = 55) -> list[int]:
    """A 24-int usages row: `minutes_per_hour` worked in each of `active_hours`, 0 elsewhere."""
    return [minutes_per_hour if h in active_hours else 0 for h in range(24)]


def _heat_row(resource_id: str, name: str, days_ago: int, usages: list[int]) -> dict:
    """One resourceUtilization row: a worker's heat-map for a single day."""
    return {
        "resourceId": resource_id,
        "digitalWorkerName": name,
        "utilizationDate": _date(days_ago),
        "usages": usages,
    }


# ResourceUtilization rows — one per worker per day, 24 ints of minutes worked
# per hour. BOT-03 (offline) has no rows at all: the raw feed only reports
# workers it actually has data for, so a worker absent from the estate's
# activity is absent here too, not zero-filled.
_DEFAULT_RESOURCE_UTILIZATION: list[dict] = [
    _heat_row("5d2c8e0a-71b4-4a8e-9f30-000000000001", "BOT-01", 2, _shift_usages(range(8, 17))),
    _heat_row("5d2c8e0a-71b4-4a8e-9f30-000000000001", "BOT-01", 1, _shift_usages(range(8, 17))),
    _heat_row("5d2c8e0a-71b4-4a8e-9f30-000000000001", "BOT-01", 0, _shift_usages(range(8, 12))),
    _heat_row(
        "5d2c8e0a-71b4-4a8e-9f30-000000000002", "BOT-02", 2, _shift_usages(range(9, 18), 40)
    ),
    _heat_row(
        "5d2c8e0a-71b4-4a8e-9f30-000000000002", "BOT-02", 1, _shift_usages(range(9, 18), 40)
    ),
]

# ProcessGroupItem rows — the flat descendant list of the process tree: a
# folder (Group) and the published processes (Items) within it.
_DEFAULT_PROCESS_GROUPS: list[dict] = [
    {
        "id": "8f0a2c4e-5b69-4d31-a2c5-000000000701",
        "name": "Finance",
        "nodeType": "Group",
        "lastModified": "0001-01-01T00:00:00Z",
    },
    {
        "id": _PROC_INVOICES,
        "name": "Invoice Processing",
        "nodeType": "Item",
        "lastModified": _ts(30, "09:05:10"),
    },
    {
        "id": _PROC_ONBOARDING,
        "name": "Customer Onboarding",
        "nodeType": "Item",
        "lastModified": _ts(90, "14:22:00"),
    },
]


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
        schedule_logs: dict[str, list[dict]] | None = None,
        limits_and_usage: dict | None = None,
        license_entitlement: dict | None = None,
        deferred_by_queue: dict[str, int] | None = None,
        permissions: list[str] | None = None,
        queue_configurations: list[dict] | None = None,
        resource_pools: list[dict] | None = None,
        environment_variables: list[dict] | None = None,
        process_groups: list[dict] | None = None,
        resource_utilization: list[dict] | None = None,
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
            else {k: [dict(e) for e in v] for k, v in _DEFAULT_SESSION_LOGS.items()}
        )
        self._schedule_logs = (
            schedule_logs
            if schedule_logs is not None
            else {k: [dict(r) for r in v] for k, v in _DEFAULT_SCHEDULE_LOGS.items()}
        )
        self._limits_and_usage = (
            limits_and_usage if limits_and_usage is not None else dict(_DEFAULT_LIMITS_AND_USAGE)
        )
        self._license_entitlement = (
            license_entitlement
            if license_entitlement is not None
            else dict(_DEFAULT_LICENSE_ENTITLEMENT)
        )
        self._deferred_by_queue = (
            deferred_by_queue if deferred_by_queue is not None else dict(_DEFAULT_DEFERRED_BY_QUEUE)
        )
        self._permissions = permissions if permissions is not None else list(_DEFAULT_PERMISSIONS)
        self._queue_configurations = (
            queue_configurations
            if queue_configurations is not None
            else [dict(c) for c in _DEFAULT_QUEUE_CONFIGURATIONS]
        )
        self._resource_pools = (
            resource_pools
            if resource_pools is not None
            else [dict(p) for p in _DEFAULT_RESOURCE_POOLS]
        )
        self._environment_variables = (
            environment_variables
            if environment_variables is not None
            else [dict(v) for v in _DEFAULT_ENVIRONMENT_VARIABLES]
        )
        self._process_groups = (
            process_groups
            if process_groups is not None
            else [dict(g) for g in _DEFAULT_PROCESS_GROUPS]
        )
        self._resource_utilization = (
            resource_utilization
            if resource_utilization is not None
            else [dict(r) for r in _DEFAULT_RESOURCE_UTILIZATION]
        )
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

    def get_session_log(
        self,
        session_id: str,
        errors_only: bool = False,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        # Mirror the live server-side filters: errors_only narrows to the
        # exception-handling stage types, the window bounds resourceStartTime,
        # and the pages come back newest-stage-first (sortBy=LogNumberDesc).
        entries = self._session_logs.get(session_id, [])
        if errors_only:
            entries = [e for e in entries if e.get("stageType") in _ERROR_STAGE_TYPES]
        if start_date:
            entries = [e for e in entries if (e.get("resourceStartTime") or "") >= start_date]
        if end_date:
            entries = [e for e in entries if _at_or_before(e.get("resourceStartTime"), end_date)]
        entries = sorted(entries, key=lambda e: e.get("logNumber") or 0, reverse=True)
        return [dict(e) for e in entries]

    def get_last_schedule_run(self, schedule_id) -> dict | None:
        # The most recent run (by startTime) for the schedule, or None when it
        # has never run — mirrors /scheduleLogs/{id}?sortBy=StartTimeDesc capped
        # to one row. Keyed by id as a string (schedule ids are integers).
        runs = self._schedule_logs.get(str(schedule_id), [])
        if not runs:
            return None
        latest = max(runs, key=lambda r: r.get("startTime") or "")
        return dict(latest)

    def get_current_limits_and_usage(self) -> dict:
        return dict(self._limits_and_usage)

    def get_license_entitlement(self) -> dict:
        return dict(self._license_entitlement)

    def get_queue_compositions(self, queue_ids: list[str]) -> list[dict]:
        # Mirror the live aggregate: one WorkQueueComposition per requested id
        # that exists, carrying the per-state counts (deferred is the datum the
        # WorkQueueSummary row lacks). Unknown ids are skipped, like the live API.
        rows = []
        for queue in self._queues:
            qid = queue.get("id")
            if qid not in queue_ids:
                continue
            rows.append(
                {
                    "id": qid,
                    "name": queue.get("name"),
                    "completed": queue.get("completedItemCount", 0),
                    "pending": queue.get("pendingItemCount", 0),
                    "deferred": self._deferred_by_queue.get(qid, 0),
                    "locked": queue.get("lockedItemCount", 0),
                    "exceptioned": queue.get("exceptionedItemCount", 0),
                }
            )
        return rows

    def get_queue_configurations(self) -> list[dict]:
        return [dict(c) for c in self._queue_configurations]

    def get_resource_pools(self) -> list[dict]:
        return [dict(p) for p in self._resource_pools]

    def get_environment_variables(self) -> list[dict]:
        return [dict(v) for v in self._environment_variables]

    def get_process_groups(self) -> list[dict]:
        return [dict(g) for g in self._process_groups]

    def get_user_permissions(self) -> list[str]:
        return list(self._permissions)

    def get_resource_utilization(self, start_date: str) -> list[dict]:
        # Mirrors the live endpoint's one param: rows from start_date onward,
        # no end bound (the tool layer filters down to its window).
        return [
            dict(r)
            for r in self._resource_utilization
            if (r.get("utilizationDate") or "") >= start_date
        ]

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


# --- Demo estate -----------------------------------------------------------
# A larger, relative-dated estate for end-to-end evaluation in an MCP client or
# a downstream console: pooled workers across departments, queues in varied
# health (an SLA-breaching one, a degrading one, a paused one, plus a healthy
# bulk), in-flight and stale sessions, and a failed schedule. Generic v7-shaped
# data only — no product opinion. The lean _DEFAULT_* fixtures stay the minimal,
# stable substrate the unit tests assert against; this is the populated estate.

# Pooled worker ids (the "...d000N" tail marks them clear of the lean fixtures).
_D_BOT_F01 = "5d2c8e0a-71b4-4a8e-9f30-0000000d0001"
_D_BOT_F02 = "5d2c8e0a-71b4-4a8e-9f30-0000000d0002"
_D_BOT_F03 = "5d2c8e0a-71b4-4a8e-9f30-0000000d0003"
_D_BOT_H01 = "5d2c8e0a-71b4-4a8e-9f30-0000000d0004"
_D_BOT_H02 = "5d2c8e0a-71b4-4a8e-9f30-0000000d0005"
_D_BOT_O01 = "5d2c8e0a-71b4-4a8e-9f30-0000000d0006"
_D_BOT_O02 = "5d2c8e0a-71b4-4a8e-9f30-0000000d0007"
_D_BOT_O03 = "5d2c8e0a-71b4-4a8e-9f30-0000000d0008"

# Process ids (the two lean ones are reused; three are net-new to the demo).
_D_PROC_PAYMENTS = "7c0e4f2b-93d1-4b66-a2af-0000000d0203"
_D_PROC_PAYROLL = "7c0e4f2b-93d1-4b66-a2af-0000000d0204"
_D_PROC_COMPLIANCE = "7c0e4f2b-93d1-4b66-a2af-0000000d0205"

# Queue ids (Invoices/Onboarding reuse the lean ids; the rest are demo-only).
_D_QUEUE_PAYMENTS = "9b6f3a1c-2e45-4d07-8c11-0000000d0103"
_D_QUEUE_PAYROLL = "9b6f3a1c-2e45-4d07-8c11-0000000d0104"
_D_QUEUE_EXPENSES = "9b6f3a1c-2e45-4d07-8c11-0000000d0105"
_D_QUEUE_VENDOR = "9b6f3a1c-2e45-4d07-8c11-0000000d0106"
_D_QUEUE_COMPLIANCE = "9b6f3a1c-2e45-4d07-8c11-0000000d0107"
_D_QUEUE_MAILROOM = "9b6f3a1c-2e45-4d07-8c11-0000000d0108"
_D_QUEUE_CLOSURES = "9b6f3a1c-2e45-4d07-8c11-0000000d0109"


def _worker(
    resource_id: str,
    name: str,
    pool: str,
    group: str,
    display_status: str,
    *,
    active: int = 0,
    warning: int = 0,
    pending: int = 0,
    database_status: str = "Ready",
) -> dict:
    return {
        "id": resource_id,
        "name": name,
        "poolName": pool,
        "groupName": group,
        "attributes": [],
        "activeSessionCount": active,
        "warningSessionCount": warning,
        "pendingSessionCount": pending,
        "databaseStatus": database_status,
        "displayStatus": display_status,
        "resourceType": "Enterprise",
    }


def _queue(
    queue_id: str,
    name: str,
    group: str,
    status: str,
    *,
    pending: int,
    completed: int,
    locked: int,
    exceptioned: int,
    average: str,
    key_field: str = "Item Key",
) -> dict:
    return {
        "id": queue_id,
        "name": name,
        "keyField": key_field,
        "status": status,
        "isEncrypted": False,
        "maxAttempts": 3,
        "pendingItemCount": pending,
        "completedItemCount": completed,
        "lockedItemCount": locked,
        "exceptionedItemCount": exceptioned,
        "totalItemCount": pending + completed + locked + exceptioned,
        "averageWorkTime": average,
        "groupName": group,
    }


def _session(
    session_id: str,
    number: int,
    process_id: str,
    process_name: str,
    resource_id: str,
    resource_name: str,
    status: str,
    start: str,
    end: str | None,
    *,
    termination: str = "None",
    exception_type: str | None = None,
    exception_message: str | None = None,
) -> dict:
    return {
        "sessionId": session_id,
        "sessionNumber": number,
        "processId": process_id,
        "processName": process_name,
        "resourceId": resource_id,
        "resourceName": resource_name,
        "status": status,
        "startTime": start,
        "endTime": end,
        "terminationReason": termination,
        "exceptionType": exception_type,
        "exceptionMessage": exception_message,
    }


# The published process / worker pairs the historical backlog cycles through.
# Every processName matches the demo catalogue so throughput_summary buckets the
# generated runs and the downstream daily zero-fill lines up with a real process.
_DEMO_HISTORY_PROCESSES = [
    (_PROC_INVOICES, "Invoice Processing", _D_BOT_F01, "BOT-F01"),
    (_D_PROC_PAYMENTS, "Payment Run", _D_BOT_F02, "BOT-F02"),
    (_D_PROC_PAYROLL, "Payroll Run", _D_BOT_H01, "BOT-H01"),
    (_PROC_ONBOARDING, "Customer Onboarding", _D_BOT_O01, "BOT-O01"),
    (_D_PROC_COMPLIANCE, "Compliance Screening", _D_BOT_O02, "BOT-O02"),
]

# How many complete days of finished-session history the demo backlog spans. The
# Intelligence quarter view reads 90 days AND its prior-period delta reads the 90
# before that, so the backlog covers both off real data rather than a flat zero
# series. Anything dated within this window still reads as legitimate recent
# operational history, not a stale calendar month.
_DEMO_HISTORY_DAYS = 180


def _demo_history() -> list[dict]:
    """A deterministic ~180-day backlog of finished sessions for the demo estate.

    Volume and outcomes vary by day so the throughput chart has shape and the
    STP-rate KPI moves period to period: weekdays are busier than weekends, and a
    small termination fraction worsens over the most recent fortnight (a degrading
    signal the deltas should pick up). Every session is past-dated (the foreground
    list owns today) and references this estate's own processes and workers. A
    pure function of the day offset — no RNG — so get_sessions() is reproducible
    run to run and the tests can assert against the shape.
    """
    sessions: list[dict] = []
    number = 100  # clear of the dozen explicit foreground sessions (1..12)
    for days_ago in range(1, _DEMO_HISTORY_DAYS + 1):
        day = _TODAY - timedelta(days=days_ago)
        weekend = day.weekday() >= 5
        # A weekday base with a gentle reproducible wave; a much lighter weekend.
        volume = (6 if weekend else 20) + (days_ago * 3) % 7
        # ~1 in 8 runs terminates over the most recent fortnight, ~1 in 16 before
        # — so the recent STP rate reads materially worse than the prior period.
        term_every = 8 if days_ago <= 14 else 16
        for k in range(volume):
            proc_id, proc_name, res_id, res_name = _DEMO_HISTORY_PROCESSES[
                (days_ago + k) % len(_DEMO_HISTORY_PROCESSES)
            ]
            terminated = k % term_every == 0
            # Spread the runs across the working day; both ends are >= a day ago,
            # so any time of day stays safely in the past.
            start_dt = datetime.combine(day, time(6 + k % 12, k * 7 % 60), tzinfo=timezone.utc)
            end_dt = start_dt + timedelta(minutes=4 if terminated else 11)
            stamp = "%Y-%m-%dT%H:%M:%SZ"
            # Terminated runs land on k that is a multiple of term_every (always
            # even), so vary the reason by the day, not k, to get a real mix of
            # process vs internal errors across the backlog.
            outcome = (
                {
                    "termination": "InternalError" if days_ago % 2 else "ProcessError",
                    "exception_type": "System Exception",
                    "exception_message": f"{proc_name} run failed",
                }
                if terminated
                else {}
            )
            number += 1
            sessions.append(
                _session(
                    f"e8a9d7c2-5f10-4b3e-bd64-{number:012d}",
                    number,
                    proc_id,
                    proc_name,
                    res_id,
                    res_name,
                    "Terminated" if terminated else "Completed",
                    start_dt.strftime(stamp),
                    end_dt.strftime(stamp),
                    **outcome,
                )
            )
    return sessions


def demo_estate() -> MockBPClient:
    """A populated, relative-dated MockBPClient for end-to-end evaluation."""
    resources = [
        _worker(_D_BOT_F01, "BOT-F01", "Finance Pool", "Finance", "Working", active=1),
        _worker(_D_BOT_F02, "BOT-F02", "Finance Pool", "Finance", "Idle"),
        _worker(_D_BOT_F03, "BOT-F03", "Finance Pool", "Finance", "Working", active=1, warning=1),
        _worker(_D_BOT_H01, "BOT-H01", "HR Pool", "HR", "Idle"),
        _worker(_D_BOT_H02, "BOT-H02", "HR Pool", "HR", "Offline", database_status="Offline"),
        _worker(_D_BOT_O01, "BOT-O01", "Onboarding Pool", "Onboarding", "Working", active=1),
        _worker(_D_BOT_O02, "BOT-O02", "Onboarding Pool", "Onboarding", "Idle", pending=1),
        _worker(
            _D_BOT_O03,
            "BOT-O03",
            "Onboarding Pool",
            "Onboarding",
            "Offline",
            database_status="Offline",
        ),
    ]

    resource_pools = [
        {
            "id": "3a5e7c9d-1b46-4f82-8e10-0000000d0501",
            "name": "Finance Pool",
            "members": 3,
            "databaseStatus": "Ready",
        },
        {
            "id": "3a5e7c9d-1b46-4f82-8e10-0000000d0502",
            "name": "HR Pool",
            "members": 2,
            "databaseStatus": "Ready",
        },
        {
            "id": "3a5e7c9d-1b46-4f82-8e10-0000000d0503",
            "name": "Onboarding Pool",
            "members": 3,
            "databaseStatus": "Ready",
        },
    ]

    queues = [
        # Loaded but flowing: a deep backlog actively being drained (items
        # locked, a resource working them). Routine load, not a problem — the
        # severity scorer must read this as ok however deep the backlog.
        _queue(
            _QUEUE_INVOICES,
            "Invoices",
            "Finance",
            "Running",
            pending=120,
            completed=812,
            locked=3,
            exceptioned=47,
            average="00:02:10",
            key_field="Invoice Number",
        ),
        # Stalled: a Running queue holding a heavy backlog with NOTHING in
        # progress (no items locked) — no resource is draining it. The genuine
        # stuck case the scorer flags critical, distinct from Invoices' flow.
        _queue(
            _D_QUEUE_PAYMENTS,
            "Payments",
            "Finance",
            "Running",
            pending=64,
            completed=540,
            locked=0,
            exceptioned=18,
            average="00:01:48",
            key_field="Payment Ref",
        ),
        # Paused: work held, a small backlog waiting.
        _queue(
            _QUEUE_ONBOARDING,
            "Onboarding",
            "Operations",
            "Paused",
            pending=35,
            completed=214,
            locked=0,
            exceptioned=4,
            average="00:03:02",
            key_field="Customer Id",
        ),
        # The healthy bulk — no exceptions — so a console's collapsed-healthy
        # summary has a real count to fold away.
        _queue(
            _D_QUEUE_PAYROLL,
            "Payroll",
            "HR",
            "Running",
            pending=4,
            completed=320,
            locked=0,
            exceptioned=0,
            average="00:04:20",
        ),
        _queue(
            _D_QUEUE_EXPENSES,
            "Expenses",
            "Finance",
            "Running",
            pending=9,
            completed=410,
            locked=1,
            exceptioned=0,
            average="00:01:05",
        ),
        _queue(
            _D_QUEUE_VENDOR,
            "Vendor Setup",
            "Operations",
            "Running",
            pending=2,
            completed=95,
            locked=0,
            exceptioned=0,
            average="00:05:40",
        ),
        _queue(
            _D_QUEUE_COMPLIANCE,
            "Compliance Checks",
            "Operations",
            "Running",
            pending=0,
            completed=150,
            locked=0,
            exceptioned=0,
            average="00:02:30",
        ),
        _queue(
            _D_QUEUE_MAILROOM,
            "Mailroom",
            "Operations",
            "Running",
            pending=7,
            completed=260,
            locked=0,
            exceptioned=0,
            average="00:00:45",
        ),
        _queue(
            _D_QUEUE_CLOSURES,
            "Account Closures",
            "Operations",
            "Running",
            pending=1,
            completed=73,
            locked=0,
            exceptioned=0,
            average="00:06:10",
        ),
    ]

    processes = [
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
        {
            "processId": _D_PROC_PAYMENTS,
            "processName": "Payment Run",
            "processDescription": "Outbound BACS payment file generation",
            "groupName": "Finance",
            "attributes": ["Published"],
        },
        {
            "processId": _D_PROC_PAYROLL,
            "processName": "Payroll Run",
            "processDescription": "Monthly payroll calculation and posting",
            "groupName": "HR",
            "attributes": ["Published"],
        },
        {
            "processId": _D_PROC_COMPLIANCE,
            "processName": "Compliance Screening",
            "processDescription": "Sanctions and PEP screening",
            "groupName": "Operations",
            "attributes": ["Published"],
        },
    ]

    sessions = [
        # Today's completed runs — anchored to wall-clock now so they always read
        # as a few hours back, never the future when the process boots early.
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0301",
            1,
            _PROC_INVOICES,
            "Invoice Processing",
            _D_BOT_F01,
            "BOT-F01",
            "Completed",
            _recent(360),
            _recent(352),
        ),
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0302",
            2,
            _PROC_INVOICES,
            "Invoice Processing",
            _D_BOT_F01,
            "BOT-F01",
            "Completed",
            _recent(300),
            _recent(292),
        ),
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0303",
            3,
            _PROC_ONBOARDING,
            "Customer Onboarding",
            _D_BOT_O01,
            "BOT-O01",
            "Completed",
            _ts(1, "11:00:00"),
            _ts(1, "11:09:00"),
        ),
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0304",
            4,
            _D_PROC_PAYROLL,
            "Payroll Run",
            _D_BOT_H01,
            "BOT-H01",
            "Completed",
            _ts(2, "07:00:00"),
            _ts(2, "07:22:00"),
        ),
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0305",
            5,
            _D_PROC_COMPLIANCE,
            "Compliance Screening",
            _D_BOT_O01,
            "BOT-O01",
            "Completed",
            _ts(3, "13:00:00"),
            _ts(3, "13:04:00"),
        ),
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0306",
            6,
            _PROC_INVOICES,
            "Invoice Processing",
            _D_BOT_F01,
            "BOT-F01",
            "Completed",
            _ts(4, "09:00:00"),
            _ts(4, "09:11:00"),
        ),
        # In-flight runs on the working bots (started a couple of hours ago).
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0307",
            7,
            _PROC_INVOICES,
            "Invoice Processing",
            _D_BOT_F03,
            "BOT-F03",
            "Running",
            _recent(150),
            None,
        ),
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0308",
            8,
            _PROC_ONBOARDING,
            "Customer Onboarding",
            _D_BOT_O01,
            "BOT-O01",
            "Running",
            _recent(90),
            None,
        ),
        # The degrading Payment Run: two recent terminations.
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0309",
            9,
            _D_PROC_PAYMENTS,
            "Payment Run",
            _D_BOT_F02,
            "BOT-F02",
            "Terminated",
            _recent(200),
            _recent(198),
            termination="ProcessError",
            exception_type="System Exception",
            exception_message="BACS gateway timed out after 30s",
        ),
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0310",
            10,
            _D_PROC_PAYMENTS,
            "Payment Run",
            _D_BOT_F01,
            "BOT-F01",
            "Terminated",
            _ts(1, "14:00:00"),
            _ts(1, "14:01:05"),
            termination="ProcessError",
            exception_type="System Exception",
            exception_message="BACS gateway timed out after 30s",
        ),
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0311",
            11,
            _PROC_ONBOARDING,
            "Customer Onboarding",
            _D_BOT_O01,
            "BOT-O01",
            "Terminated",
            _ts(2, "10:00:00"),
            _ts(2, "10:00:50"),
            termination="ProcessError",
            exception_type="Business Exception",
            exception_message="Customer record not found",
        ),
        # A stale Running session on a bot that reads Idle — silently stuck for
        # days, the in-flight-severity case a console must surface.
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0312",
            12,
            _PROC_INVOICES,
            "Invoice Processing",
            _D_BOT_F02,
            "BOT-F02",
            "Running",
            _ts(5, "16:00:00"),
            None,
        ),
        # The ~180-day finished-session backlog behind the foreground twelve: the
        # volume substrate the throughput history and STP trend read off.
        *_demo_history(),
    ]

    schedules = [
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
            "name": "Nightly Payment Run",
            "description": "Generates the outbound BACS file overnight",
            "isRetired": False,
            "tasksCount": 1,
            "intervalType": "Day",
            "calendarName": "Working Week",
        },
        {
            "id": 3,
            "name": "Weekly Reconciliation",
            "description": "Legacy reconciliation run",
            "isRetired": True,
            "tasksCount": 2,
            "intervalType": "Week",
            "calendarName": "Working Week",
        },
        {
            "id": 4,
            "name": "Hourly Compliance Sync",
            "description": "Keeps the sanctions list current",
            "isRetired": False,
            "tasksCount": 1,
            "intervalType": "Hour",
            "calendarName": "All Days",
        },
    ]

    schedule_logs = {
        "1": [
            {
                "scheduleLogId": 31,
                "scheduleId": 1,
                "scheduleName": "Daily Invoice Run",
                "startTime": _ts(0, "06:00:00"),
                "endTime": _ts(0, "06:12:40"),
                "duration": "00:12:40",
                "status": "completed",
                "serverName": "BP-APP-01",
            },
        ],
        # The failed schedule: its most recent run terminated.
        "2": [
            {
                "scheduleLogId": 32,
                "scheduleId": 2,
                "scheduleName": "Nightly Payment Run",
                "startTime": _ts(0, "02:00:00"),
                "endTime": _ts(0, "02:00:45"),
                "duration": "00:00:45",
                "status": "terminated",
                "serverName": "BP-APP-02",
            },
        ],
        "4": [
            {
                "scheduleLogId": 33,
                "scheduleId": 4,
                "scheduleName": "Hourly Compliance Sync",
                "startTime": _ts(0, "08:00:00"),
                "endTime": _ts(0, "08:01:10"),
                "duration": "00:01:10",
                "status": "completed",
                "serverName": "BP-APP-01",
            },
        ],
    }

    # A representative sample of drill-in items for the two attention queues —
    # seeded explicitly (not left to fall back to the lean fixtures, whose items
    # reference workers absent from this estate) so a drill-in stays consistent
    # with the list reads. The queue summaries above carry the true totals; this
    # is the visible head of the backlog, referencing this estate's own workers.
    queue_items = [
        {
            "queue": _QUEUE_INVOICES,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0401",
            "priority": 1,
            "state": "Completed",
            "keyValue": "INV-2001",
            "status": "",
            "attemptNumber": 1,
            "lastUpdated": _recent(340),
            "completedDate": _recent(340),
            "workTimeInSeconds": 92,
            "exceptionReason": None,
            "resource": "BOT-F01",
        },
        {
            "queue": _QUEUE_INVOICES,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0402",
            "priority": 1,
            "state": "Exceptioned",
            "keyValue": "INV-2002",
            "status": "",
            "attemptNumber": 2,
            "lastUpdated": _recent(180),
            "exceptionedDate": _recent(180),
            "workTimeInSeconds": 45,
            "exceptionReason": "Invoice total did not match purchase order",
            "resource": "BOT-F01",
        },
        {
            "queue": _QUEUE_INVOICES,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0403",
            "priority": 1,
            "state": "Exceptioned",
            "keyValue": "INV-2003",
            "status": "",
            "attemptNumber": 1,
            "lastUpdated": _recent(120),
            "exceptionedDate": _recent(120),
            "workTimeInSeconds": 38,
            "exceptionReason": "Supplier not found in ledger",
            "resource": "BOT-F03",
        },
        {
            "queue": _QUEUE_INVOICES,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0404",
            "priority": 2,
            "state": "Pending",
            "keyValue": "INV-2004",
            "status": "",
            "attemptNumber": 1,
            "lastUpdated": _recent(60),
            "workTimeInSeconds": 0,
            "exceptionReason": None,
            "resource": None,
        },
        {
            "queue": _D_QUEUE_PAYMENTS,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0405",
            "priority": 1,
            "state": "Exceptioned",
            "keyValue": "PAY-5001",
            "status": "",
            "attemptNumber": 1,
            "lastUpdated": _recent(210),
            "exceptionedDate": _recent(210),
            "workTimeInSeconds": 30,
            "exceptionReason": "BACS gateway timed out",
            "resource": "BOT-F02",
        },
        {
            "queue": _D_QUEUE_PAYMENTS,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0406",
            "priority": 1,
            "state": "Pending",
            "keyValue": "PAY-5002",
            "status": "",
            "attemptNumber": 1,
            "lastUpdated": _recent(45),
            "workTimeInSeconds": 0,
            "exceptionReason": None,
            "resource": None,
        },
    ]

    # Stage logs for a couple of notable sessions so the session-log viewer has
    # content: a clean completed run and the headline terminated one (its
    # Exception stage surfaces in the errors-only filter).
    session_logs = {
        "e8a9d7c2-5f10-4b3e-bd64-0000000d0301": [
            {
                "logNumber": 1,
                "stageName": "Start",
                "stageType": "Start",
                "result": "",
                "resourceStartTime": _recent(360),
            },
            {
                "logNumber": 2,
                "stageName": "Read Invoice",
                "stageType": "Action",
                "result": "OK",
                "resourceStartTime": _recent(357),
            },
            {
                "logNumber": 3,
                "stageName": "Post to Ledger",
                "stageType": "Action",
                "result": "OK",
                "resourceStartTime": _recent(353),
            },
        ],
        "e8a9d7c2-5f10-4b3e-bd64-0000000d0309": [
            {
                "logNumber": 1,
                "stageName": "Start",
                "stageType": "Start",
                "result": "",
                "resourceStartTime": _recent(200),
            },
            {
                "logNumber": 2,
                "stageName": "Build BACS File",
                "stageType": "Action",
                "result": "Connecting to gateway",
                "resultType": "Text",
                "resourceStartTime": _recent(199),
            },
            {
                "logNumber": 3,
                "stageName": "Gateway Timeout",
                "stageType": "Exception",
                "result": "BACS gateway timed out after 30s",
                "resultType": "Text",
                "resourceStartTime": _recent(198),
            },
        ],
    }

    deferred_by_queue = {_QUEUE_INVOICES: 6, _D_QUEUE_PAYMENTS: 2}

    # A week of per-worker heat-map so resource_utilization has a real spread
    # to aggregate: F01/F02 run a full 9-5 most days (saturated), O01 a lighter
    # shift, and H02/O03 (the offline pair) report no rows at all — the "no
    # data for an offline worker" case the estate roll-up must still average
    # correctly across.
    resource_utilization = [
        row
        for days_ago in range(7)
        for row in (
            _heat_row(_D_BOT_F01, "BOT-F01", days_ago, _shift_usages(range(8, 17))),
            _heat_row(_D_BOT_F02, "BOT-F02", days_ago, _shift_usages(range(8, 17), 50)),
            _heat_row(_D_BOT_O01, "BOT-O01", days_ago, _shift_usages(range(9, 13), 35)),
        )
    ]

    limits_and_usage = {
        "publishedProcessesLimit": None,
        "publishedProcessesUsed": 5,
        "concurrentSessionsLimit": 10,
        "concurrentSessionsUsed": 4,
        "runtimeResourcesLimit": 8,
        "runtimeResourcesUsed": 5,
        "processAlertMachinesLimit": None,
        "processAlertMachinesUsed": 0,
    }

    return MockBPClient(
        resources=resources,
        resource_pools=resource_pools,
        queues=queues,
        processes=processes,
        sessions=sessions,
        queue_items=queue_items,
        session_logs=session_logs,
        schedules=schedules,
        schedule_logs=schedule_logs,
        deferred_by_queue=deferred_by_queue,
        limits_and_usage=limits_and_usage,
        resource_utilization=resource_utilization,
    )

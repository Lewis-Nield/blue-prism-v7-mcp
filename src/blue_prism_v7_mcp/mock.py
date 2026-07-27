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
exceptionMessage/terminationReason), items are full-field WorkQueueItemNoData
(no payload `data`, but every other field the schema carries — including
`sessionId`, the item→session/resource correlation, and the sla/loadedDate/
processName/tags group), processes are Process (processId/processName — the
one entity not keyed id/name), log entries are SessionLogSummary, and schedule
ids are integers (the one non-UUID id in the API). The `queue` key on item
fixtures is mock-internal plumbing (which queue holds the item), not an API
field — the single-item read drops it, renames the NoData `slaDatetime` typo
to the single-item schema's `slaDateTime`, and attaches the WorkQueueItem
`data` payload (a DataCollection, held per-item in _DEFAULT_ITEM_DATA), which
the list read never carries. Attempt history (_DEFAULT_ITEM_ATTEMPTS) is
NoData rows too, each carrying the sessionId of the session that worked that
attempt. Schedule task chains (_DEFAULT_SCHEDULE_TASKS, ScheduledTask rows
keyed by schedule id) and their sessions (_DEFAULT_TASK_SESSIONS, keyed by
task id) carry the integer ids and name-not-id session triples the live API
answers.

`tests/test_fixture_parity.py` guards this completeness for the two item
models: every fixture row's keys must be a subset of the banked schema field
list (plus the known mock-internal `queue` key), and every schema field must
appear in at least one row — so a future field this file forgets to fixture
fails CI instead of waiting for another manual spec audit.

Seed it with your own data, or accept the small built-in fixtures below.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, time, timedelta, timezone

# Fixtures are anchored to a "now" captured once at import, so the mock estate
# always reads as the current day/week rather than drifting stale against a
# hardcoded calendar date. The helpers build the ISO-8601 "...Z" timestamps the
# v7 schemas use, offset back (or forward, for an ETA) from this anchor. Tests
# that assert against the defaults import these same helpers, so they stay green
# as time passes instead of pinning literals.
_NOW = datetime.now(timezone.utc)
_TODAY = _NOW.date()
# ISO string form of _NOW, comparable lexicographically against the ISO
# `slaDatetime` timestamps items carry (the `within_sla` computed filter).
_NOW_ISO = _NOW.strftime("%Y-%m-%dT%H:%M:%SZ")


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
        "initialTaskId": 11,
        "initialTaskName": "Process Invoices",
        "intervalType": "Day",
        "calendarName": "Working Week",
        # The single-schedule read's richer definition fields (a live list row
        # carries them too — ScheduleSummary is the full definition plus ids).
        "startDate": _ts(90, "06:00:00"),
        "endDate": None,
        "timeZoneId": "GMT Standard Time",
        "dailyDetails": {"period": 1, "calendarId": 1},
    },
    {
        "id": 2,
        "name": "Weekly Reconciliation",
        "description": "Legacy reconciliation run",
        "isRetired": True,
        "tasksCount": 2,
        "initialTaskId": 21,
        "initialTaskName": "Extract Ledger",
        "intervalType": "Week",
        "calendarName": "Working Week",
    },
]

# ScheduledTask rows keyed by schedule id (task ids are integers, like schedule
# ids). Schedule 1 is a single task; schedule 2 a two-task onSuccess chain —
# the fixtures stay linear, tests seed branch/orphan shapes explicitly.
_DEFAULT_SCHEDULE_TASKS: dict[str, list[dict]] = {
    "1": [
        {
            "id": 11,
            "name": "Process Invoices",
            "description": "Work the Invoices queue",
            "failFastOnError": True,
            "delayAfterEnd": 0,
            "onSuccessTaskId": None,
            "onSuccessTaskName": None,
            "onFailureTaskId": None,
            "onFailureTaskName": None,
            "sessionsCount": 1,
        },
    ],
    "2": [
        # Listed out of chain order on purpose: the tool's chain walk (from
        # initialTaskId 21) must order these 21 → 22 regardless.
        {
            "id": 22,
            "name": "Post Adjustments",
            "description": "Post reconciliation adjustments",
            "failFastOnError": True,
            "delayAfterEnd": 0,
            "onSuccessTaskId": None,
            "onSuccessTaskName": None,
            "onFailureTaskId": None,
            "onFailureTaskName": None,
            "sessionsCount": 1,
        },
        {
            "id": 21,
            "name": "Extract Ledger",
            "description": "Pull the ledger extract",
            "failFastOnError": True,
            "delayAfterEnd": 5,
            "onSuccessTaskId": 22,
            "onSuccessTaskName": "Post Adjustments",
            "onFailureTaskId": None,
            "onFailureTaskName": None,
            "sessionsCount": 1,
        },
    ],
}

# ScheduledSession triples keyed by task id: what each task runs, and where.
# Names, not ids — the live response carries processName/resourceName.
_DEFAULT_TASK_SESSIONS: dict[str, list[dict]] = {
    "11": [
        {"processName": "Invoice Processing", "resourceName": "BOT-01", "taskSessionId": 111},
    ],
    "21": [
        {"processName": "Invoice Processing", "resourceName": "BOT-02", "taskSessionId": 211},
    ],
    "22": [
        {"processName": "Customer Onboarding", "resourceName": "BOT-02", "taskSessionId": 221},
    ],
}

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
        "ident": 1001,
        "state": "Completed",
        "keyValue": "INV-1001",
        "status": "",
        "tags": [],
        "attemptNumber": 1,
        "loadedDate": _ts(8, "08:55:00"),
        "deferredDate": None,
        "lockedDate": _ts(8, "09:00:00"),
        "lastUpdated": _ts(8, "09:05:00"),
        "completedDate": _ts(8, "09:05:00"),
        "workTimeInSeconds": 84,
        "attemptWorkTimeInSeconds": 84,
        "exceptionReason": None,
        "resource": "BOT-01",
        # Item → session correlation (BOT-01's completed day-8 Invoice run).
        "sessionId": "e8a9d7c2-5f10-4b3e-bd64-000000000301",
        "sla": 60,
        "slaDatetime": _ts(8, "10:00:00"),
        "processName": "Invoice Processing",
        "isSuggested": False,
    },
    {
        "queue": _QUEUE_INVOICES,
        "id": "f3b2a190-8c47-4e2d-9b55-000000000402",
        "priority": 1,
        "ident": 1002,
        "state": "Exceptioned",
        "keyValue": "INV-1002",
        "status": "",
        "tags": ["supplier-query"],
        "attemptNumber": 1,
        "loadedDate": _ts(7, "11:00:00"),
        "deferredDate": None,
        "lockedDate": _ts(7, "11:05:00"),
        "lastUpdated": _ts(7, "11:20:00"),
        "exceptionedDate": _ts(7, "11:20:00"),
        "workTimeInSeconds": 40,
        "attemptWorkTimeInSeconds": 40,
        "exceptionReason": "Invoice total did not match purchase order",
        "resource": "BOT-01",
        "sessionId": "e8a9d7c2-5f10-4b3e-bd64-000000000301",
        "sla": 30,
        "slaDatetime": _ts(7, "11:30:00"),  # breached shortly after the exception
        "processName": "Invoice Processing",
        "isSuggested": False,
    },
    {
        "queue": _QUEUE_ONBOARDING,
        "id": "f3b2a190-8c47-4e2d-9b55-000000000403",
        "priority": 2,
        "ident": 42,
        "state": "Pending",
        "keyValue": "CUST-0042",
        "status": "",
        "tags": [],
        "attemptNumber": 1,
        "loadedDate": _ts(6, "07:55:00"),
        "deferredDate": None,
        "lockedDate": None,
        "lastUpdated": _ts(6, "08:00:00"),
        "workTimeInSeconds": 0,
        "attemptWorkTimeInSeconds": 0,
        "exceptionReason": None,
        # Never worked — no session has touched this item yet.
        "resource": None,
        "sessionId": None,
        "sla": 120,
        "slaDatetime": _ts(-1, "08:00:00"),  # still ahead — not yet breached
        "processName": "Customer Onboarding",
        "isSuggested": False,
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
# newest carrying the live state). exceptionReason is the scrub target; each
# row carries the sessionId of the session that worked THAT attempt (None for
# an attempt no session has picked up yet).
_DEFAULT_ITEM_ATTEMPTS: dict[str, list[dict]] = {
    _ITEM_INVOICE_EXCEPTION: [
        {
            "id": _ITEM_INVOICE_EXCEPTION,
            "priority": 1,
            "ident": 1002,
            "state": "Exceptioned",
            "keyValue": "INV-1002",
            "status": "",
            "tags": ["supplier-query"],
            "attemptNumber": 1,
            "loadedDate": _ts(7, "11:00:00"),
            "deferredDate": None,
            "lockedDate": _ts(7, "11:05:00"),
            "completedDate": None,
            "lastUpdated": _ts(7, "11:20:00"),
            "exceptionedDate": _ts(7, "11:20:00"),
            "workTimeInSeconds": 40,
            "attemptWorkTimeInSeconds": 40,
            "exceptionReason": "Invoice total did not match PO; query raised by 07700 900123",
            "resource": "BOT-01",
            "sessionId": "e8a9d7c2-5f10-4b3e-bd64-000000000301",
            "sla": 30,
            "slaDatetime": _ts(7, "11:30:00"),
            "processName": "Invoice Processing",
            "isSuggested": False,
        },
        {
            "id": _ITEM_INVOICE_EXCEPTION,
            "priority": 1,
            "ident": 1002,
            "state": "Pending",
            "keyValue": "INV-1002",
            "status": "",
            "tags": ["supplier-query"],
            "attemptNumber": 2,
            "loadedDate": _ts(7, "11:00:00"),
            "deferredDate": None,
            "lockedDate": None,
            "completedDate": None,
            "lastUpdated": _ts(7, "12:00:00"),
            "exceptionedDate": None,
            "workTimeInSeconds": 0,
            "attemptWorkTimeInSeconds": 0,
            "exceptionReason": None,
            # Not yet picked up by any session.
            "resource": None,
            "sessionId": None,
            "sla": 30,
            "slaDatetime": _ts(7, "11:30:00"),
            "processName": "Invoice Processing",
            "isSuggested": False,
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
    _heat_row("5d2c8e0a-71b4-4a8e-9f30-000000000002", "BOT-02", 2, _shift_usages(range(9, 18), 40)),
    _heat_row("5d2c8e0a-71b4-4a8e-9f30-000000000002", "BOT-02", 1, _shift_usages(range(9, 18), 40)),
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
        schedule_tasks: dict[str, list[dict]] | None = None,
        task_sessions: dict[str, list[dict]] | None = None,
        limits_and_usage: dict | None = None,
        license_entitlement: dict | None = None,
        deferred_by_queue: dict[str, int] | None = None,
        permissions: list[str] | None = None,
        queue_configurations: list[dict] | None = None,
        resource_pools: list[dict] | None = None,
        environment_variables: list[dict] | None = None,
        process_groups: list[dict] | None = None,
        resource_utilization: list[dict] | None = None,
        now_fn: Callable[[], datetime] | None = None,
        settle_after: timedelta = timedelta(minutes=5),
    ) -> None:
        self._resources = (
            resources if resources is not None else [dict(r) for r in _DEFAULT_RESOURCES]
        )
        self._queues = queues if queues is not None else [dict(q) for q in _DEFAULT_QUEUES]
        self._schedules = (
            schedules if schedules is not None else [dict(s) for s in _DEFAULT_SCHEDULES]
        )
        self._sessions = sessions if sessions is not None else [dict(s) for s in _DEFAULT_SESSIONS]
        self._processes = (
            processes if processes is not None else [dict(p) for p in _DEFAULT_PROCESSES]
        )
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
        self._schedule_tasks = (
            schedule_tasks
            if schedule_tasks is not None
            else {k: [dict(t) for t in v] for k, v in _DEFAULT_SCHEDULE_TASKS.items()}
        )
        self._task_sessions = (
            task_sessions
            if task_sessions is not None
            else {k: [dict(s) for s in v] for k, v in _DEFAULT_TASK_SESSIONS.items()}
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
        self._now: Callable[[], datetime] = now_fn or (lambda: datetime.now(timezone.utc))
        self._settle_after = settle_after
        self._live_run_ids: set[str] = set()
        self._live_schedule_log_ids: set[int] = set()

    def clear_cache(self) -> None:
        """No-op — the mock has no cache, but keeps the interface identical."""

    def _fmt(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def _duration(self, start: datetime, end: datetime) -> str:
        delta = end - start
        hours, rem = divmod(int(max(delta.total_seconds(), 0)), 3600)
        mins, secs = divmod(rem, 60)
        return f"{hours:02d}:{mins:02d}:{secs:02d}"

    def _release_worker(self, resource_id: str, resource_name: str) -> None:
        row = next(
            (r for r in self._resources if r["id"] == resource_id or r["name"] == resource_name),
            None,
        )
        if row is None:
            return
        row["activeSessionCount"] = max(0, row.get("activeSessionCount", 1) - 1)
        if row["activeSessionCount"] == 0 and row.get("displayStatus") == "Working":
            row["displayStatus"] = "Idle"

    def _occupy_worker(self, resource_id: str, resource_name: str) -> None:
        row = next(
            (r for r in self._resources if r["id"] == resource_id or r["name"] == resource_name),
            None,
        )
        if row is None:
            return
        row["activeSessionCount"] = row.get("activeSessionCount", 0) + 1
        row["displayStatus"] = "Working"

    def _recount_queue(self, queue_row: dict) -> None:
        queue_row["totalItemCount"] = (
            queue_row.get("pendingItemCount", 0)
            + queue_row.get("completedItemCount", 0)
            + queue_row.get("lockedItemCount", 0)
            + queue_row.get("exceptionedItemCount", 0)
        )

    def _settle(self) -> None:
        now = self._now()
        settled_runs: set[str] = set()
        for sid in list(self._live_run_ids):
            session = self._find_session(sid)
            if session is None:
                self._live_run_ids.discard(sid)
                continue
            start = datetime.strptime(session["startTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if now - start < self._settle_after:
                continue
            session["status"] = "Completed"
            session["endTime"] = self._fmt(now)
            self._release_worker(session.get("resourceId", ""), session.get("resourceName", ""))
            self._limits_and_usage["concurrentSessionsUsed"] = max(
                0, self._limits_and_usage.get("concurrentSessionsUsed", 1) - 1
            )
            log = self._session_logs.get(sid)
            if log is not None:
                max_log = max((e["logNumber"] for e in log), default=0)
                log.append(
                    {
                        "logNumber": max_log + 1,
                        "stageName": "End",
                        "stageType": "End",
                        "result": "",
                        "resourceStartTime": self._fmt(now),
                    }
                )
            settled_runs.add(sid)
        self._live_run_ids -= settled_runs

        settled_logs: set[int] = set()
        for log_id in list(self._live_schedule_log_ids):
            log_row = None
            for rows in self._schedule_logs.values():
                for r in rows:
                    if r.get("scheduleLogId") == log_id:
                        log_row = r
                        break
                if log_row is not None:
                    break
            if log_row is None:
                self._live_schedule_log_ids.discard(log_id)
                continue
            start = datetime.strptime(log_row["startTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            if now - start < self._settle_after:
                continue
            log_row["status"] = "completed"
            log_row["endTime"] = self._fmt(now)
            log_row["duration"] = self._duration(start, now)
            settled_logs.add(log_id)
        self._live_schedule_log_ids -= settled_logs

    # --- Tier 1 reads -------------------------------------------------------

    def get_resources(self) -> list[dict]:
        self._settle()
        return [dict(r) for r in self._resources]

    def get_queues(self) -> list[dict]:
        self._settle()
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
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str | Sequence[str] | None = None,
        process_name: str | None = None,
        resource_name: str | None = None,
    ) -> list[dict]:
        """Mirror the live signature, filtering in memory.

        The narrowing filters match EXACTLY, matching the live API's `[eq]`
        and its comma-joined status enum. Case-insensitivity is the tool
        layer's job (it canonicalises names against the catalogues before
        calling); a lenient mock here would hide a bug in that step from
        every mock-backed test.
        """
        self._settle()
        sessions = self._sessions
        if start_date:
            sessions = [s for s in sessions if (s.get("startTime") or "") >= start_date]
        if end_date:
            sessions = [s for s in sessions if _at_or_before(s.get("startTime"), end_date)]
        if status:
            wanted = {status} if isinstance(status, str) else set(status)
            sessions = [s for s in sessions if s.get("status") in wanted]
        if process_name:
            sessions = [s for s in sessions if s.get("processName") == process_name]
        if resource_name:
            sessions = [s for s in sessions if s.get("resourceName") == resource_name]
        return [dict(s) for s in sessions]

    def get_session(self, session_id: str) -> dict:
        self._settle()
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
        within_sla: bool | None = None,
        sla_before: str | None = None,
        sort_by: str | None = None,
        max_records: int | None = None,
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
        if within_sla is not None:
            items = [i for i in items if _item_within_sla(i) == within_sla]
        if sla_before:
            items = [i for i in items if (i.get("slaDatetime") or "") <= sla_before]
        if sort_by == "LoadedDateAsc":
            items = sorted(items, key=lambda i: i.get("loadedDate") or "")
        if max_records is not None:
            items = items[:max_records]
        return [dict(i) for i in items]

    def get_queue_item(self, item_id: str) -> dict:
        # The single-item read returns WorkQueueItem (WITH `data`); the list
        # read returns NoData. Item ids are globally unique, so this matches the
        # queue-less live path. The mock-internal `queue` key is dropped (it is
        # not an API field), and `data` always present like the live schema —
        # an empty collection when no payload fixture exists for the item.
        # WorkQueueItem also spells the SLA deadline field with a capital T
        # (`slaDateTime`) where the NoData list/attempt rows spell it
        # `slaDatetime` (the API's own typo) — rename on the way out so the
        # single-item shape matches the live schema exactly.
        for item in self._queue_items:
            if item.get("id") == item_id:
                row = {k: v for k, v in item.items() if k != "queue"}
                if "slaDatetime" in row:
                    row["slaDateTime"] = row.pop("slaDatetime")
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
        self._settle()
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
        self._settle()
        runs = self._schedule_logs.get(str(schedule_id), [])
        if not runs:
            return None
        latest = max(runs, key=lambda r: r.get("startTime") or "")
        return dict(latest)

    def get_latest_schedule_runs(self, schedule_ids: list) -> dict[str, dict]:
        # One sweep answering the latest run per wanted schedule — mirrors the
        # live plural /scheduleLogs sweep's semantics (string keys; a schedule
        # that has never run is absent, never a fabricated outcome).
        runs: dict[str, dict] = {}
        for sid in schedule_ids:
            if sid is None:
                continue
            run = self.get_last_schedule_run(sid)
            if run is not None:
                runs[str(sid)] = run
        return runs

    def get_schedule(self, schedule_id) -> dict:
        # Strict like the live endpoint: id only (names resolve at the tool
        # layer), unknown id raises like the live 404 HTTPError. Compared as
        # strings — schedule ids are integers, but the live client
        # interpolates them into the path as strings.
        for schedule in self._schedules:
            if str(schedule.get("id")) == str(schedule_id):
                return dict(schedule)
        raise LookupError(f"No schedule with id {schedule_id!r}")

    def get_schedule_tasks(self, schedule_id) -> list[dict]:
        # Strict on the schedule (the live path 404s for an unknown id); a
        # known schedule with no task fixtures answers an empty chain.
        self.get_schedule(schedule_id)
        return [dict(t) for t in self._schedule_tasks.get(str(schedule_id), [])]

    def get_task_sessions(self, task_id) -> list[dict]:
        # Benign-empty on an unknown task, like get_item_attempts: the tool
        # layer folds sessions per task id it just listed, so an empty list is
        # the useful mock-visible shape for a task with no session fixtures.
        return [dict(s) for s in self._task_sessions.get(str(task_id), [])]

    def get_schedule_logs(
        self,
        schedule_id=None,
        status: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        # Mirror the live plural read: every schedule's runs (or one schedule's
        # with schedule_id), status matched case-insensitively (the query enum
        # is Capitalised, response rows spell it lowercase), the window on
        # startTime, newest first.
        self._settle()
        if schedule_id is not None:
            rows = list(self._schedule_logs.get(str(schedule_id), []))
        else:
            rows = [r for runs in self._schedule_logs.values() for r in runs]
        if status:
            wanted = status.casefold()
            rows = [r for r in rows if str(r.get("status", "")).casefold() == wanted]
        if start_date:
            rows = [r for r in rows if (r.get("startTime") or "") >= start_date]
        if end_date:
            rows = [r for r in rows if _at_or_before(r.get("startTime"), end_date)]
        rows = sorted(rows, key=lambda r: r.get("startTime") or "", reverse=True)
        return [dict(r) for r in rows]

    def get_current_limits_and_usage(self) -> dict:
        self._settle()
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
        prev_state = item.get("state")
        item["state"] = "Pending"
        item["attemptNumber"] = int(item.get("attemptNumber", 1)) + 1
        item["lastUpdated"] = self._fmt(self._now())
        item["exceptionReason"] = None
        item["resource"] = None
        item.pop("exceptionedDate", None)
        queue = next((q for q in self._queues if q["id"] == queue_id), None)
        if queue is not None and prev_state == "Exceptioned":
            queue["exceptionedItemCount"] = max(0, queue.get("exceptionedItemCount", 1) - 1)
            queue["pendingItemCount"] = queue.get("pendingItemCount", 0) + 1
            self._recount_queue(queue)
        attempts = self._item_attempts.setdefault(item_id, [])
        attempts.append(
            {
                "id": item_id,
                "priority": item.get("priority", 1),
                "ident": item.get("ident"),
                "state": "Pending",
                "keyValue": item.get("keyValue"),
                "status": item.get("status", ""),
                "tags": list(item.get("tags", [])),
                "attemptNumber": item["attemptNumber"],
                "loadedDate": item.get("loadedDate"),
                "deferredDate": None,
                "lockedDate": None,
                "completedDate": None,
                "lastUpdated": item["lastUpdated"],
                "exceptionedDate": None,
                "workTimeInSeconds": 0,
                "attemptWorkTimeInSeconds": 0,
                "exceptionReason": None,
                "resource": None,
                "sessionId": None,
                "sla": item.get("sla"),
                "slaDatetime": item.get("slaDatetime"),
                "processName": item.get("processName"),
                "isSuggested": item.get("isSuggested", False),
            }
        )
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
            prev_state = item.get("state")
            item["state"] = "Deferred"
            item["deferredDate"] = defer_until
            item["lastUpdated"] = self._fmt(self._now())
            queue = next((q for q in self._queues if q["id"] == queue_id), None)
            if queue is not None and prev_state == "Pending":
                queue["pendingItemCount"] = max(0, queue.get("pendingItemCount", 1) - 1)
                self._recount_queue(queue)
            self._deferred_by_queue[queue_id] = self._deferred_by_queue.get(queue_id, 0) + 1
        return None

    def start_process(
        self, process_id: str, resource_id: str, parameters: dict | None = None
    ) -> dict:
        self._session_counter += 1
        session_id = f"e8a9d7c2-5f10-4b3e-bd64-{self._session_counter:012d}"
        if parameters:
            self._session_parameters[session_id] = parameters
        now = self._now()
        start_time = self._fmt(now)
        process_name = next(
            (p["processName"] for p in self._processes if p["processId"] == process_id),
            process_id,
        )
        resource_name = next(
            (r["name"] for r in self._resources if r["id"] == resource_id),
            resource_id,
        )
        max_number = max((s.get("sessionNumber", 0) for s in self._sessions), default=0)
        self._sessions.append(
            {
                "sessionId": session_id,
                "sessionNumber": max_number + 1,
                "processId": process_id,
                "processName": process_name,
                "resourceId": resource_id,
                "resourceName": resource_name,
                "status": "Running",
                "startTime": start_time,
                "endTime": None,
                "terminationReason": "None",
                "exceptionType": None,
                "exceptionMessage": None,
            }
        )
        self._occupy_worker(resource_id, resource_name)
        self._limits_and_usage["concurrentSessionsUsed"] = (
            self._limits_and_usage.get("concurrentSessionsUsed", 0) + 1
        )
        self._session_logs[session_id] = [
            {
                "logNumber": 1,
                "stageName": "Start",
                "stageType": "Start",
                "result": "",
                "resourceStartTime": start_time,
            }
        ]
        self._live_run_ids.add(session_id)
        return {"sessionId": session_id, "status": "Running"}

    def stop_session(self, session_id: str) -> dict:
        session = self._find_session(session_id)
        if session is not None:
            was_live = session.get("status") in ("Running", "Stopping", "Warning")
            session["status"] = "Stopped"
            session["endTime"] = self._fmt(self._now())
            if was_live:
                self._release_worker(session.get("resourceId", ""), session.get("resourceName", ""))
                self._limits_and_usage["concurrentSessionsUsed"] = max(
                    0, self._limits_and_usage.get("concurrentSessionsUsed", 1) - 1
                )
            self._live_run_ids.discard(session_id)
            log = self._session_logs.get(session_id)
            if log is not None:
                max_log = max((e["logNumber"] for e in log), default=0)
                log.append(
                    {
                        "logNumber": max_log + 1,
                        "stageName": "End",
                        "stageType": "End",
                        "result": "",
                        "resourceStartTime": self._fmt(self._now()),
                    }
                )
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
        sid = str(schedule["id"])
        now = self._now()
        if start_time:
            parsed = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc)
            st = self._fmt(parsed)
        else:
            st = self._fmt(now)
        all_ids = [r["scheduleLogId"] for rows in self._schedule_logs.values() for r in rows]
        next_id = max(all_ids, default=0) + 1
        log_row = {
            "scheduleLogId": next_id,
            "scheduleId": schedule["id"],
            "scheduleName": schedule["name"],
            "startTime": st,
            "endTime": None,
            "duration": None,
            "status": "running",
            "serverName": "BP-APP-01",
        }
        self._schedule_logs.setdefault(sid, []).append(log_row)
        self._live_schedule_log_ids.add(next_id)
        return {"schedule": schedule_id, "status": "Triggered"}

    def create_queue_items(self, queue_id: str, items: list[dict]) -> dict:
        queue = next((q for q in self._queues if q["id"] == queue_id), None)
        if queue is None:
            raise ValueError(f"Queue {queue_id!r} not found.")
        ids = []
        for item in items:
            item_id = f"f3b2a190-8c47-4e2d-9b55-{len(self._queue_items):012d}"
            self._queue_items.append(
                {
                    "queue": queue_id,
                    "id": item_id,
                    "priority": item.get("priority", 1),
                    "ident": len(self._queue_items) + 9000,
                    "state": "Pending",
                    "keyValue": None,
                    "status": item.get("status", ""),
                    "tags": item.get("tags", []),
                    "attemptNumber": 1,
                    "loadedDate": _NOW_ISO,
                    "deferredDate": item.get("deferredDate"),
                    "lockedDate": None,
                    "lastUpdated": _NOW_ISO,
                    "workTimeInSeconds": 0,
                    "attemptWorkTimeInSeconds": 0,
                    "exceptionReason": None,
                    "resource": None,
                    "sessionId": None,
                    "sla": item.get("sla"),
                    "slaDatetime": None,
                    "processName": item.get("processName"),
                    "isSuggested": item.get("isSuggested", False),
                }
            )
            ids.append(item_id)
        queue["pendingItemCount"] = queue.get("pendingItemCount", 0) + len(items)
        queue["totalItemCount"] = queue.get("totalItemCount", 0) + len(items)
        return {"ids": ids}

    def stop_schedule(self, schedule_id: str) -> None:
        schedule = self._find_schedule(schedule_id)
        if schedule is None:
            return None
        sid = str(schedule["id"])
        now = self._now()
        for row in self._schedule_logs.get(sid, []):
            if row.get("status") == "running":
                row["status"] = "terminated"
                row["endTime"] = self._fmt(now)
                start = datetime.strptime(row["startTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=timezone.utc
                )
                row["duration"] = self._duration(start, now)
                self._live_schedule_log_ids.discard(row["scheduleLogId"])
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


def _item_within_sla(item: dict) -> bool:
    """True when an item's SLA deadline has not yet passed — the live API's
    computed ``withinSla`` field, mirrored here off ``slaDatetime`` vs the
    mock's captured "now". An item with no ``slaDatetime`` has nothing to
    breach, so it reads as within SLA.
    """
    deadline = item.get("slaDatetime")
    if not deadline:
        return True
    return deadline >= _NOW_ISO


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

# The demo estate's six hand-written queue items (four on Invoices, two on
# Payments — see demo_estate()) carry the deliberate narrative: PAY-5001's
# SLA breach and session link, the Invoices exceptions. Every queue's summary
# still needs to declare a real backlog, so _demo_queue_items() below tops
# each queue up to these per-state counts. This table already excludes what
# the hand-written items contribute — e.g. Invoices declares 120/812/47
# pending/completed/exceptioned, of which the hand-written items are 1/1/2,
# so the generator's own share is 119/811/45. Payments' two hand-written
# items are 1 pending/1 exceptioned (no completed of its own), so its share
# of the declared 64/540/18 is 63/540/17. Locked is NOT a top-up count — a
# locked item means a session is working it right now, so those rows come
# from _DEMO_QUEUE_LIVE_LOCKS below instead.
_DEMO_QUEUE_TOPUP_COUNTS: dict[str, tuple[int, int, int]] = {
    _QUEUE_INVOICES: (119, 811, 45),
    _D_QUEUE_PAYMENTS: (63, 540, 17),
    _QUEUE_ONBOARDING: (35, 214, 4),
    _D_QUEUE_PAYROLL: (4, 320, 0),
    _D_QUEUE_EXPENSES: (9, 410, 0),
    _D_QUEUE_VENDOR: (2, 95, 0),
    _D_QUEUE_COMPLIANCE: (0, 150, 0),
    _D_QUEUE_MAILROOM: (7, 260, 0),
    _D_QUEUE_CLOSURES: (1, 73, 0),
}

# queue id -> the locked items this estate can truthfully hold: one per
# in-flight session against that queue's process, since a Locked item is one
# a running session holds *now*. Generating locked rows freely instead would
# reintroduce exactly the incoherence this estate keeps closing — an item
# locked weeks ago by a bot that has no session at all, which reads as a
# stuck lock nobody meant to seed. Each entry is
# (resource name, session id, that session's start), and the item's lock is
# taken a minute into the run.
#
# Payments deliberately holds none: its narrative is a Running queue with a
# heavy backlog and NOTHING in progress. Every other queue has no in-flight
# session against its process, so it holds none either.
_DEMO_QUEUE_LIVE_LOCKS: dict[str, list[tuple[str, str, str]]] = {
    _QUEUE_INVOICES: [
        # BOT-F01's healthy four-minute run (session #13).
        ("BOT-F01", "e8a9d7c2-5f10-4b3e-bd64-0000000d0313", _recent(4)),
        # BOT-F02's silently stuck five-day run (session #12) — its item has
        # been locked just as long, which is what makes a stuck lock visible
        # from the queue side as well as the session side.
        ("BOT-F02", "e8a9d7c2-5f10-4b3e-bd64-0000000d0312", _ts(5, "16:00:00")),
    ],
}

# Deferred items sit outside the four counts above (WorkQueueSummary has no
# deferred field of its own — see get_queues' separate deferred lookup).
# Shared between _demo_queue_items() (which generates matching rows) and
# demo_estate()'s deferred_by_queue kwarg, so the two can't drift apart.
_DEMO_DEFERRED_BY_QUEUE: dict[str, int] = {_QUEUE_INVOICES: 6, _D_QUEUE_PAYMENTS: 2}

# queue id -> (keyValue prefix, processName, resource pool to draw
# completed/exceptioned items from). processName tracks a real startable
# process where this estate has one (Invoices, Payments, Onboarding, Payroll,
# Compliance) — the link A6 will later use to lock a queue item when a session
# starts. The remaining queues have no process in this estate; they still get a
# plausible processName label, they simply never drain via A6.
_DEMO_QUEUE_ITEM_SHAPE: dict[str, tuple[str, str, list[str]]] = {
    _QUEUE_INVOICES: ("INV", "Invoice Processing", ["BOT-F01", "BOT-F02", "BOT-F03"]),
    _D_QUEUE_PAYMENTS: ("PAY", "Payment Run", ["BOT-F01", "BOT-F02", "BOT-F03"]),
    _QUEUE_ONBOARDING: ("CUST", "Customer Onboarding", ["BOT-O01", "BOT-O02"]),
    _D_QUEUE_PAYROLL: ("PR", "Payroll Run", ["BOT-H01"]),
    _D_QUEUE_EXPENSES: ("EXP", "Expense Processing", ["BOT-F01", "BOT-F02", "BOT-F03"]),
    _D_QUEUE_VENDOR: ("VEN", "Vendor Setup", ["BOT-O01", "BOT-O02"]),
    _D_QUEUE_COMPLIANCE: ("CMP", "Compliance Screening", ["BOT-O02"]),
    _D_QUEUE_MAILROOM: ("MAIL", "Mailroom Triage", ["BOT-O01", "BOT-O02"]),
    _D_QUEUE_CLOSURES: ("CLOS", "Account Closure", ["BOT-O01", "BOT-O02"]),
}

# Generated idents come off one running counter from this base, rather than a
# per-queue range: `ident` is unique estate-wide in v7 (it is the item table's
# own identity), and hand-picked per-queue ranges silently start overlapping
# the moment a count in _DEMO_QUEUE_TOPUP_COUNTS grows. The base clears the
# hand-written items' idents (2001-2004, 5001-5002) and create_queue_items'
# own len()+9000 allocation. test_demo_estate asserts the uniqueness.
_DEMO_GENERATED_IDENT_BASE = 100_000

_DEMO_ITEM_EXCEPTION_REASONS = [
    "Validation failed against business rules",
    "Required field missing from source record",
    "Downstream system returned an error",
    "Duplicate record detected",
]

# How many days of generated queue-item history to spread across — matches
# the 90-day lookback a consumer's drill-in reads (QUEUE_ITEMS_LOOKBACK_DAYS),
# so a read at any range in that window finds populated rows.
_DEMO_QUEUE_ITEM_LOOKBACK_DAYS = 90


def _demo_item_age_minutes(i: int) -> int:
    """A deterministic age in minutes for the i-th generated item.

    Spread across _DEMO_QUEUE_ITEM_LOOKBACK_DAYS and weighted toward recent
    (the exponent compresses the day spread toward zero) so a drill-in at any
    lookback finds rows, with more of them recent. A pure function of the
    index — no RNG — so reads are reproducible run to run.
    """
    days_ago = int(_DEMO_QUEUE_ITEM_LOOKBACK_DAYS * ((i % 97) / 97) ** 1.6)
    minutes_of_day = (i * 17) % (24 * 60)
    return days_ago * 24 * 60 + minutes_of_day


def _demo_pending_age_minutes(i: int, sla_minutes: int) -> int:
    """How long the i-th generated pending item has been waiting.

    A fraction of the item's own SLA rather than a slice of the 90-day
    history spread: a pending item is current backlog, not archive, and its
    slaDatetime is loadedDate + sla — so loading the backlog months ago would
    declare all of it breached. Three in twenty wait past their deadline, so
    a `within_sla=false` drill-in returns real rows without the queue reading
    as wholly late. Pure in the index, like _demo_item_age_minutes.
    """
    return sla_minutes * (i % 20) // 16


def _minutes_since(timestamp: str) -> int:
    """Whole minutes from an ISO ``...Z`` timestamp to the captured now."""
    moment = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return int((_NOW - moment).total_seconds() // 60)


def _demo_item(
    queue_id: str,
    item_id: str,
    ident: int,
    key_value: str,
    state: str,
    process_name: str,
    resource: str | None,
    *,
    age_minutes: int,
    priority: int = 1,
    attempt: int = 1,
    sla_minutes: int = 60,
    work_seconds: int = 60,
    exception_reason: str | None = None,
    session_id: str | None = None,
) -> dict:
    """One generated WorkQueueItemNoData row, shaped for its state.

    A pure function of its arguments — the caller supplies the deterministic
    spread (age, priority, attempt) so this stays a simple state-to-fields
    mapping. Every row's ``slaDatetime`` is its own load time plus its ``sla``
    (so the deadline an item carries always agrees with the two fields it is
    computed from), and Locked/Completed/Exceptioned items pass through a lock
    phase before their terminal date — the same shape the hand-written
    foreground items already follow.
    """
    loaded = _recent(age_minutes)
    row: dict = {
        "queue": queue_id,
        "id": item_id,
        "priority": priority,
        "ident": ident,
        "state": state,
        "keyValue": key_value,
        "status": "",
        "tags": [],
        "attemptNumber": attempt,
        "loadedDate": loaded,
        "deferredDate": None,
        "lockedDate": None,
        "lastUpdated": loaded,
        "workTimeInSeconds": 0,
        "attemptWorkTimeInSeconds": 0,
        "exceptionReason": None,
        "resource": None,
        "sessionId": None,
        "sla": sla_minutes,
        "slaDatetime": _recent(age_minutes - sla_minutes),
        "processName": process_name,
        "isSuggested": False,
    }
    if state == "Pending":
        # slaDatetime stays loadedDate + sla (set above): whether a waiting
        # item has breached is then a fact about how long it has waited, not a
        # flag set independently of its own dates. The caller keeps most of
        # the pending backlog inside its SLA — see _demo_queue_items().
        return row
    if state == "Deferred":
        # A deferred item was Pending before being deferred a few days out —
        # mirrors defer_queue_item's own semantics (deferredDate is the
        # re-processing date, everything else about the item is unchanged).
        defer_days = 2 + (ident % 4)
        row["deferredDate"] = _recent(-defer_days * 24 * 60)
        return row
    lock_age = max(age_minutes - 5, 0)
    row["lockedDate"] = _recent(lock_age)
    row["resource"] = resource
    row["sessionId"] = session_id
    row["lastUpdated"] = row["lockedDate"]
    if state == "Locked":
        return row
    work_age = max(lock_age - max(1, work_seconds // 60), 0)
    row["workTimeInSeconds"] = work_seconds
    row["attemptWorkTimeInSeconds"] = work_seconds
    if state == "Completed":
        row["completedDate"] = _recent(work_age)
        row["lastUpdated"] = row["completedDate"]
    elif state == "Exceptioned":
        row["exceptionedDate"] = _recent(work_age)
        row["lastUpdated"] = row["exceptionedDate"]
        row["exceptionReason"] = exception_reason
    return row


def _demo_queue_items() -> list[dict]:
    """Deterministic top-up items for every demo queue.

    Tops up each queue to _DEMO_QUEUE_TOPUP_COUNTS' per-state counts (on top
    of the hand-written foreground items), plus the session-backed Locked rows
    from _DEMO_QUEUE_LIVE_LOCKS and Deferred rows matching
    _DEMO_DEFERRED_BY_QUEUE — exactly as _demo_history() tops up the twelve
    foreground sessions. A pure function of a running index, no RNG, so
    get_queue_items() is reproducible run to run and tests can assert shape.
    """
    items: list[dict] = []
    counter = 0

    def next_ident() -> int:
        # One estate-wide sequence, like the live item table's own identity.
        return _DEMO_GENERATED_IDENT_BASE + counter

    for queue_id, (pending, completed, exceptioned) in _DEMO_QUEUE_TOPUP_COUNTS.items():
        prefix, process_name, resources = _DEMO_QUEUE_ITEM_SHAPE[queue_id]
        for state, count in (
            ("Completed", completed),
            ("Exceptioned", exceptioned),
            ("Pending", pending),
        ):
            for _ in range(count):
                counter += 1
                ident = next_ident()
                sla_minutes = 30 + (counter % 6) * 15
                items.append(
                    _demo_item(
                        queue_id,
                        f"a1c4d8e0-6b2f-4a9c-8d31-{counter:012d}",
                        ident,
                        f"{prefix}-{ident}",
                        state,
                        process_name,
                        resources[counter % len(resources)],
                        age_minutes=(
                            _demo_pending_age_minutes(counter, sla_minutes)
                            if state == "Pending"
                            else _demo_item_age_minutes(counter)
                        ),
                        priority=(counter % 3) + 1,
                        attempt=2 if state == "Exceptioned" and counter % 5 == 0 else 1,
                        sla_minutes=sla_minutes,
                        work_seconds=30 + (counter * 11) % 180,
                        exception_reason=(
                            _DEMO_ITEM_EXCEPTION_REASONS[
                                counter % len(_DEMO_ITEM_EXCEPTION_REASONS)
                            ]
                            if state == "Exceptioned"
                            else None
                        ),
                    )
                )
    for queue_id, locks in _DEMO_QUEUE_LIVE_LOCKS.items():
        prefix, process_name, _resources = _DEMO_QUEUE_ITEM_SHAPE[queue_id]
        for resource, session_id, session_start in locks:
            counter += 1
            ident = next_ident()
            # Locked a minute into its session's run, loaded five minutes
            # before that: the lock belongs to a session that is still going,
            # so it can never read as held by an idle bot.
            lock_minutes = max(_minutes_since(session_start) - 1, 0)
            items.append(
                _demo_item(
                    queue_id,
                    f"a1c4d8e0-6b2f-4a9c-8d31-{counter:012d}",
                    ident,
                    f"{prefix}-{ident}",
                    "Locked",
                    process_name,
                    resource,
                    age_minutes=lock_minutes + 5,
                    priority=(counter % 3) + 1,
                    sla_minutes=30 + (counter % 6) * 15,
                    session_id=session_id,
                )
            )
    for queue_id, deferred_count in _DEMO_DEFERRED_BY_QUEUE.items():
        prefix, process_name, _resources = _DEMO_QUEUE_ITEM_SHAPE[queue_id]
        for _ in range(deferred_count):
            counter += 1
            ident = next_ident()
            items.append(
                _demo_item(
                    queue_id,
                    f"a1c4d8e0-6b2f-4a9c-8d31-{counter:012d}",
                    ident,
                    f"{prefix}-{ident}",
                    "Deferred",
                    process_name,
                    None,
                    age_minutes=_demo_item_age_minutes(counter),
                    priority=(counter % 3) + 1,
                    sla_minutes=45,
                )
            )
    return items


def _queue_counts_from_items(queue_id: str, items: list[dict]) -> tuple[int, int, int, int]:
    """(pending, completed, locked, exceptioned) counts for queue_id, derived
    from the item rows themselves rather than hand-carried alongside them —
    so a queue's declared summary can never drift from what a drill-in
    actually returns. Deferred items sit outside these four states.
    """
    pending = completed = locked = exceptioned = 0
    for item in items:
        if item.get("queue") != queue_id:
            continue
        state = item.get("state")
        if state == "Pending":
            pending += 1
        elif state == "Completed":
            completed += 1
        elif state == "Locked":
            locked += 1
        elif state == "Exceptioned":
            exceptioned += 1
    return pending, completed, locked, exceptioned


def _demo_queue(
    queue_id: str,
    name: str,
    group: str,
    status: str,
    items: list[dict],
    average: str,
    key_field: str = "Item Key",
) -> dict:
    """A demo-estate queue row whose four counts are derived from items."""
    pending, completed, locked, exceptioned = _queue_counts_from_items(queue_id, items)
    return _queue(
        queue_id,
        name,
        group,
        status,
        pending=pending,
        completed=completed,
        locked=locked,
        exceptioned=exceptioned,
        average=average,
        key_field=key_field,
    )


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

# Each process's typical completed-run length in minutes — distinct per
# process rather than one flat figure, so a per-process duration baseline
# (throughput_summary's duration_p50/p95/max) actually has shape to derive
# from. Payment Run is the deliberately long batch (a real payment run can
# take the best part of two hours) — everything else is a normal few-minute
# transactional process. Only Completed runs use these; Terminated runs stay
# short (a failure, not a full run) regardless of process.
_DEMO_HISTORY_BASE_MINUTES = {
    "Invoice Processing": 12,
    "Payment Run": 90,
    "Payroll Run": 20,
    "Customer Onboarding": 8,
    "Compliance Screening": 6,
}

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
    list owns today) and references this estate's own processes and workers.
    Completed-run lengths follow _DEMO_HISTORY_BASE_MINUTES per process — most
    are a normal few minutes, Payment Run is a genuinely long batch — so
    throughput_summary's duration percentiles have real, distinct per-process
    shape rather than one flat figure. A pure function of the day offset — no
    RNG — so get_sessions() is reproducible run to run and the tests can assert
    against the shape.
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
            # Terminated runs stay a short, flat 4 minutes (a failure, not a
            # completed run, so it must not shape the process's own baseline);
            # completed runs take that process's typical length plus a small
            # reproducible wobble, still a pure function of (days_ago, k).
            duration = (
                4 if terminated else _DEMO_HISTORY_BASE_MINUTES[proc_name] + (days_ago + k) % 3
            )
            end_dt = start_dt + timedelta(minutes=duration)
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
    """A populated, relative-dated MockBPClient for end-to-end evaluation.

    Invariant this fixture must satisfy (see tests/test_demo_estate.py): every
    worker with displayStatus == "Working" or activeSessionCount > 0 has at
    least one matching in-flight (status == "Running") session — matched on
    resourceId OR resourceName, since consumers join on both and v7 does not
    guarantee the two agree — and activeSessionCount equals that worker's
    in-flight session count. Every in-flight run's age is deliberate against
    its process's _DEMO_HISTORY_BASE_MINUTES; any run intended to read stale
    says so in a comment. BOT-F02's 5-day Invoice Processing run (session #12
    below) is the deliberate silently-stuck case and must stay.
    """
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

    # Six hand-written items carry the deliberate narrative — PAY-5001's SLA
    # breach and session link, the Invoices exceptions — referencing this
    # estate's own workers rather than falling back to the lean fixtures'.
    # _demo_queue_items() tops every queue up to its declared backlog on top
    # of these, so a drill-in at any queue returns real rows, not just the
    # visible head. queues (below) derives its four per-state counts from
    # this combined list, so a summary can never drift from what a read
    # actually returns.
    queue_items: list[dict] = [
        {
            "queue": _QUEUE_INVOICES,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0401",
            "priority": 1,
            "ident": 2001,
            "state": "Completed",
            "keyValue": "INV-2001",
            "status": "",
            "tags": [],
            "attemptNumber": 1,
            "loadedDate": _recent(345),
            "deferredDate": None,
            "lockedDate": _recent(342),
            "lastUpdated": _recent(340),
            "completedDate": _recent(340),
            "workTimeInSeconds": 92,
            "attemptWorkTimeInSeconds": 92,
            "exceptionReason": None,
            "resource": "BOT-F01",
            "sessionId": "e8a9d7c2-5f10-4b3e-bd64-0000000d0301",
            "sla": 60,
            "slaDatetime": _recent(300),
            "processName": "Invoice Processing",
            "isSuggested": False,
        },
        {
            "queue": _QUEUE_INVOICES,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0402",
            "priority": 1,
            "ident": 2002,
            "state": "Exceptioned",
            "keyValue": "INV-2002",
            "status": "",
            "tags": ["supplier-query"],
            "attemptNumber": 2,
            "loadedDate": _recent(185),
            "deferredDate": None,
            "lockedDate": _recent(182),
            "lastUpdated": _recent(180),
            "exceptionedDate": _recent(180),
            "workTimeInSeconds": 45,
            "attemptWorkTimeInSeconds": 45,
            "exceptionReason": "Invoice total did not match purchase order",
            "resource": "BOT-F01",
            "sessionId": "e8a9d7c2-5f10-4b3e-bd64-0000000d0302",
            "sla": 45,
            "slaDatetime": _recent(150),  # breached
            "processName": "Invoice Processing",
            "isSuggested": False,
        },
        {
            "queue": _QUEUE_INVOICES,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0403",
            "priority": 1,
            "ident": 2003,
            "state": "Exceptioned",
            "keyValue": "INV-2003",
            "status": "",
            "tags": [],
            "attemptNumber": 1,
            "loadedDate": _recent(125),
            "deferredDate": None,
            "lockedDate": _recent(122),
            "lastUpdated": _recent(120),
            "exceptionedDate": _recent(120),
            "workTimeInSeconds": 38,
            "attemptWorkTimeInSeconds": 38,
            "exceptionReason": "Supplier not found in ledger",
            "resource": "BOT-F03",
            "sessionId": "e8a9d7c2-5f10-4b3e-bd64-0000000d0307",
            "sla": 30,
            "slaDatetime": _recent(90),  # breached
            "processName": "Invoice Processing",
            "isSuggested": False,
        },
        {
            "queue": _QUEUE_INVOICES,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0404",
            "priority": 2,
            "ident": 2004,
            "state": "Pending",
            "keyValue": "INV-2004",
            "status": "",
            "tags": [],
            "attemptNumber": 1,
            "loadedDate": _recent(65),
            "deferredDate": None,
            "lockedDate": None,
            "lastUpdated": _recent(60),
            "workTimeInSeconds": 0,
            "attemptWorkTimeInSeconds": 0,
            "exceptionReason": None,
            "resource": None,
            "sessionId": None,
            "sla": 90,
            "slaDatetime": _recent(-120),  # still ahead
            "processName": "Invoice Processing",
            "isSuggested": False,
        },
        {
            "queue": _D_QUEUE_PAYMENTS,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0405",
            "priority": 1,
            "ident": 5001,
            "state": "Exceptioned",
            "keyValue": "PAY-5001",
            "status": "",
            "tags": [],
            "attemptNumber": 1,
            "loadedDate": _recent(215),
            "deferredDate": None,
            "lockedDate": _recent(212),
            "lastUpdated": _recent(210),
            "exceptionedDate": _recent(210),
            "workTimeInSeconds": 30,
            "attemptWorkTimeInSeconds": 30,
            "exceptionReason": "BACS gateway timed out",
            "resource": "BOT-F02",
            "sessionId": "e8a9d7c2-5f10-4b3e-bd64-0000000d0309",
            "sla": 20,
            "slaDatetime": _recent(180),  # breached
            "processName": "Payment Run",
            "isSuggested": False,
        },
        {
            "queue": _D_QUEUE_PAYMENTS,
            "id": "f3b2a190-8c47-4e2d-9b55-0000000d0406",
            "priority": 1,
            "ident": 5002,
            "state": "Pending",
            "keyValue": "PAY-5002",
            "status": "",
            "tags": [],
            "attemptNumber": 1,
            "loadedDate": _recent(50),
            "deferredDate": None,
            "lockedDate": None,
            "lastUpdated": _recent(45),
            "workTimeInSeconds": 0,
            "attemptWorkTimeInSeconds": 0,
            "exceptionReason": None,
            "resource": None,
            "sessionId": None,
            "sla": 60,
            "slaDatetime": _recent(-15),  # still ahead
            "processName": "Payment Run",
            "isSuggested": False,
        },
        *_demo_queue_items(),
    ]

    # Every queue's four counts below are derived from queue_items itself
    # (not hand-carried alongside it) — so a drill-in always returns exactly
    # what the summary declares.
    queues = [
        # Loaded but flowing: a deep backlog actively being drained (items
        # locked, a resource working them). Routine load, not a problem — the
        # severity scorer must read this as ok however deep the backlog.
        _demo_queue(
            _QUEUE_INVOICES,
            "Invoices",
            "Finance",
            "Running",
            queue_items,
            average="00:02:10",
            key_field="Invoice Number",
        ),
        # Stalled: a Running queue holding a heavy backlog with NOTHING in
        # progress (no items locked) — no resource is draining it. The genuine
        # stuck case the scorer flags critical, distinct from Invoices' flow.
        _demo_queue(
            _D_QUEUE_PAYMENTS,
            "Payments",
            "Finance",
            "Running",
            queue_items,
            average="00:01:48",
            key_field="Payment Ref",
        ),
        # Paused: work held, a small backlog waiting.
        _demo_queue(
            _QUEUE_ONBOARDING,
            "Onboarding",
            "Operations",
            "Paused",
            queue_items,
            average="00:03:02",
            key_field="Customer Id",
        ),
        # The healthy bulk — no exceptions — so a console's collapsed-healthy
        # summary has a real count to fold away.
        _demo_queue(
            _D_QUEUE_PAYROLL,
            "Payroll",
            "HR",
            "Running",
            queue_items,
            average="00:04:20",
        ),
        _demo_queue(
            _D_QUEUE_EXPENSES,
            "Expenses",
            "Finance",
            "Running",
            queue_items,
            average="00:01:05",
        ),
        _demo_queue(
            _D_QUEUE_VENDOR,
            "Vendor Setup",
            "Operations",
            "Running",
            queue_items,
            average="00:05:40",
        ),
        _demo_queue(
            _D_QUEUE_COMPLIANCE,
            "Compliance Checks",
            "Operations",
            "Running",
            queue_items,
            average="00:02:30",
        ),
        _demo_queue(
            _D_QUEUE_MAILROOM,
            "Mailroom",
            "Operations",
            "Running",
            queue_items,
            average="00:00:45",
        ),
        _demo_queue(
            _D_QUEUE_CLOSURES,
            "Account Closures",
            "Operations",
            "Running",
            queue_items,
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
        # In-flight runs on the working bots. This one is Payment Run — the
        # genuinely long batch (~90min baseline, see _DEMO_HISTORY_BASE_MINUTES)
        # — an hour into its run: healthy under its own baseline, where a flat
        # global threshold would misread it as stale. Session #12 below is the
        # other direction: a short-baseline process truthfully stuck for days.
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0307",
            7,
            _D_PROC_PAYMENTS,
            "Payment Run",
            _D_BOT_F03,
            "BOT-F03",
            "Running",
            _recent(60),
            None,
        ),
        # Five minutes into an 8min-baseline process: healthy, unlike the old
        # 90-minutes-in seed that read as 9x its own baseline.
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0308",
            8,
            _PROC_ONBOARDING,
            "Customer Onboarding",
            _D_BOT_O01,
            "BOT-O01",
            "Running",
            _recent(5),
            None,
        ),
        # BOT-F01 reads displayStatus Working with activeSessionCount 1 — it
        # needs a matching in-flight session or a resource/session join reports
        # a status/session mismatch. Four minutes against Invoice Processing's
        # ~12min baseline reads healthy.
        _session(
            "e8a9d7c2-5f10-4b3e-bd64-0000000d0313",
            13,
            _PROC_INVOICES,
            "Invoice Processing",
            _D_BOT_F01,
            "BOT-F01",
            "Running",
            _recent(4),
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
        # days, the in-flight-severity case a console must surface. Invoice
        # Processing's own baseline is ~12min (see _DEMO_HISTORY_BASE_MINUTES),
        # so this reads truthfully Warning/Critical against ITS OWN history,
        # not just a flat global threshold.
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
            "initialTaskId": 11,
            "initialTaskName": "Process Invoices",
            "intervalType": "Day",
            "calendarName": "Working Week",
            "startDate": _ts(180, "06:00:00"),
            "endDate": None,
            "timeZoneId": "GMT Standard Time",
            "dailyDetails": {"period": 1, "calendarId": 1},
        },
        {
            "id": 2,
            "name": "Nightly Payment Run",
            "description": "Generates the outbound BACS file overnight",
            "isRetired": False,
            "tasksCount": 3,
            "initialTaskId": 21,
            "initialTaskName": "Build BACS File",
            "intervalType": "Day",
            "calendarName": "Working Week",
            "startDate": _ts(180, "02:00:00"),
            "endDate": None,
            "timeZoneId": "GMT Standard Time",
            "dailyDetails": {"period": 1, "calendarId": 1},
        },
        {
            "id": 3,
            "name": "Weekly Reconciliation",
            "description": "Legacy reconciliation run",
            "isRetired": True,
            "tasksCount": 2,
            "initialTaskId": 31,
            "initialTaskName": "Extract Ledger",
            "intervalType": "Week",
            "calendarName": "Working Week",
        },
        {
            "id": 4,
            "name": "Hourly Compliance Sync",
            "description": "Keeps the sanctions list current",
            "isRetired": False,
            "tasksCount": 1,
            "initialTaskId": 41,
            "initialTaskName": "Sync Sanctions List",
            "intervalType": "Hour",
            "calendarName": "All Days",
            "startDate": _ts(180, "00:00:00"),
            "endDate": None,
            "timeZoneId": "GMT Standard Time",
            "hourlyDetails": {"period": 1, "start": "07:00", "end": "19:00", "calendarId": 2},
        },
    ]

    # Task chains for the demo schedules. The headline: the failed Nightly
    # Payment Run is a real branching chain — Build BACS File runs Payment Run
    # on BOT-F02, dispatches on success, and alerts on-call on failure — so a
    # consumer can walk from "the schedule terminated" to which task, running
    # what, where. The compliance task fans out across two workers (one task,
    # two sessions).
    schedule_tasks = {
        "1": [
            {
                "id": 11,
                "name": "Process Invoices",
                "description": "Work the Invoices queue",
                "failFastOnError": True,
                "delayAfterEnd": 0,
                "onSuccessTaskId": None,
                "onSuccessTaskName": None,
                "onFailureTaskId": None,
                "onFailureTaskName": None,
                "sessionsCount": 1,
            },
        ],
        "2": [
            {
                "id": 21,
                "name": "Build BACS File",
                "description": "Assemble the outbound payment file",
                "failFastOnError": True,
                "delayAfterEnd": 0,
                "onSuccessTaskId": 22,
                "onSuccessTaskName": "Dispatch Payments",
                "onFailureTaskId": 23,
                "onFailureTaskName": "Alert On-Call",
                "sessionsCount": 1,
            },
            {
                "id": 22,
                "name": "Dispatch Payments",
                "description": "Submit the BACS file to the gateway",
                "failFastOnError": True,
                "delayAfterEnd": 0,
                "onSuccessTaskId": None,
                "onSuccessTaskName": None,
                "onFailureTaskId": 23,
                "onFailureTaskName": "Alert On-Call",
                "sessionsCount": 1,
            },
            {
                "id": 23,
                "name": "Alert On-Call",
                "description": "Raise the out-of-hours payments alert",
                "failFastOnError": False,
                "delayAfterEnd": 0,
                "onSuccessTaskId": None,
                "onSuccessTaskName": None,
                "onFailureTaskId": None,
                "onFailureTaskName": None,
                "sessionsCount": 1,
            },
        ],
        "3": [
            {
                "id": 31,
                "name": "Extract Ledger",
                "description": "Pull the ledger extract",
                "failFastOnError": True,
                "delayAfterEnd": 5,
                "onSuccessTaskId": 32,
                "onSuccessTaskName": "Post Adjustments",
                "onFailureTaskId": None,
                "onFailureTaskName": None,
                "sessionsCount": 1,
            },
            {
                "id": 32,
                "name": "Post Adjustments",
                "description": "Post reconciliation adjustments",
                "failFastOnError": True,
                "delayAfterEnd": 0,
                "onSuccessTaskId": None,
                "onSuccessTaskName": None,
                "onFailureTaskId": None,
                "onFailureTaskName": None,
                "sessionsCount": 1,
            },
        ],
        "4": [
            {
                "id": 41,
                "name": "Sync Sanctions List",
                "description": "Refresh the sanctions screening data",
                "failFastOnError": False,
                "delayAfterEnd": 0,
                "onSuccessTaskId": None,
                "onSuccessTaskName": None,
                "onFailureTaskId": None,
                "onFailureTaskName": None,
                "sessionsCount": 2,
            },
        ],
    }

    task_sessions = {
        "11": [
            {"processName": "Invoice Processing", "resourceName": "BOT-F01", "taskSessionId": 111},
        ],
        "21": [
            {"processName": "Payment Run", "resourceName": "BOT-F02", "taskSessionId": 211},
        ],
        "22": [
            {"processName": "Payment Run", "resourceName": "BOT-F02", "taskSessionId": 221},
        ],
        "23": [
            {"processName": "Payment Run", "resourceName": "BOT-F03", "taskSessionId": 231},
        ],
        "31": [
            {"processName": "Invoice Processing", "resourceName": "BOT-F03", "taskSessionId": 311},
        ],
        "32": [
            {"processName": "Invoice Processing", "resourceName": "BOT-F03", "taskSessionId": 321},
        ],
        "41": [
            {
                "processName": "Compliance Screening",
                "resourceName": "BOT-O02",
                "taskSessionId": 411,
            },
            {
                "processName": "Compliance Screening",
                "resourceName": "BOT-O01",
                "taskSessionId": 412,
            },
        ],
    }

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

    # Shares _DEMO_DEFERRED_BY_QUEUE with _demo_queue_items() (above), which
    # generates the matching Deferred item rows — the two can't drift apart.
    deferred_by_queue = dict(_DEMO_DEFERRED_BY_QUEUE)

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
        schedule_tasks=schedule_tasks,
        task_sessions=task_sessions,
        deferred_by_queue=deferred_by_queue,
        limits_and_usage=limits_and_usage,
        resource_utilization=resource_utilization,
    )

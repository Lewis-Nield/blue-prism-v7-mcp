"""MockBPClient — an offline, in-memory stand-in for BPClient.

The dashboard's mock_provider read JSON fixtures so the UI could run with no
live estate. Here the same idea becomes a drop-in client: it exposes the exact
surface of BPClient — the Tier 1 reads plus the Phase 2 extensions (queue items,
processes, the session stage-log) and the Tier 3 writes — but serves data held
in memory, so tool tests (Phase 4) and local runs need neither a Blue Prism
server nor mocked HTTP. The writes mutate the in-memory fixtures, so a test can
call retry/defer/trigger and then observe the effect on a subsequent read.

Seed it with your own data, or accept the small built-in fixtures below.
"""
from __future__ import annotations

from typing import Any

_DEFAULT_RESOURCES: list[dict] = [
    {"name": "BOT-01", "status": "Idle", "attributes": "None"},
    {"name": "BOT-02", "status": "Running", "attributes": "None"},
    {"name": "BOT-03", "status": "Offline", "attributes": "None"},
]

_DEFAULT_QUEUES: list[dict] = [
    {"name": "Invoices", "pending": 12, "completed": 340, "exceptioned": 5},
    {"name": "Onboarding", "pending": 0, "completed": 88, "exceptioned": 0},
]

_DEFAULT_SCHEDULES: list[dict] = [
    {"name": "Daily Invoice Run", "enabled": True, "lastOutcome": "Success"},
    {"name": "Weekly Reconciliation", "enabled": False, "lastOutcome": "Failed"},
]

_DEFAULT_SESSIONS: list[dict] = [
    {
        "id": "sess-001",
        "date": "2026-03-01",
        "resource": "BOT-01",
        "process": "Invoice Processing",
        "status": "Completed",
        "items_processed": 120,
        "duration_secs": 540,
    },
    {
        "id": "sess-002",
        "date": "2026-03-02",
        "resource": "BOT-02",
        "process": "Customer Onboarding",
        "status": "Terminated",
        "items_processed": 4,
        "duration_secs": 95,
    },
    {
        "id": "sess-003",
        "date": "2026-03-05",
        "resource": "BOT-01",
        "process": "Invoice Processing",
        "status": "Completed",
        "items_processed": 200,
        "duration_secs": 880,
    },
]

_DEFAULT_PROCESSES: list[dict] = [
    {"id": "proc-001", "name": "Invoice Processing", "version": 3, "published": True},
    {"id": "proc-002", "name": "Customer Onboarding", "version": 1, "published": True},
]

_DEFAULT_QUEUE_ITEMS: list[dict] = [
    {
        "queue": "Invoices",
        "id": "item-001",
        "status": "Completed",
        "process": "Invoice Processing",
        "completed": "2026-03-01",
        "exceptionReason": None,
    },
    {
        "queue": "Invoices",
        "id": "item-002",
        "status": "Exceptioned",
        "process": "Invoice Processing",
        "completed": "2026-03-02",
        "exceptionType": "Business",
        "exceptionReason": "Invoice total did not match purchase order",
    },
    {
        "queue": "Onboarding",
        "id": "item-003",
        "status": "Pending",
        "process": "Customer Onboarding",
        "completed": "",
        "exceptionReason": None,
    },
]

_DEFAULT_SESSION_LOGS: dict[str, list[dict]] = {
    "sess-001": [
        {"stage": "Start", "result": "Success", "resource": "BOT-01"},
        {"stage": "Read Invoice", "result": "Success", "resource": "BOT-01"},
        {"stage": "Post to Ledger", "result": "Success", "resource": "BOT-01"},
    ],
    "sess-002": [
        {"stage": "Start", "result": "Success", "resource": "BOT-02"},
        {
            "stage": "Validate Customer",
            "result": "Exception",
            "resource": "BOT-02",
            "exceptionReason": "Customer record not found",
        },
    ],
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
        session_logs: dict[str, list[dict]] | None = None,
    ) -> None:
        self._resources = resources if resources is not None else list(_DEFAULT_RESOURCES)
        self._queues = queues if queues is not None else list(_DEFAULT_QUEUES)
        self._schedules = schedules if schedules is not None else list(_DEFAULT_SCHEDULES)
        self._sessions = sessions if sessions is not None else list(_DEFAULT_SESSIONS)
        self._processes = processes if processes is not None else list(_DEFAULT_PROCESSES)
        self._queue_items = (
            queue_items if queue_items is not None else [dict(i) for i in _DEFAULT_QUEUE_ITEMS]
        )
        self._session_logs = (
            session_logs
            if session_logs is not None
            else {k: list(v) for k, v in _DEFAULT_SESSION_LOGS.items()}
        )

    def clear_cache(self) -> None:
        """No-op — the mock has no cache, but keeps the interface identical."""

    # --- Tier 1 reads -------------------------------------------------------

    def get_resources(self) -> list[dict]:
        return list(self._resources)

    def get_queues(self) -> list[dict]:
        return list(self._queues)

    def get_schedules(self) -> list[dict]:
        return list(self._schedules)

    def get_sessions(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict]:
        sessions = self._sessions
        if start_date:
            sessions = [s for s in sessions if _date_of(s) >= start_date]
        if end_date:
            sessions = [s for s in sessions if _date_of(s) <= end_date]
        return list(sessions)

    def get_processes(self) -> list[dict]:
        return list(self._processes)

    def get_queue_items(
        self,
        queue: str,
        status: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        items = [i for i in self._queue_items if i.get("queue") == queue]
        if status:
            items = [i for i in items if i.get("status") == status]
        if start_date:
            items = [i for i in items if i.get("completed", "") >= start_date]
        if end_date:
            items = [i for i in items if i.get("completed", "") <= end_date]
        return [dict(i) for i in items]

    def get_session_log(self, session_id: str) -> list[dict]:
        return [dict(e) for e in self._session_logs.get(session_id, [])]

    # --- Tier 3 writes (mutate the in-memory fixtures) ----------------------

    def retry_queue_item(self, queue: str, item_id: str) -> dict:
        item = self._find_item(queue, item_id)
        if item is not None:
            item["status"] = "Pending"
        return {"queue": queue, "id": item_id, "status": "Pending"}

    def defer_queue_item(self, queue: str, item_id: str, defer_until: str) -> dict:
        item = self._find_item(queue, item_id)
        if item is not None:
            item["status"] = "Deferred"
            item["deferUntil"] = defer_until
        return {"queue": queue, "id": item_id, "status": "Deferred", "deferUntil": defer_until}

    def mark_exception_resolved(self, queue: str, item_id: str) -> dict:
        item = self._find_item(queue, item_id)
        if item is not None:
            item["status"] = "Completed"
            item["exceptionReason"] = None
        return {"queue": queue, "id": item_id, "status": "Completed"}

    def start_process(self, process_id: str, resource: str | None = None) -> dict:
        session_id = f"sess-{process_id}"
        self._sessions.append(
            {
                "id": session_id,
                "date": "",
                "resource": resource or "",
                "process": process_id,
                "status": "Pending",
                "items_processed": 0,
                "duration_secs": 0,
            }
        )
        return {"sessionId": session_id, "status": "Pending"}

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> dict:
        schedule = self._find_schedule(schedule_id)
        if schedule is not None:
            schedule["enabled"] = bool(enabled)
        return {"schedule": schedule_id, "enabled": bool(enabled)}

    def trigger_schedule(self, schedule_id: str) -> dict:
        schedule = self._find_schedule(schedule_id)
        if schedule is not None:
            schedule["lastOutcome"] = "Triggered"
        return {"schedule": schedule_id, "status": "Triggered"}

    # --- Lookup helpers -----------------------------------------------------

    def _find_item(self, queue: str, item_id: str) -> dict | None:
        for item in self._queue_items:
            if item.get("queue") == queue and item.get("id") == item_id:
                return item
        return None

    def _find_schedule(self, schedule_id: str) -> dict | None:
        for schedule in self._schedules:
            if schedule_id in (schedule.get("id"), schedule.get("name")):
                return schedule
        return None


def _date_of(session: dict[str, Any]) -> str:
    return session.get("date", "")

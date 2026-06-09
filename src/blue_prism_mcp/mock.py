"""MockBPClient — an offline, in-memory stand-in for BPClient.

The dashboard's mock_provider read JSON fixtures so the UI could run with no
live estate. Here the same idea becomes a drop-in client: it exposes the exact
read surface of BPClient (get_resources / get_queues / get_schedules /
get_sessions) but serves data held in memory, so tool tests (Phase 4) and
local runs need neither a Blue Prism server nor mocked HTTP.

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
        "date": "2026-03-01",
        "resource": "BOT-01",
        "process": "Invoice Processing",
        "status": "Completed",
        "items_processed": 120,
        "duration_secs": 540,
    },
    {
        "date": "2026-03-02",
        "resource": "BOT-02",
        "process": "Customer Onboarding",
        "status": "Terminated",
        "items_processed": 4,
        "duration_secs": 95,
    },
    {
        "date": "2026-03-05",
        "resource": "BOT-01",
        "process": "Invoice Processing",
        "status": "Completed",
        "items_processed": 200,
        "duration_secs": 880,
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
    ) -> None:
        self._resources = resources if resources is not None else list(_DEFAULT_RESOURCES)
        self._queues = queues if queues is not None else list(_DEFAULT_QUEUES)
        self._schedules = schedules if schedules is not None else list(_DEFAULT_SCHEDULES)
        self._sessions = sessions if sessions is not None else list(_DEFAULT_SESSIONS)

    def clear_cache(self) -> None:
        """No-op — the mock has no cache, but keeps the interface identical."""

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


def _date_of(session: dict[str, Any]) -> str:
    return session.get("date", "")

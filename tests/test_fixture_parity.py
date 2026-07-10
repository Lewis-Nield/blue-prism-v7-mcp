"""Fixture-parity CI guard: mock.py's fixture rows vs banked live-schema fields.

Three prior spec audits verified endpoint existence and consumed-field
correctness, but never asked "which response-model fields exist that the mock
simply never fixtures?" (see DESIGN.md's ground truth, the 2026-07-07
correction block — the sessionId gap this closes went unnoticed through all
three). This test closes that class for the two item models that have a
verified, on-hand field list: `WorkQueueItemNoData` (list rows, attempt-history
rows) and `WorkQueueItem` (the single-item GET, which adds `data` and spells
the SLA field `slaDateTime`). It asserts every fixture row's keys are a subset
of (schema fields ∪ known mock-internal keys) and every schema field appears
in at least one row, so a field this file forgets to fixture fails CI instead
of waiting for another manual audit.

Scoped deliberately to just these two models: other response shapes
(SessionSummary, WorkQueueSummary, ScheduleLogSummary, Calendar, ...) don't
have a verified full field list banked yet, and the live spec JSON needs a
browser-UA fetch to refetch — do NOT invent a field list for a model you
can't verify. Extend `_MODEL_FIELDS`-shaped structures here only once a list
is banked in the ground-truth memory.
"""

from blue_prism_v7_mcp.mock import (
    _DEFAULT_ITEM_ATTEMPTS,
    _DEFAULT_QUEUE_ITEMS,
    MockBPClient,
    demo_estate,
)

# WorkQueueItemNoData — verified against the 7.5.1 spec, identical in 7.2.0.
_WORK_QUEUE_ITEM_NO_DATA_FIELDS = frozenset(
    {
        "id",
        "priority",
        "ident",
        "state",
        "keyValue",
        "status",
        "tags",
        "attemptNumber",
        "loadedDate",
        "deferredDate",
        "lockedDate",
        "completedDate",
        "exceptionedDate",
        "lastUpdated",
        "workTimeInSeconds",
        "attemptWorkTimeInSeconds",
        "exceptionReason",
        "resource",
        "sessionId",
        "sla",
        "slaDatetime",
        "processName",
        "isSuggested",
    }
)

# WorkQueueItem (the single-item GET) — same field set plus the payload
# `data`, and the SLA deadline is spelled with a capital T (a real API
# inconsistency between the two shapes, not a typo in this list).
_WORK_QUEUE_ITEM_FIELDS = (_WORK_QUEUE_ITEM_NO_DATA_FIELDS - {"slaDatetime"}) | {
    "slaDateTime",
    "data",
}

# Mock-internal keys that are plumbing, not API fields — stripped before a
# row ever reaches a caller (get_queue_item drops `queue`).
_MOCK_INTERNAL_KEYS = frozenset({"queue"})


def _assert_full_parity(rows: list[dict], schema_fields: frozenset[str], label: str) -> None:
    allowed = schema_fields | _MOCK_INTERNAL_KEYS
    seen: set[str] = set()
    for row in rows:
        extra = set(row) - allowed
        assert not extra, f"{label} row {row.get('id')!r} has unknown keys: {extra}"
        seen |= set(row) & schema_fields
    missing = schema_fields - seen
    assert not missing, f"{label} fixtures never populate: {missing}"


class TestQueueItemFixtureParity:
    def test_default_queue_items_have_full_field_parity(self):
        _assert_full_parity(
            _DEFAULT_QUEUE_ITEMS, _WORK_QUEUE_ITEM_NO_DATA_FIELDS, "default queue item"
        )

    def test_default_item_attempts_have_full_field_parity(self):
        rows = [row for attempts in _DEFAULT_ITEM_ATTEMPTS.values() for row in attempts]
        _assert_full_parity(rows, _WORK_QUEUE_ITEM_NO_DATA_FIELDS, "default item attempt")

    def test_demo_estate_queue_items_have_full_field_parity(self):
        rows = demo_estate()._queue_items
        _assert_full_parity(rows, _WORK_QUEUE_ITEM_NO_DATA_FIELDS, "demo queue item")

    def test_get_queue_item_answers_the_full_work_queue_item_shape(self):
        # The single-item read composes its row from the list fixture (drops
        # `queue`, renames `slaDatetime`, adds `data`) — verify the COMPOSED
        # shape matches WorkQueueItem exactly, across both estates.
        for client in (demo_estate(), MockBPClient()):
            rows = [client.get_queue_item(item["id"]) for item in client._queue_items]
            _assert_full_parity(rows, _WORK_QUEUE_ITEM_FIELDS, "get_queue_item")

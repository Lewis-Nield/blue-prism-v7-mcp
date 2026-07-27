"""Demo-estate fixture invariants (see mock.py's demo_estate() docstring).

Every worker the demo fixture marks as busy must be backed by a real in-flight
session, and the session count on the worker row must match reality — else a
consumer joining resources to sessions reports a status/session mismatch that
no governed action can resolve. This guards the invariant so the fixture can't
silently drift out of self-consistency again.
"""

from datetime import datetime, timezone

from blue_prism_v7_mcp.mock import (
    _DEMO_DEFERRED_BY_QUEUE,
    _DEMO_HISTORY_BASE_MINUTES,
    _QUEUE_INVOICES,
    _date,
    demo_estate,
)


def _in_flight_sessions_for(resource, sessions):
    return [
        s
        for s in sessions
        if s["status"] == "Running"
        and (s["resourceId"] == resource["id"] or s["resourceName"] == resource["name"])
    ]


class TestDemoEstateWorkerSessionCoherence:
    def test_busy_workers_have_a_matching_in_flight_session(self):
        client = demo_estate()
        for resource in client._resources:
            busy = resource["displayStatus"] == "Working" or resource["activeSessionCount"] > 0
            if not busy:
                continue
            in_flight = _in_flight_sessions_for(resource, client._sessions)
            assert in_flight, f"{resource['name']} reads busy with no in-flight session"

    def test_active_session_count_matches_in_flight_session_count(self):
        # Scoped to busy workers only: BOT-F02 is the deliberate exception —
        # Idle with activeSessionCount 0 but a genuinely stuck Running session,
        # which is exactly the silently-stuck case this fixture must keep.
        client = demo_estate()
        for resource in client._resources:
            busy = resource["displayStatus"] == "Working" or resource["activeSessionCount"] > 0
            if not busy:
                continue
            in_flight = _in_flight_sessions_for(resource, client._sessions)
            assert resource["activeSessionCount"] == len(in_flight), (
                f"{resource['name']} activeSessionCount "
                f"({resource['activeSessionCount']}) != in-flight sessions ({len(in_flight)})"
            )

    def test_exactly_one_worker_holds_a_deliberately_stale_run(self):
        # BOT-F02's 5-day Invoice Processing run is the deliberate
        # silently-stuck case: Running but on a worker that reads Idle, so it
        # is excluded from the "busy workers" invariant above by design.
        client = demo_estate()
        idle_running = [
            s
            for s in client._sessions
            if s["status"] == "Running"
            and not any(
                (r["id"] == s["resourceId"] or r["name"] == s["resourceName"])
                and (r["displayStatus"] == "Working" or r["activeSessionCount"] > 0)
                for r in client._resources
            )
        ]
        assert [s["resourceName"] for s in idle_running] == ["BOT-F02"]

    def test_working_bots_read_healthy_against_their_own_baseline(self):
        # BOT-F01, BOT-F03, BOT-O01 each hold a healthy in-flight run — its age
        # sits within a generous band of its own process's baseline. BOT-F02 is
        # excluded: its run is the deliberate stale case, many multiples over.
        client = demo_estate()
        now = datetime.now(timezone.utc)
        by_worker = {s["resourceName"]: s for s in client._sessions if s["status"] == "Running"}
        assert set(by_worker) == {"BOT-F01", "BOT-F03", "BOT-O01", "BOT-F02"}
        for name in ("BOT-F01", "BOT-F03", "BOT-O01"):
            session = by_worker[name]
            baseline = _DEMO_HISTORY_BASE_MINUTES[session["processName"]]
            start = datetime.strptime(session["startTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            elapsed_minutes = (now - start).total_seconds() / 60
            assert elapsed_minutes <= 2 * baseline, (
                f"{name}'s in-flight run is {elapsed_minutes:.0f}min against a "
                f"{baseline}min baseline — reads stale, not healthy"
            )

        stale = by_worker["BOT-F02"]
        stale_baseline = _DEMO_HISTORY_BASE_MINUTES[stale["processName"]]
        stale_start = datetime.strptime(stale["startTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
        stale_elapsed_minutes = (now - stale_start).total_seconds() / 60
        assert stale_elapsed_minutes > 10 * stale_baseline

    def test_licence_usage_matches_the_estate_it_describes(self):
        # A7: concurrentSessionsUsed/runtimeResourcesUsed must be derived from
        # the fixture, not hand-picked, or a downstream utilisation-against-
        # licence reading traces to nothing.
        client = demo_estate()
        limits = client.get_current_limits_and_usage()
        running_sessions = sum(1 for s in client._sessions if s["status"] == "Running")
        non_offline_workers = sum(1 for r in client._resources if r["databaseStatus"] != "Offline")
        assert limits["concurrentSessionsUsed"] == running_sessions
        assert limits["runtimeResourcesUsed"] == non_offline_workers
        # Headroom against the authored limits is deliberate — a demo with no
        # slack left looks broken, not busy.
        assert limits["concurrentSessionsUsed"] < limits["concurrentSessionsLimit"]
        assert limits["runtimeResourcesUsed"] < limits["runtimeResourcesLimit"]

    def test_every_worker_has_a_utilization_row_every_day(self):
        # A8: a missing read is never a fabricated 0%, but an absent worker
        # looks broken in a demo — every one of the eight gets a real row.
        client = demo_estate()
        rows = client.get_resource_utilization(_date(6))
        names = {r["name"] for r in client._resources}
        for name in names:
            worker_rows = [r for r in rows if r["digitalWorkerName"] == name]
            assert len(worker_rows) == 7, f"{name} has {len(worker_rows)} rows, expected 7"

    def test_offline_workers_read_zero_not_absent(self):
        client = demo_estate()
        rows = client.get_resource_utilization(_date(6))
        for name in ("BOT-H02", "BOT-O03"):
            worker_rows = [r for r in rows if r["digitalWorkerName"] == name]
            assert worker_rows
            for row in worker_rows:
                assert sum(row["usages"]) == 0

    def test_working_bots_have_nonzero_utilization(self):
        client = demo_estate()
        rows = client.get_resource_utilization(_date(6))
        for name in ("BOT-F01", "BOT-F02", "BOT-F03", "BOT-H01", "BOT-O01", "BOT-O02"):
            worker_rows = [r for r in rows if r["digitalWorkerName"] == name]
            assert any(sum(row["usages"]) > 0 for row in worker_rows), name


class TestDemoEstateQueueItemBacking:
    """A5: every queue's declared counts must equal what a drill-in actually
    returns — the fixture no longer just asserts thousands while the item
    list holds a handful.
    """

    def test_queue_summary_counts_equal_item_counts_by_state(self):
        client = demo_estate()
        for queue in client._queues:
            items = [i for i in client._queue_items if i["queue"] == queue["id"]]
            by_state = {"Pending": 0, "Completed": 0, "Locked": 0, "Exceptioned": 0}
            for item in items:
                if item["state"] in by_state:
                    by_state[item["state"]] += 1
            assert queue["pendingItemCount"] == by_state["Pending"], queue["name"]
            assert queue["completedItemCount"] == by_state["Completed"], queue["name"]
            assert queue["lockedItemCount"] == by_state["Locked"], queue["name"]
            assert queue["exceptionedItemCount"] == by_state["Exceptioned"], queue["name"]
            assert queue["totalItemCount"] == sum(by_state.values()), queue["name"]

    def test_payments_pending_read_returns_the_full_declared_backlog(self):
        client = demo_estate()
        payments = next(q for q in client._queues if q["name"] == "Payments")
        rows = client.get_queue_items(payments["id"], state="Pending")
        assert len(rows) == payments["pendingItemCount"] == 64

    def test_every_declared_queue_has_a_nonempty_item_set(self):
        # A queue with a nonzero declared count must have that many rows, not
        # just the visible hand-written head — every queue in this estate
        # declares at least one state with items. totalItemCount excludes
        # Deferred rows (see the class docstring above), so compare against
        # the four counted states only.
        client = demo_estate()
        for queue in client._queues:
            items = [
                i
                for i in client._queue_items
                if i["queue"] == queue["id"] and i["state"] != "Deferred"
            ]
            assert len(items) == queue["totalItemCount"], queue["name"]
            assert items, f"{queue['name']} has no backing items at all"

    def test_deferred_items_match_deferred_by_queue_and_sit_outside_the_four_counts(self):
        client = demo_estate()
        for queue_id, expected in _DEMO_DEFERRED_BY_QUEUE.items():
            deferred = [
                i
                for i in client._queue_items
                if i["queue"] == queue_id and i["state"] == "Deferred"
            ]
            assert len(deferred) == expected == client._deferred_by_queue[queue_id]
            queue = next(q for q in client._queues if q["id"] == queue_id)
            # totalItemCount is pending+completed+locked+exceptioned only —
            # deferred items must not have inflated it.
            assert queue["totalItemCount"] == (
                queue["pendingItemCount"]
                + queue["completedItemCount"]
                + queue["lockedItemCount"]
                + queue["exceptionedItemCount"]
            )

    def test_generated_idents_are_unique_estate_wide(self):
        # `ident` is the item table's own identity in v7 — unique across every
        # queue, not per queue. Hand-picked per-queue ranges overlap as soon as
        # a count grows, so the generator draws one estate-wide sequence and
        # this asserts it.
        client = demo_estate()
        idents = [i["ident"] for i in client._queue_items]
        assert len(idents) == len(set(idents))
        key_values = [i["keyValue"] for i in client._queue_items]
        assert len(key_values) == len(set(key_values))


class TestDemoEstateQueueItemCoherence:
    """A locked item is one a running session holds right now, and an item's
    SLA deadline is a fact about its own load time — neither may be asserted
    independently of the estate around it.
    """

    def test_every_locked_item_is_held_by_a_live_session(self):
        client = demo_estate()
        locked = [i for i in client._queue_items if i["state"] == "Locked"]
        assert locked, "the estate should hold at least one in-progress item"
        for item in locked:
            session = next(
                (s for s in client._sessions if s["sessionId"] == item["sessionId"]), None
            )
            assert session is not None, item["keyValue"]
            assert session["status"] == "Running", item["keyValue"]
            assert session["resourceName"] == item["resource"], item["keyValue"]
            assert session["processName"] == item["processName"], item["keyValue"]
            # The lock is taken during the run, never before it started.
            assert item["lockedDate"] >= session["startTime"], item["keyValue"]

    def test_item_sla_deadlines_follow_from_their_own_load_times(self):
        client = demo_estate()
        for item in client._queue_items:
            # The hand-written narrative items carry their own deliberate
            # deadlines; the generated bulk must be internally consistent.
            if not item["id"].startswith("a1c4d8e0") or not item["sla"]:
                continue
            loaded = datetime.strptime(item["loadedDate"], "%Y-%m-%dT%H:%M:%SZ")
            deadline = datetime.strptime(item["slaDatetime"], "%Y-%m-%dT%H:%M:%SZ")
            assert round((deadline - loaded).total_seconds() / 60) == item["sla"], item["keyValue"]

    def test_the_pending_backlog_has_real_breaches_without_being_wholly_late(self):
        # The A5 wall in the other direction: an SLA drill-in that returns
        # nothing is as useless as an item read that returns one row — but a
        # backlog where everything is late is not an estate anyone would demo.
        client = demo_estate()
        pending = [i for i in client._queue_items if i["state"] == "Pending"]
        breached = client.get_queue_items(_QUEUE_INVOICES, state="Pending", within_sla=False)
        assert breached
        assert len(breached) < sum(1 for i in pending if i["queue"] == _QUEUE_INVOICES)

"""Demo-estate fixture invariants (see mock.py's demo_estate() docstring).

Every worker the demo fixture marks as busy must be backed by a real in-flight
session, and the session count on the worker row must match reality — else a
consumer joining resources to sessions reports a status/session mismatch that
no governed action can resolve. This guards the invariant so the fixture can't
silently drift out of self-consistency again.
"""

from datetime import datetime, timezone

from blue_prism_v7_mcp.mock import _DEMO_DEFERRED_BY_QUEUE, _DEMO_HISTORY_BASE_MINUTES, demo_estate


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

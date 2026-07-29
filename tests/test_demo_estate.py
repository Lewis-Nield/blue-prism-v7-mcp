"""Demo-estate fixture invariants (see mock.py's demo_estate() docstring).

Every worker the demo fixture marks as busy must be backed by a real in-flight
session, and the session count on the worker row must match reality — else a
consumer joining resources to sessions reports a status/session mismatch that
no governed action can resolve. This guards the invariant so the fixture can't
silently drift out of self-consistency again.
"""

from datetime import datetime, timedelta, timezone

from blue_prism_v7_mcp.mock import (
    _DEMO_DEFERRED_BY_QUEUE,
    _DEMO_HISTORY_BASE_MINUTES,
    _DEMO_HISTORY_PROCESSES,
    _DEMO_QUEUE_ITEM_SHAPE,
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


class TestDemoEstateCatalogueCoherence:
    """Nothing in this estate may name a process the catalogue does not hold.

    Every assertion here is phrased as an invariant over demo_estate() rather
    than against a fixed expected count. The orphan queues these close (four
    queues stamping a processName on thousands of items with no process row,
    no session and no schedule behind it) survived the previous coherence pass
    precisely because the tests of the day asserted counts, and counts stay
    true while the thing they count grows incoherent.
    """

    def test_every_queue_item_names_a_process_in_the_catalogue(self):
        client = demo_estate()
        catalogue = {p["processName"] for p in client.get_processes()}
        named = {i["processName"] for i in client._queue_items if i.get("processName")}
        assert named <= catalogue, f"queue items name absent processes: {sorted(named - catalogue)}"

    def test_every_session_names_a_process_in_the_catalogue(self):
        client = demo_estate()
        catalogue = {p["processName"] for p in client.get_processes()}
        named = {s["processName"] for s in client._sessions}
        assert named <= catalogue, f"sessions name absent processes: {sorted(named - catalogue)}"

    def test_every_queue_shape_names_a_real_process_and_real_workers(self):
        # The shape table is what stamps a processName onto every generated
        # item, so it is the single place an orphan queue can be introduced.
        client = demo_estate()
        catalogue = {p["processName"] for p in client.get_processes()}
        workers = {r["name"] for r in client.get_resources()}
        for queue_id, (_prefix, process_name, resources) in _DEMO_QUEUE_ITEM_SHAPE.items():
            assert process_name in catalogue, f"{queue_id} works an absent process"
            assert set(resources) <= workers, f"{queue_id} draws on absent workers"

    def test_every_process_has_finished_runs_behind_it(self):
        # A process with no run history has no duration baseline, so a console
        # cannot say whether any in-flight run of it is healthy or stale.
        client = demo_estate()
        finished = {
            s["processName"] for s in client._sessions if s["status"] in ("Completed", "Terminated")
        }
        for process in client.get_processes():
            assert process["processName"] in finished, process["processName"]

    def test_every_process_has_its_own_duration_baseline(self):
        client = demo_estate()
        for process in client.get_processes():
            assert process["processName"] in _DEMO_HISTORY_BASE_MINUTES, process["processName"]
        # Distinct baselines are the point of that table — one flat figure
        # gives a per-process duration percentile nothing to take shape from.
        baselines = [_DEMO_HISTORY_BASE_MINUTES[n] for n in _history_process_names()]
        assert len(set(baselines)) == len(baselines)

    def test_the_history_rotation_covers_the_whole_catalogue(self):
        client = demo_estate()
        catalogue = {p["processId"]: p["processName"] for p in client.get_processes()}
        rotated = {process_id: name for process_id, name, _rid, _rname in _DEMO_HISTORY_PROCESSES}
        assert rotated == catalogue

    def test_licence_published_process_count_is_derived_from_the_catalogue(self):
        client = demo_estate()
        published = sum(1 for p in client.get_processes() if "Published" in p["attributes"])
        assert client.get_current_limits_and_usage()["publishedProcessesUsed"] == published

    def test_every_queue_has_a_configuration_naming_the_process_that_works_it(self):
        client = demo_estate()
        process_ids = {p["processName"]: p["processId"] for p in client.get_processes()}
        configurations = {c["id"]: c for c in client.get_queue_configurations()}
        for queue in client.get_queues():
            configuration = configurations.get(queue["id"])
            assert configuration is not None, f"{queue['name']} has no configuration"
            _prefix, process_name, _resources = _DEMO_QUEUE_ITEM_SHAPE[queue["id"]]
            assigned = configuration["activeWorkQueueConfiguration"]["assignedProcessId"]
            assert assigned == process_ids[process_name], queue["name"]

    def test_configuration_active_sessions_agree_with_the_queue_summary(self):
        # A configuration claiming sessions on a queue whose summary reports
        # nothing locked is the same class of dangling join as an orphan
        # process name, one field further in.
        client = demo_estate()
        summaries = {q["id"]: q for q in client.get_queues()}
        for configuration in client.get_queue_configurations():
            active = configuration["activeQueueStats"]["activeSessions"]
            assert active == summaries[configuration["id"]]["lockedItemCount"], configuration[
                "name"
            ]

    def test_the_process_tree_holds_every_process_and_its_folder(self):
        client = demo_estate()
        nodes = client.get_process_groups()
        items = {n["name"] for n in nodes if n["nodeType"] == "Item"}
        folders = {n["name"] for n in nodes if n["nodeType"] == "Group"}
        processes = client.get_processes()
        assert {p["processName"] for p in processes} == items
        assert {p["groupName"] for p in processes} <= folders

    def test_every_scheduled_task_session_names_real_processes_and_workers(self):
        client = demo_estate()
        catalogue = {p["processName"] for p in client.get_processes()}
        workers = {r["name"] for r in client.get_resources()}
        for schedule in client.get_schedules():
            for task in client.get_schedule_tasks(schedule["id"]):
                for run in client.get_task_sessions(task["id"]):
                    assert run["processName"] in catalogue, task["name"]
                    assert run["resourceName"] in workers, task["name"]

    def test_every_schedule_task_chain_resolves(self):
        client = demo_estate()
        for schedule in client.get_schedules():
            tasks = client.get_schedule_tasks(schedule["id"])
            assert schedule["tasksCount"] == len(tasks), schedule["name"]
            by_id = {t["id"]: t for t in tasks}
            assert schedule["initialTaskId"] in by_id, schedule["name"]
            for task in tasks:
                for link in ("onSuccessTaskId", "onFailureTaskId"):
                    if task[link] is not None:
                        assert task[link] in by_id, f"{schedule['name']}/{task['name']}"

    def test_some_published_process_is_deliberately_left_unscheduled(self):
        # Preserved incoherence, not an oversight: a real estate always runs
        # some published processes by hand or off a queue trigger, and a
        # uniform queue->process->schedule estate reads synthetic. Vendor
        # Setup, Payroll Run and Customer Onboarding are the current three.
        client = demo_estate()
        scheduled = {
            run["processName"]
            for schedule in client.get_schedules()
            for task in client.get_schedule_tasks(schedule["id"])
            for run in client.get_task_sessions(task["id"])
        }
        catalogue = {p["processName"] for p in client.get_processes()}
        assert catalogue - scheduled


def _history_process_names():
    return [name for _process_id, name, _rid, _rname in _DEMO_HISTORY_PROCESSES]


class TestDemoEstateConfigurationStaysLive:
    """A configuration's stats describe the estate now, not at construction.

    The identity half of a configuration (which process works this queue,
    which group drains it) is fixed, but the activity half is a claim about
    live state — and this estate settles. Frozen stats are the same dangling
    join as an orphan process name arriving by a different route: the numbers
    stay plausible while the queue they describe empties underneath them.
    """

    def _configuration(self, client, queue_name):
        queue = next(q for q in client.get_queues() if q["name"] == queue_name)
        configuration = next(c for c in client.get_queue_configurations() if c["id"] == queue["id"])
        return queue, configuration

    def test_stats_follow_a_session_from_start_through_drain(self):
        client = demo_estate()
        _queue, before = self._configuration(client, "Mailroom")
        assert before["activeQueueStats"]["activeSessions"] == 0

        process = next(p for p in client.get_processes() if p["processName"] == "Mailroom Triage")
        worker = next(r for r in client.get_resources() if r["name"] == "BOT-O01")
        client.start_process(process["processId"], worker["id"])

        queue, started = self._configuration(client, "Mailroom")
        assert started["activeQueueStats"]["activeSessions"] == queue["lockedItemCount"] == 1

        # Far enough ahead that the run drains the queue and ends itself.
        client._now = lambda: datetime.now(timezone.utc) + timedelta(hours=3)
        drained, after = self._configuration(client, "Mailroom")
        assert drained["pendingItemCount"] == 0
        assert after["activeQueueStats"]["activeSessions"] == 0
        assert after["activeQueueStats"]["timeRemaining"] == "00:00:00"

    def test_active_sessions_track_the_summary_after_a_trigger(self):
        # The same agreement the catalogue tests assert at construction, but
        # after a schedule has moved the estate on.
        client = demo_estate()
        client.trigger_schedule("Nightly Payment Run")
        summaries = {q["id"]: q for q in client.get_queues()}
        for configuration in client.get_queue_configurations():
            active = configuration["activeQueueStats"]["activeSessions"]
            assert active == summaries[configuration["id"]]["lockedItemCount"], configuration[
                "name"
            ]

    def test_a_returned_configuration_cannot_rewrite_the_fixture(self):
        client = demo_estate()
        row = client.get_queue_configurations()[0]
        row["activeQueueStats"]["activeSessions"] = 999
        row["activeWorkQueueConfiguration"]["assignedProcessId"] = "rewritten"
        fresh = client.get_queue_configurations()[0]
        assert fresh["activeQueueStats"]["activeSessions"] != 999
        assert fresh["activeWorkQueueConfiguration"]["assignedProcessId"] != "rewritten"


class TestDemoEstateScheduleTrigger:
    """A9: triggering a schedule must not just log itself — it should start
    its tasks' sessions exactly as a manual start_process would, so the
    "stalled queue" (Payments, deliberately holding no in-flight lock — see
    _DEMO_QUEUE_LIVE_LOCKS) actually starts draining once the schedule that
    works it fires.
    """

    def test_triggering_the_nightly_payment_run_starts_its_tasks(self):
        client = demo_estate()
        payments_before = next(q for q in client.get_queues() if q["name"] == "Payments")
        assert payments_before["lockedItemCount"] == 0

        resources_before = {r["name"]: r["activeSessionCount"] for r in client.get_resources()}

        def _payment_runs(resource_name):
            return [
                s
                for s in client.get_sessions()
                if s["status"] == "Running"
                and s["resourceName"] == resource_name
                and s["processName"] == "Payment Run"
            ]

        f02_before = len(_payment_runs("BOT-F02"))
        f03_before = len(_payment_runs("BOT-F03"))

        client.trigger_schedule("Nightly Payment Run")

        # Build BACS File and Dispatch Payments both fire on BOT-F02; Alert
        # On-Call fires on BOT-F03 — all at once, no chaining, on top of
        # whatever in-flight runs the estate already held on those bots.
        assert len(_payment_runs("BOT-F02")) == f02_before + 2
        assert len(_payment_runs("BOT-F03")) == f03_before + 1

        resources = {r["name"]: r for r in client.get_resources()}
        assert resources["BOT-F02"]["displayStatus"] == "Working"
        assert resources["BOT-F02"]["activeSessionCount"] == resources_before["BOT-F02"] + 2

        payments_after = next(q for q in client.get_queues() if q["name"] == "Payments")
        assert payments_after["lockedItemCount"] == 3
        assert payments_after["pendingItemCount"] == payments_before["pendingItemCount"] - 3
        assert payments_after["totalItemCount"] == payments_before["totalItemCount"]

        # The schedule's own log still settles exactly as before A9.
        last = client.get_last_schedule_run(2)
        assert last is not None
        assert last["status"] == "running"

    def test_triggering_a_schedule_with_a_future_start_time_defers_its_sessions(self):
        # trigger_schedule documents a future start_time as "run it once at
        # that time instead" — a trigger scheduled days out must not occupy
        # workers this instant, only once the mock clock reaches that time.
        client = demo_estate()
        before = {r["name"]: r["activeSessionCount"] for r in client.get_resources()}

        client.trigger_schedule("Nightly Payment Run", "2099-01-01T09:00:00Z")

        after = {r["name"]: r["activeSessionCount"] for r in client.get_resources()}
        assert after["BOT-F02"] == before["BOT-F02"]
        assert after["BOT-F03"] == before["BOT-F03"]

        payments = next(q for q in client.get_queues() if q["name"] == "Payments")
        assert payments["lockedItemCount"] == 0

        # The log row itself still exists, running, dated for the future.
        last = client.get_last_schedule_run(2)
        assert last is not None
        assert last["startTime"] == "2099-01-01T09:00:00Z"
        assert last["status"] == "running"

    def test_stopping_a_triggered_schedule_stops_only_the_sessions_it_started(self):
        # stop_schedule is documented as trigger_schedule's sibling: it must
        # undo exactly what the trigger started, leaving the estate's own
        # pre-seeded in-flight runs on the same workers untouched.
        client = demo_estate()
        client.trigger_schedule("Nightly Payment Run")

        started = [
            s
            for s in client._sessions
            if s["status"] == "Running" and s["resourceName"] in ("BOT-F02", "BOT-F03")
        ]
        assert len(started) == 5

        client.stop_schedule("Nightly Payment Run")

        still = [
            s
            for s in client._sessions
            if s["status"] == "Running" and s["resourceName"] in ("BOT-F02", "BOT-F03")
        ]
        # Only the two pre-seeded in-flight runs remain: BOT-F02's silently
        # stuck 5-day Invoice Processing session and BOT-F03's own Payment
        # Run, both untouched by a stop meant for the trigger's own sessions.
        assert len(still) == 2

        resources = {r["name"]: r for r in client.get_resources()}
        assert resources["BOT-F02"]["activeSessionCount"] == 0
        assert resources["BOT-F03"]["activeSessionCount"] == 1

        payments = next(q for q in client.get_queues() if q["name"] == "Payments")
        assert payments["lockedItemCount"] == 0

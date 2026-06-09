"""Tests for the decoupled v7 client (Phase 1).

Ported from the dashboard's tests/test_providers.py, adapted for the
Streamlit-ectomy: state lives on a BPClient instance, so each test builds a
fresh client (no cross-test cache/token leakage to reset) and patches that
instance's requests.Session. Out-of-core reads (exceptions/utilisation/ROI/
Zabbix) are not ported — they are not part of the reusable surface.
"""
from unittest.mock import MagicMock

import pytest

from blue_prism_mcp.client import BPClient
from blue_prism_mcp.config import BPConfig
from blue_prism_mcp.mock import MockBPClient


def make_config(**overrides) -> BPConfig:
    base = dict(base_url="https://bp.example/api/v7", username="u", password="p")
    base.update(overrides)
    return BPConfig(**base)


def _resp(body, status_code: int = 200) -> MagicMock:
    return MagicMock(
        status_code=status_code,
        json=MagicMock(return_value=body),
        raise_for_status=MagicMock(),
    )


def _auth_resp(token: str = "test-token") -> MagicMock:
    return _resp({"access_token": token})


def make_client(**config_overrides) -> tuple[BPClient, MagicMock]:
    """A BPClient wired to a fully-mocked session, pre-stubbed for auth."""
    session = MagicMock()
    session.post.return_value = _auth_resp()
    client = BPClient(make_config(**config_overrides), session=session)
    return client, session


# --- Mock client --------------------------------------------------------------


class TestMockClient:
    """The offline client serves valid in-memory data with the right shape."""

    def test_get_resources(self):
        resources = MockBPClient().get_resources()
        assert isinstance(resources, list)
        assert len(resources) > 0
        assert all("name" in r for r in resources)

    def test_get_queues(self):
        assert len(MockBPClient().get_queues()) > 0

    def test_get_schedules(self):
        assert len(MockBPClient().get_schedules()) > 0

    def test_get_sessions(self):
        sessions = MockBPClient().get_sessions()
        assert len(sessions) > 0
        required = {"process", "status", "items_processed", "duration_secs"}
        for s in sessions:
            assert required.issubset(s.keys())

    def test_get_sessions_with_date_filter(self):
        client = MockBPClient()
        all_sessions = client.get_sessions()
        dates = sorted({s["date"] for s in all_sessions})
        mid = dates[len(dates) // 2]
        filtered = client.get_sessions(start_date=mid)
        assert all(s["date"] >= mid for s in filtered)
        assert len(filtered) <= len(all_sessions)

    def test_get_sessions_with_end_date_filter(self):
        client = MockBPClient()
        dates = sorted({s["date"] for s in client.get_sessions()})
        mid = dates[len(dates) // 2]
        filtered = client.get_sessions(end_date=mid)
        assert all(s["date"] <= mid for s in filtered)

    def test_seeded_data_overrides_defaults(self):
        client = MockBPClient(queues=[{"name": "Only"}])
        assert client.get_queues() == [{"name": "Only"}]

    def test_returned_lists_are_copies(self):
        client = MockBPClient()
        client.get_resources().append({"name": "INJECTED"})
        assert all(r["name"] != "INJECTED" for r in client.get_resources())


# --- Live client (mocked HTTP) ------------------------------------------------


class TestBPClient:
    def test_get_resources(self):
        client, session = make_client()
        session.get.return_value = _resp([{"name": "BOT-01"}])
        assert client.get_resources() == [{"name": "BOT-01"}]
        session.get.assert_called_once()

    def test_get_queues(self):
        client, session = make_client()
        session.get.return_value = _resp([{"name": "Q1", "pending": 5}])
        assert client.get_queues() == [{"name": "Q1", "pending": 5}]

    def test_get_schedules(self):
        client, session = make_client()
        session.get.return_value = _resp([{"name": "Daily Run"}])
        assert client.get_schedules() == [{"name": "Daily Run"}]

    def test_get_sessions(self):
        client, session = make_client()
        session.get.return_value = _resp([{"process": "Test", "status": "Completed"}])
        assert len(client.get_sessions()) == 1

    def test_get_sessions_passes_date_params(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_sessions(start_date="2026-03-01", end_date="2026-03-07")
        params = session.get.call_args.kwargs["params"]
        assert params["startdatefrom"] == "2026-03-01"
        assert params["startdateto"] == "2026-03-07"

    def test_token_caches(self):
        client, session = make_client()
        session.post.return_value = _auth_resp("cached-token")
        assert client._get_token() == "cached-token"
        client._get_token()
        session.post.assert_called_once()

    def test_token_refresh_on_401(self):
        client, session = make_client()
        client._token = "expired-token"
        session.post.return_value = _auth_resp("new-token")
        session.get.side_effect = [_resp(None, 401), _resp([{"name": "BOT-01"}])]
        assert client.get_resources() == [{"name": "BOT-01"}]
        assert client._token == "new-token"

    def test_read_is_cached_within_ttl(self):
        client, session = make_client()
        session.get.return_value = _resp([{"name": "BOT-01"}])
        client.get_resources()
        client.get_resources()
        session.get.assert_called_once()  # second read served from cache

    def test_clear_cache_forces_refetch(self):
        client, session = make_client()
        session.get.return_value = _resp([{"name": "BOT-01"}])
        client.get_resources()
        client.clear_cache()
        client.get_resources()
        assert session.get.call_count == 2

    def test_expired_cache_refetches(self):
        client, session = make_client(cache_ttl=0)  # everything is immediately stale
        session.get.return_value = _resp([{"name": "BOT-01"}])
        client.get_resources()
        client.get_resources()
        assert session.get.call_count == 2

    def test_two_clients_do_not_share_token(self):
        a, _ = make_client()
        b, _ = make_client()
        a._token = "token-a"
        assert b._token is None


# --- Pagination (_get_collection) ---------------------------------------------


class TestPagination:
    """Defensive paging handles plain-list, item-envelope, token, and offset."""

    def test_plain_list_single_page(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": 1}, {"id": 2}])
        assert client._get_collection("/resources") == [{"id": 1}, {"id": 2}]
        assert session.get.call_count == 1  # short page → stop

    def test_items_envelope_without_token(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [{"id": 1}]})
        assert client._get_collection("/workqueues") == [{"id": 1}]
        assert session.get.call_count == 1

    def test_token_paging_follows_tokens(self):
        client, session = make_client()
        session.get.side_effect = [
            _resp({"items": [{"id": 1}], "pagingToken": "p2"}),
            _resp({"items": [{"id": 2}], "pagingToken": "p3"}),
            _resp({"items": [{"id": 3}]}),  # no token → last page
        ]
        assert client._get_collection("/sessions") == [{"id": 1}, {"id": 2}, {"id": 3}]
        assert session.get.call_count == 3

    def test_offset_paging_until_short_page(self):
        client, session = make_client(page_size=3)
        full = [{"id": i} for i in range(3)]
        session.get.side_effect = [_resp(full), _resp([{"id": 999}])]
        result = client._get_collection("/sessions")
        assert len(result) == 4
        assert session.get.call_count == 2

    def test_max_pages_cap(self):
        client, session = make_client(page_size=3, max_pages=2)
        session.get.return_value = _resp([{"id": i} for i in range(3)])  # always full
        result = client._get_collection("/sessions")
        assert session.get.call_count == 2  # capped
        assert len(result) == 6

    def test_auto_detects_token_paging_from_present_empty_token(self):
        # A token endpoint marks its last page with an empty token. The page is
        # full (== page_size), so a truthiness-based guess would misread it as
        # offset and issue a spurious follow-up. Presence of the key → token,
        # and the empty value stops the loop after a single request.
        client, session = make_client(page_size=2)
        session.get.return_value = _resp({"items": [{"id": 1}, {"id": 2}], "pagingToken": ""})
        assert client._get_collection("/sessions") == [{"id": 1}, {"id": 2}]
        assert session.get.call_count == 1  # not misclassified as offset

    def test_mode_none_single_request(self):
        client, session = make_client(paging_mode="none", page_size=3)
        session.get.return_value = _resp([{"id": i} for i in range(3)])
        result = client._get_collection("/sessions")
        assert session.get.call_count == 1  # no paging despite a full page
        assert len(result) == 3

    def test_unexpected_body_yields_empty(self):
        client, session = make_client(paging_mode="none")
        session.get.return_value = _resp("not a collection")
        assert client._get_collection("/resources") == []

    def test_offset_param_advances_by_collected(self):
        client, session = make_client(page_size=2)
        session.get.side_effect = [_resp([{"id": 1}, {"id": 2}]), _resp([{"id": 3}])]
        client._get_collection("/sessions")
        second_call_params = session.get.call_args_list[1].kwargs["params"]
        assert second_call_params["startIndex"] == 2


# --- Phase 2: extended reads (mocked HTTP) ------------------------------------


class TestExtendedReads:
    def test_get_processes(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "p1", "name": "Proc"}])
        assert client.get_processes() == [{"id": "p1", "name": "Proc"}]

    def test_get_queue_items_hits_queue_path_with_filters(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_queue_items(
            "Invoices",
            status="Exceptioned",
            start_date="2026-03-01",
            end_date="2026-03-07",
        )
        call = session.get.call_args
        assert call.args[0].endswith("/workqueues/Invoices/items")
        params = call.kwargs["params"]
        assert params["status"] == "Exceptioned"
        assert params["completedafter"] == "2026-03-01"
        assert params["completedbefore"] == "2026-03-07"

    def test_get_queue_items_without_filters_sends_no_filter_params(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "i1"}])
        assert client.get_queue_items("Invoices") == [{"id": "i1"}]
        params = session.get.call_args.kwargs["params"]  # paging adds pageSize only
        assert "status" not in params
        assert "completedafter" not in params
        assert "completedbefore" not in params

    def test_get_queue_items_caches_per_filter(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "i1"}])
        client.get_queue_items("Q", status="Pending")
        client.get_queue_items("Q", status="Exceptioned")
        assert session.get.call_count == 2  # distinct cache keys, not shared

    def test_get_session_log(self):
        client, session = make_client()
        session.get.return_value = _resp([{"stage": "Start"}])
        assert client.get_session_log("sess-1") == [{"stage": "Start"}]
        assert session.get.call_args.args[0].endswith("/sessions/sess-1/logs")


# --- Phase 2: Tier 3 writes (mocked HTTP) -------------------------------------


class TestTierThreeWrites:
    def test_retry_queue_item(self):
        client, session = make_client()
        client._token = "t"
        session.post.return_value = _resp({"status": "Pending"})
        assert client.retry_queue_item("Invoices", "item-1") == {"status": "Pending"}
        assert session.post.call_args.args[0].endswith(
            "/workqueues/Invoices/items/item-1/retry"
        )

    def test_defer_queue_item_sends_body(self):
        client, session = make_client()
        client._token = "t"
        session.post.return_value = _resp({})
        client.defer_queue_item("Invoices", "item-1", "2026-04-01T00:00:00")
        assert session.post.call_args.kwargs["json"] == {
            "deferUntil": "2026-04-01T00:00:00"
        }
        assert session.post.call_args.args[0].endswith(
            "/workqueues/Invoices/items/item-1/defer"
        )

    def test_mark_exception_resolved(self):
        client, session = make_client()
        client._token = "t"
        session.post.return_value = _resp({})
        client.mark_exception_resolved("Q", "i1")
        assert session.post.call_args.args[0].endswith("/workqueues/Q/items/i1/resolve")

    def test_start_process_with_resource(self):
        client, session = make_client()
        client._token = "t"
        session.post.return_value = _resp({"sessionId": "s1"})
        client.start_process("proc-1", resource="BOT-01")
        assert session.post.call_args.kwargs["json"] == {
            "processId": "proc-1",
            "resourceName": "BOT-01",
        }

    def test_start_process_without_resource_omits_it(self):
        client, session = make_client()
        client._token = "t"
        session.post.return_value = _resp({})
        client.start_process("proc-1")
        assert session.post.call_args.kwargs["json"] == {"processId": "proc-1"}

    def test_set_schedule_enabled_uses_put(self):
        client, session = make_client()
        client._token = "t"
        session.put.return_value = _resp({})
        client.set_schedule_enabled("sched-1", False)
        assert session.put.call_args.kwargs["json"] == {"enabled": False}
        assert session.put.call_args.args[0].endswith("/schedules/sched-1")

    def test_trigger_schedule(self):
        client, session = make_client()
        client._token = "t"
        session.post.return_value = _resp({})
        client.trigger_schedule("sched-1")
        assert session.post.call_args.args[0].endswith("/schedules/sched-1/run")

    def test_write_invalidates_read_cache(self):
        client, session = make_client()
        session.get.return_value = _resp([{"name": "Q1"}])
        client.get_queues()  # populate cache
        client._token = "t"
        session.post.return_value = _resp({})
        client.trigger_schedule("s1")  # write must drop the cache
        client.get_queues()
        assert session.get.call_count == 2  # refetched after the write

    def test_write_reauths_on_401(self):
        client, session = make_client()
        client._token = "expired"
        session.post.side_effect = [
            _resp(None, 401),  # write rejected — token expired
            _auth_resp("new"),  # re-auth
            _resp({"ok": True}),  # write retried
        ]
        assert client.trigger_schedule("s1") == {"ok": True}
        assert client._token == "new"


# --- Phase 2: mock client extensions ------------------------------------------


class TestMockExtended:
    def test_get_processes(self):
        assert len(MockBPClient().get_processes()) > 0

    def test_get_queue_items_filters_by_queue_status_and_dates(self):
        client = MockBPClient()
        items = client.get_queue_items(
            "Invoices", status="Exceptioned", start_date="2026-03-01", end_date="2026-03-31"
        )
        assert items and all(i["queue"] == "Invoices" for i in items)
        assert all(i["status"] == "Exceptioned" for i in items)

    def test_get_queue_items_unknown_queue_is_empty(self):
        assert MockBPClient().get_queue_items("Nope") == []

    def test_get_session_log(self):
        log = MockBPClient().get_session_log("sess-002")
        assert any(e["result"] == "Exception" for e in log)

    def test_get_session_log_unknown_session_is_empty(self):
        assert MockBPClient().get_session_log("nope") == []

    def test_retry_queue_item_flips_status(self):
        client = MockBPClient()
        client.retry_queue_item("Invoices", "item-002")
        item = client.get_queue_items("Invoices", status="Pending")
        assert any(i["id"] == "item-002" for i in item)

    def test_defer_queue_item_records_defer_until(self):
        client = MockBPClient()
        client.defer_queue_item("Invoices", "item-001", "2026-04-01T00:00:00")
        deferred = client.get_queue_items("Invoices", status="Deferred")
        assert deferred[0]["deferUntil"] == "2026-04-01T00:00:00"

    def test_mark_exception_resolved_clears_reason(self):
        client = MockBPClient()
        client.mark_exception_resolved("Invoices", "item-002")
        resolved = [i for i in client.get_queue_items("Invoices") if i["id"] == "item-002"]
        assert resolved[0]["status"] == "Completed"
        assert resolved[0]["exceptionReason"] is None

    def test_start_process_appends_session(self):
        client = MockBPClient()
        before = len(client.get_sessions())
        result = client.start_process("Invoice Processing", resource="BOT-01")
        assert result["status"] == "Pending"
        assert len(client.get_sessions()) == before + 1

    def test_set_schedule_enabled_flips_flag(self):
        client = MockBPClient()
        client.set_schedule_enabled("Daily Invoice Run", False)
        sched = [s for s in client.get_schedules() if s["name"] == "Daily Invoice Run"][0]
        assert sched["enabled"] is False

    def test_trigger_schedule_records_outcome(self):
        client = MockBPClient()
        client.trigger_schedule("Daily Invoice Run")
        sched = [s for s in client.get_schedules() if s["name"] == "Daily Invoice Run"][0]
        assert sched["lastOutcome"] == "Triggered"

    def test_write_on_unknown_target_is_safe(self):
        client = MockBPClient()
        # No matching item/schedule — returns an ack without raising or mutating.
        assert client.retry_queue_item("Invoices", "ghost")["status"] == "Pending"
        assert client.set_schedule_enabled("ghost", True)["enabled"] is True


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Guard: fail loudly if any test reaches a real socket via requests."""
    import requests

    def _boom(*_a, **_k):  # pragma: no cover - only fires on a test bug
        raise AssertionError("a test attempted a real HTTP request")

    monkeypatch.setattr(requests.Session, "request", _boom)

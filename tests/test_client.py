"""Tests for the decoupled v7 client (Phases 1–2).

Ported from the dashboard's tests/test_providers.py, adapted for the
Streamlit-ectomy: state lives on a BPClient instance, so each test builds a
fresh client (no cross-test cache/token leakage to reset) and patches that
instance's requests.Session. The wire contract pinned here — OAuth2
client-credentials auth, token paging, deepObject filters, attempt-based item
writes — follows the official v7 API specs (see DESIGN.md's ground truth).
"""
from unittest.mock import MagicMock

import pytest

from blue_prism_mcp.client import BPClient
from blue_prism_mcp.config import BPConfig
from blue_prism_mcp.mock import MockBPClient


def make_config(**overrides) -> BPConfig:
    base = dict(
        base_url="https://bp.example/api/v7",
        auth_url="https://auth.example",
        client_id="svc-client",
        client_secret="s3cret",
    )
    base.update(overrides)
    return BPConfig(**base)


def _resp(body, status_code: int = 200) -> MagicMock:
    return MagicMock(
        status_code=status_code,
        json=MagicMock(return_value=body),
        raise_for_status=MagicMock(),
    )


def _auth_resp(token: str = "test-token", expires_in: int = 3600) -> MagicMock:
    return _resp({"access_token": token, "expires_in": expires_in})


def make_client(**config_overrides) -> tuple[BPClient, MagicMock]:
    """A BPClient wired to a fully-mocked session, pre-stubbed for auth."""
    session = MagicMock()
    session.post.return_value = _auth_resp()
    client = BPClient(make_config(**config_overrides), session=session)
    return client, session


def prime_token(client: BPClient, token: str = "t") -> None:
    """Give the client a live token so a test exercises only the request path."""
    client._token = token
    client._token_expiry = float("inf")


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


# --- Live client: auth (mocked HTTP) -------------------------------------------


class TestAuth:
    """OAuth2 client-credentials against the Authentication Server."""

    def test_token_request_is_client_credentials_form_post(self):
        client, session = make_client()
        client._get_token()
        call = session.post.call_args
        assert call.args[0] == "https://auth.example/connect/token"
        assert call.kwargs["data"] == {
            "grant_type": "client_credentials",
            "client_id": "svc-client",
            "client_secret": "s3cret",
            "scope": "bp-api",
        }

    def test_token_caches_until_expiry(self):
        client, session = make_client()
        session.post.return_value = _auth_resp("cached-token")
        assert client._get_token() == "cached-token"
        client._get_token()
        session.post.assert_called_once()

    def test_expired_token_is_refetched(self):
        client, session = make_client()
        client._token = "stale"
        client._token_expiry = 0.0  # already past
        session.post.return_value = _auth_resp("fresh")
        assert client._get_token() == "fresh"

    def test_token_without_expires_in_is_trusted_until_401(self):
        client, session = make_client()
        session.post.return_value = _resp({"access_token": "no-expiry"})
        client._get_token()
        assert client._token_expiry == float("inf")

    def test_token_refresh_on_401(self):
        client, session = make_client()
        prime_token(client, "expired-token")  # live by expiry, rejected by server
        session.post.return_value = _auth_resp("new-token")
        session.get.side_effect = [_resp(None, 401), _resp([{"name": "BOT-01"}])]
        assert client.get_resources() == [{"name": "BOT-01"}]
        assert client._token == "new-token"

    def test_two_clients_do_not_share_token(self):
        a, _ = make_client()
        b, _ = make_client()
        a._token = "token-a"
        assert b._token is None


# --- Live client: reads and cache (mocked HTTP) ---------------------------------


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

    def test_get_sessions_passes_deepobject_date_params(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_sessions(start_date="2026-03-01", end_date="2026-03-07")
        params = session.get.call_args.kwargs["params"]
        assert params["startTime[gte]"] == "2026-03-01"
        assert params["startTime[lte]"] == "2026-03-07"

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


# --- Pagination (_get_collection) ---------------------------------------------


class TestPagination:
    """Token paging is the v7 default; offset/auto stay as config escape hatches."""

    def test_plain_list_single_page(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": 1}, {"id": 2}])
        assert client._get_collection("/resources") == [{"id": 1}, {"id": 2}]
        assert session.get.call_count == 1  # no token → stop

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

    def test_page_size_param_is_items_per_page(self):
        client, session = make_client(page_size=500)
        session.get.return_value = _resp({"items": []})
        client._get_collection("/sessions")
        assert session.get.call_args.kwargs["params"]["itemsPerPage"] == 500

    def test_auto_detects_offset_and_pages_until_short_page(self):
        client, session = make_client(paging_mode="auto", page_size=3)
        full = [{"id": i} for i in range(3)]
        session.get.side_effect = [_resp(full), _resp([{"id": 999}])]
        result = client._get_collection("/sessions")
        assert len(result) == 4
        assert session.get.call_count == 2

    def test_max_pages_cap(self):
        client, session = make_client(paging_mode="offset", page_size=3, max_pages=2)
        session.get.return_value = _resp([{"id": i} for i in range(3)])  # always full
        result = client._get_collection("/sessions")
        assert session.get.call_count == 2  # capped
        assert len(result) == 6

    def test_auto_detects_token_paging_from_present_empty_token(self):
        # A token endpoint marks its last page with an empty token. The page is
        # full (== page_size), so a truthiness-based guess would misread it as
        # offset and issue a spurious follow-up. Presence of the key → token,
        # and the empty value stops the loop after a single request.
        client, session = make_client(paging_mode="auto", page_size=2)
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
        client, session = make_client(paging_mode="offset", page_size=2)
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
            "q-uuid-1",
            state="Exceptioned",
            start_date="2026-03-01",
            end_date="2026-03-07",
        )
        call = session.get.call_args
        assert call.args[0].endswith("/workqueues/q-uuid-1/items")
        params = call.kwargs["params"]
        assert params["state"] == "Exceptioned"
        assert params["lastUpdated[gte]"] == "2026-03-01"
        assert params["lastUpdated[lte]"] == "2026-03-07"

    def test_get_queue_items_status_is_a_string_filter(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_queue_items("q-uuid-1", status="awaiting review")
        assert session.get.call_args.kwargs["params"]["status[eq]"] == "awaiting review"

    def test_get_queue_items_without_filters_sends_no_filter_params(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "i1"}])
        assert client.get_queue_items("q-uuid-1") == [{"id": "i1"}]
        params = session.get.call_args.kwargs["params"]  # paging adds itemsPerPage only
        assert "state" not in params
        assert "status[eq]" not in params
        assert "lastUpdated[gte]" not in params

    def test_get_queue_items_caches_per_filter(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "i1"}])
        client.get_queue_items("Q", state="Pending")
        client.get_queue_items("Q", state="Exceptioned")
        assert session.get.call_count == 2  # distinct cache keys, not shared

    def test_get_session_log(self):
        client, session = make_client()
        session.get.return_value = _resp([{"stage": "Start"}])
        assert client.get_session_log("sess-1") == [{"stage": "Start"}]
        assert session.get.call_args.args[0].endswith("/sessions/sess-1/logs")


# --- Phase 2: Tier 3 writes (mocked HTTP) -------------------------------------


class TestTierThreeWrites:
    def test_retry_creates_a_new_attempt(self):
        client, session = make_client()
        prime_token(client)
        session.post.return_value = _resp({"attemptId": 2}, 201)
        assert client.retry_queue_item("q1", "item-1") == {"attemptId": 2}
        assert session.post.call_args.args[0].endswith(
            "/workqueues/q1/items/item-1/attempts"
        )

    def test_defer_patches_the_attempt_with_json_patch(self):
        client, session = make_client()
        prime_token(client)
        session.patch.return_value = _resp(None, 204)
        result = client.defer_queue_item("q1", "item-1", 2, "2026-04-01T00:00:00Z")
        assert result is None  # 204 — no body
        call = session.patch.call_args
        assert call.args[0].endswith("/workqueues/q1/items/item-1/attempts/2")
        assert call.kwargs["json"] == [
            {"op": "replace", "path": "/deferredDate", "value": "2026-04-01T00:00:00Z"}
        ]

    def test_start_process_creates_then_runs_the_session(self):
        client, session = make_client()
        prime_token(client)
        session.post.return_value = _resp("sess-uuid-1")  # bare UUID per spec
        session.patch.return_value = _resp(None, 204)
        result = client.start_process("proc-1", "res-1")
        assert result == {"sessionId": "sess-uuid-1", "status": "Running"}
        assert session.post.call_args.kwargs["json"] == {
            "processId": "proc-1",
            "resourceId": "res-1",
        }
        patch_call = session.patch.call_args
        assert patch_call.args[0].endswith("/sessions/sess-uuid-1")
        assert patch_call.kwargs["json"] == {"status": "Running"}

    def test_set_schedule_enabled_maps_to_retirement(self):
        client, session = make_client()
        prime_token(client)
        session.put.return_value = _resp(None, 200)
        client.set_schedule_enabled("sched-1", False)
        assert session.put.call_args.kwargs["json"] == {"isRetired": True}
        assert session.put.call_args.args[0].endswith("/schedules/sched-1")

    def test_trigger_schedule_posts_runs(self):
        client, session = make_client()
        prime_token(client)
        session.post.return_value = _resp({"scheduleId": 1}, 202)
        client.trigger_schedule("sched-1")
        assert session.post.call_args.args[0].endswith("/schedules/sched-1/runs")
        assert session.post.call_args.kwargs["json"] == {}

    def test_trigger_schedule_passes_start_time(self):
        client, session = make_client()
        prime_token(client)
        session.post.return_value = _resp({}, 202)
        client.trigger_schedule("sched-1", start_time="2026-06-10T09:00:00Z")
        assert session.post.call_args.kwargs["json"] == {
            "startTime": "2026-06-10T09:00:00Z"
        }

    def test_empty_response_body_returns_none(self):
        client, session = make_client()
        prime_token(client)
        empty = MagicMock(status_code=200, content=b"", raise_for_status=MagicMock())
        session.put.return_value = empty
        assert client.set_schedule_enabled("sched-1", True) is None
        empty.json.assert_not_called()

    def test_write_invalidates_read_cache(self):
        client, session = make_client()
        session.get.return_value = _resp([{"name": "Q1"}])
        client.get_queues()  # populate cache
        prime_token(client)
        session.post.return_value = _resp({}, 202)
        client.trigger_schedule("s1")  # write must drop the cache
        client.get_queues()
        assert session.get.call_count == 2  # refetched after the write

    def test_write_reauths_on_401(self):
        client, session = make_client()
        prime_token(client, "expired")  # live by expiry, rejected by the server
        session.post.side_effect = [
            _resp(None, 401),  # write rejected — token expired
            _auth_resp("new"),  # re-auth
            _resp({"ok": True}, 202),  # write retried
        ]
        assert client.trigger_schedule("s1") == {"ok": True}
        assert client._token == "new"


# --- Phase 2: mock client extensions ------------------------------------------


class TestMockExtended:
    def test_get_processes(self):
        assert len(MockBPClient().get_processes()) > 0

    def test_get_queue_items_filters_by_queue_state_and_dates(self):
        client = MockBPClient()
        items = client.get_queue_items(
            "Invoices", state="Exceptioned", start_date="2026-03-01", end_date="2026-03-31"
        )
        assert items and all(i["queue"] == "Invoices" for i in items)
        assert all(i["state"] == "Exceptioned" for i in items)

    def test_get_queue_items_filters_by_user_status_text(self):
        client = MockBPClient(
            queue_items=[
                {"queue": "Q", "id": "i1", "state": "Pending", "status": "awaiting review"},
                {"queue": "Q", "id": "i2", "state": "Pending", "status": ""},
            ]
        )
        items = client.get_queue_items("Q", status="awaiting review")
        assert [i["id"] for i in items] == ["i1"]

    def test_get_queue_items_unknown_queue_is_empty(self):
        assert MockBPClient().get_queue_items("Nope") == []

    def test_get_session_log(self):
        log = MockBPClient().get_session_log("sess-002")
        assert any(e["result"] == "Exception" for e in log)

    def test_get_session_log_unknown_session_is_empty(self):
        assert MockBPClient().get_session_log("nope") == []

    def test_retry_queue_item_creates_attempt_and_flips_state(self):
        client = MockBPClient()
        result = client.retry_queue_item("Invoices", "item-002")
        assert result == {"attemptId": 2}
        pending = client.get_queue_items("Invoices", state="Pending")
        assert any(i["id"] == "item-002" for i in pending)

    def test_defer_queue_item_records_deferred_date(self):
        client = MockBPClient()
        assert client.defer_queue_item("Invoices", "item-001", 1, "2026-04-01T00:00:00Z") is None
        deferred = client.get_queue_items("Invoices", state="Deferred")
        assert deferred[0]["deferredDate"] == "2026-04-01T00:00:00Z"

    def test_start_process_appends_running_session(self):
        client = MockBPClient()
        before = len(client.get_sessions())
        result = client.start_process("proc-001", "BOT-01")
        assert result["status"] == "Running"
        assert len(client.get_sessions()) == before + 1

    def test_set_schedule_enabled_flips_retirement(self):
        client = MockBPClient()
        client.set_schedule_enabled("Daily Invoice Run", False)
        sched = [s for s in client.get_schedules() if s["name"] == "Daily Invoice Run"][0]
        assert sched["isRetired"] is True

    def test_trigger_schedule_records_outcome(self):
        client = MockBPClient()
        client.trigger_schedule("Daily Invoice Run")
        sched = [s for s in client.get_schedules() if s["name"] == "Daily Invoice Run"][0]
        assert sched["lastOutcome"] == "Triggered"

    def test_write_on_unknown_target_is_safe(self):
        client = MockBPClient()
        # No matching item/schedule — answers None without raising or mutating.
        assert client.retry_queue_item("Invoices", "ghost") is None
        assert client.set_schedule_enabled("ghost", True) is None
        assert client.trigger_schedule("ghost") is None

    def test_instances_do_not_share_fixture_mutations(self):
        # A write on one instance must not leak into a fresh instance via the
        # module-level default fixtures.
        MockBPClient().set_schedule_enabled("Daily Invoice Run", False)
        fresh = [
            s for s in MockBPClient().get_schedules() if s["name"] == "Daily Invoice Run"
        ][0]
        assert fresh["isRetired"] is False


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Guard: fail loudly if any test reaches a real socket via requests."""
    import requests

    def _boom(*_a, **_k):  # pragma: no cover - only fires on a test bug
        raise AssertionError("a test attempted a real HTTP request")

    monkeypatch.setattr(requests.Session, "request", _boom)

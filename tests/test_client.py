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
import requests

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


def _http_error_resp(status_code: int) -> MagicMock:
    """A response whose raise_for_status raises like a real requests one."""
    resp = MagicMock(status_code=status_code)
    error = requests.HTTPError(f"{status_code} error", response=resp)
    resp.raise_for_status = MagicMock(side_effect=error)
    return resp


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
        required = {"sessionId", "processName", "resourceName", "status", "startTime"}
        for s in sessions:
            assert required.issubset(s.keys())

    def test_get_sessions_with_date_filter(self):
        client = MockBPClient()
        all_sessions = client.get_sessions()
        dates = sorted({s["startTime"][:10] for s in all_sessions})
        mid = dates[len(dates) // 2]
        filtered = client.get_sessions(start_date=mid)
        assert all(s["startTime"][:10] >= mid for s in filtered)
        assert len(filtered) <= len(all_sessions)

    def test_get_sessions_with_end_date_filter(self):
        # A date-only end bound includes the whole day: a session at
        # 2026-03-02T10:00 is within end_date 2026-03-02 even though the raw
        # string compares greater.
        client = MockBPClient()
        dates = sorted({s["startTime"][:10] for s in client.get_sessions()})
        mid = dates[len(dates) // 2]
        filtered = client.get_sessions(end_date=mid)
        assert filtered
        assert all(s["startTime"][:10] <= mid for s in filtered)

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

    def test_a_caller_authorization_header_skips_the_token_fetch(self):
        # An explicit Authorization override (any casing) must not trigger a
        # needless token round-trip to the auth server.
        for header_key in ("Authorization", "authorization"):
            client, session = make_client()
            session.get.return_value = _resp([{"name": "BOT-01"}])
            result = client._request("GET", "/resources", headers={header_key: "Bearer ext"})
            assert result == [{"name": "BOT-01"}]
            session.post.assert_not_called()  # no call to the auth server
            assert session.get.call_args.kwargs["headers"] == {header_key: "Bearer ext"}

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

    def test_get_queue_hits_single_queue_path(self):
        client, session = make_client()
        session.get.return_value = _resp({"id": "q-uuid-1", "name": "Invoices"})
        assert client.get_queue("q-uuid-1") == {"id": "q-uuid-1", "name": "Invoices"}
        assert session.get.call_args.args[0].endswith("/workqueues/q-uuid-1")

    def test_get_queue_is_cached_per_id(self):
        client, session = make_client()
        session.get.return_value = _resp({"id": "q1"})
        client.get_queue("q1")
        client.get_queue("q1")
        session.get.assert_called_once()

    def test_get_current_limits_and_usage(self):
        client, session = make_client()
        session.get.return_value = _resp({"concurrentSessionsUsed": 3})
        assert client.get_current_limits_and_usage() == {"concurrentSessionsUsed": 3}
        assert session.get.call_args.args[0].endswith("/dashboards/currentLimitsAndUsage")
        client.get_current_limits_and_usage()
        session.get.assert_called_once()  # cached

    def test_get_user_permissions(self):
        # A flat array of permission-name strings (7.5.1 spec) — validated
        # strictly, cached like every read, consumed by the Phase 5
        # capability resolver at startup.
        client, session = make_client()
        session.get.return_value = _resp(["Execute Process", "Control Resource"])
        assert client.get_user_permissions() == ["Execute Process", "Control Resource"]
        assert session.get.call_args.args[0].endswith("/user/permissions")
        client.get_user_permissions()
        session.get.assert_called_once()  # cached

    def test_user_permissions_must_be_a_json_array(self):
        # Capability gating must never run over garbage: a gateway envelope
        # (or any dict) would silently iterate as its keys, so the shape
        # refuses loudly instead.
        client, session = make_client()
        session.get.return_value = _resp({"permissions": ["Edit Schedule"]})
        with pytest.raises(ValueError, match="returned dict"):
            client.get_user_permissions()

    def test_user_permissions_entries_must_be_non_empty_strings(self):
        client, session = make_client()
        session.get.return_value = _resp([{"name": "Edit Schedule"}, "  "])
        with pytest.raises(ValueError, match="non-empty strings"):
            client.get_user_permissions()


# --- Session log: the logslight probe ------------------------------------------


class TestSessionLogProbe:
    """logslight (7.4+) is shape-identical to /logs, so the client probes it
    first and only pins /logs when a 404 is corroborated by /logs succeeding —
    a bare 404 may just mean an unknown session id."""

    def test_logslight_is_preferred_when_available(self):
        client, session = make_client()
        session.get.return_value = _resp([{"stageName": "Start"}])
        assert client.get_session_log("sess-1") == [{"stageName": "Start"}]
        session.get.assert_called_once()
        assert session.get.call_args.args[0].endswith("/sessions/sess-1/logslight")

    def test_missing_logslight_falls_back_and_pins_logs(self):
        client, session = make_client()
        session.get.side_effect = [
            _http_error_resp(404),  # logslight absent (pre-7.4 estate)
            _resp([{"stageName": "Start"}]),  # /logs succeeds → pin
            _resp([{"stageName": "Start"}]),  # next read goes straight to /logs
        ]
        assert client.get_session_log("sess-1") == [{"stageName": "Start"}]
        fallback_url = session.get.call_args_list[1].args[0]
        assert fallback_url.endswith("/sessions/sess-1/logs")
        client.clear_cache()
        client.get_session_log("sess-1")
        assert session.get.call_count == 3  # no second probe after the pin
        assert session.get.call_args.args[0].endswith("/sessions/sess-1/logs")

    def test_unknown_session_raises_and_does_not_pin(self):
        # Both endpoints 404 on a bad session id; that must not demote a 7.4+
        # estate to /logs forever.
        client, session = make_client()
        session.get.side_effect = [
            _http_error_resp(404),  # logslight: session unknown
            _http_error_resp(404),  # /logs: session unknown too
            _resp([{"stageName": "Start"}]),  # later, a valid session
        ]
        with pytest.raises(requests.HTTPError):
            client.get_session_log("ghost")
        client.get_session_log("sess-1")  # probes logslight again
        assert session.get.call_args.args[0].endswith("/sessions/sess-1/logslight")

    def test_non_404_errors_propagate_without_fallback(self):
        client, session = make_client()
        session.get.side_effect = [_http_error_resp(500)]
        with pytest.raises(requests.HTTPError):
            client.get_session_log("sess-1")
        session.get.assert_called_once()  # no fallback attempt on a 500

    def test_an_httperror_without_a_response_propagates(self):
        # requests can raise HTTPError with response=None (e.g. from an
        # adapter); there is no status to inspect, so it must not be
        # swallowed by the 404 fallback.
        client, session = make_client()
        session.get.side_effect = requests.HTTPError("boom")
        with pytest.raises(requests.HTTPError, match="boom"):
            client.get_session_log("sess-1")


# --- Phase 2: Tier 3 writes (mocked HTTP) -------------------------------------


class TestTierThreeWrites:
    def test_retry_creates_a_new_attempt(self):
        client, session = make_client()
        prime_token(client)
        session.post.return_value = _resp({"attemptId": 2}, 201)
        assert client.retry_queue_item("q1", "item-1") == {"attemptId": 2}
        assert session.post.call_args.args[0].endswith("/workqueues/q1/items/item-1/attempts")

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
        # RFC 6902 media type — a patch list as plain application/json can be
        # rejected — without clobbering the auth header it merges over.
        assert call.kwargs["headers"]["Content-Type"] == "application/json-patch+json"
        assert call.kwargs["headers"]["Authorization"].startswith("Bearer ")

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
        assert session.post.call_args.kwargs["json"] == {"startTime": "2026-06-10T09:00:00Z"}

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


def _queue_id(client: MockBPClient, name: str = "Invoices") -> str:
    """The fixture queue's id — items are keyed by queue UUID, as on the API."""
    return next(q["id"] for q in client.get_queues() if q["name"] == name)


class TestMockExtended:
    def test_get_processes(self):
        processes = MockBPClient().get_processes()
        assert processes
        # Process is the one v7 entity keyed processId/processName, not id/name.
        assert all({"processId", "processName"}.issubset(p) for p in processes)

    def test_get_queue_returns_the_matching_queue(self):
        client = MockBPClient()
        qid = _queue_id(client)
        assert client.get_queue(qid)["name"] == "Invoices"

    def test_get_queue_unknown_id_raises(self):
        # The live endpoint 404s; the mock fails loudly too rather than
        # answering None.
        with pytest.raises(LookupError):
            MockBPClient().get_queue("no-such-queue")

    def test_get_current_limits_and_usage(self):
        usage = MockBPClient().get_current_limits_and_usage()
        assert "concurrentSessionsUsed" in usage
        assert "concurrentSessionsLimit" in usage

    def test_get_user_permissions_defaults_to_the_full_action_surface(self):
        permissions = MockBPClient().get_user_permissions()
        assert "Full Access to Queue Management" in permissions
        assert "Edit Schedule" in permissions

    def test_get_user_permissions_returns_a_copy_and_accepts_seeding(self):
        client = MockBPClient(permissions=["Edit Schedule"])
        client.get_user_permissions().append("INJECTED")
        assert client.get_user_permissions() == ["Edit Schedule"]

    def test_get_queue_items_filters_by_queue_state_and_dates(self):
        client = MockBPClient()
        qid = _queue_id(client)
        items = client.get_queue_items(
            qid, state="Exceptioned", start_date="2026-03-01", end_date="2026-03-31"
        )
        assert items and all(i["queue"] == qid for i in items)
        assert all(i["state"] == "Exceptioned" for i in items)

    def test_get_queue_items_end_date_includes_the_whole_day(self):
        # Items carry full timestamps; a date-only end bound must include
        # items updated later that same day.
        client = MockBPClient()
        items = client.get_queue_items(
            _queue_id(client), start_date="2026-03-02", end_date="2026-03-02"
        )
        assert [i["keyValue"] for i in items] == ["INV-1002"]

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
        client = MockBPClient()
        failed = next(s for s in client.get_sessions() if s["status"] == "Terminated")
        log = client.get_session_log(failed["sessionId"])
        assert any(str(e.get("result", "")).startswith("ERROR") for e in log)

    def test_get_session_log_unknown_session_is_empty(self):
        assert MockBPClient().get_session_log("nope") == []

    def test_retry_queue_item_creates_attempt_and_flips_state(self):
        client = MockBPClient()
        qid = _queue_id(client)
        exceptioned = client.get_queue_items(qid, state="Exceptioned")[0]
        result = client.retry_queue_item(qid, exceptioned["id"])
        assert result == {"attemptId": 2}
        pending = client.get_queue_items(qid, state="Pending")
        assert any(i["id"] == exceptioned["id"] for i in pending)

    def test_defer_queue_item_records_deferred_date(self):
        client = MockBPClient()
        qid = _queue_id(client)
        item = client.get_queue_items(qid)[0]["id"]
        assert client.defer_queue_item(qid, item, 1, "2026-04-01T00:00:00Z") is None
        deferred = client.get_queue_items(qid, state="Deferred")
        assert deferred[0]["deferredDate"] == "2026-04-01T00:00:00Z"

    def test_defer_queue_item_is_attempt_scoped(self):
        # The live endpoint is .../attempts/{attemptId}; a stale attempt id
        # must not mutate the item, so the mock refuses it too.
        client = MockBPClient()
        qid = _queue_id(client)
        item = client.get_queue_items(qid)[0]["id"]
        client.defer_queue_item(qid, item, 99, "2026-04-01T00:00:00Z")
        assert client.get_queue_items(qid, state="Deferred") == []

    def test_defer_of_an_unknown_item_is_a_noop(self):
        client = MockBPClient()
        qid = _queue_id(client)
        assert client.defer_queue_item(qid, "no-such-item", 1, "2026-04-01T00:00:00Z") is None
        assert client.get_queue_items(qid, state="Deferred") == []

    def test_defer_defaults_a_missing_attempt_number_to_one(self):
        # A simplified fixture without attemptNumber means attempt 1 — the
        # same default retry_queue_item uses when bumping.
        client = MockBPClient()
        qid = _queue_id(client)
        item = client.get_queue_items(qid)[0]["id"]
        del client._find_item(qid, item)["attemptNumber"]
        client.defer_queue_item(qid, item, 1, "2026-04-01T00:00:00Z")
        deferred = client.get_queue_items(qid, state="Deferred")
        assert [i["id"] for i in deferred] == [item]

    def test_defer_refuses_an_unparsable_attempt_number(self):
        client = MockBPClient()
        qid = _queue_id(client)
        item = client.get_queue_items(qid)[0]["id"]
        client._find_item(qid, item)["attemptNumber"] = "soon"
        client.defer_queue_item(qid, item, 1, "2026-04-01T00:00:00Z")
        assert client.get_queue_items(qid, state="Deferred") == []

    def test_defer_tracks_the_attempt_created_by_retry(self):
        # retry bumps the attempt number; deferring must address the NEW
        # attempt, and the old attempt id no longer works.
        client = MockBPClient()
        qid = _queue_id(client)
        item = client.get_queue_items(qid, state="Exceptioned")[0]["id"]
        new_attempt = client.retry_queue_item(qid, item)["attemptId"]
        client.defer_queue_item(qid, item, new_attempt - 1, "2026-04-01T00:00:00Z")
        assert client.get_queue_items(qid, state="Deferred") == []
        client.defer_queue_item(qid, item, new_attempt, "2026-04-01T00:00:00Z")
        deferred = client.get_queue_items(qid, state="Deferred")
        assert [i["id"] for i in deferred] == [item]

    def test_start_process_appends_running_session(self):
        client = MockBPClient()
        before = len(client.get_sessions())
        result = client.start_process("proc-001", "BOT-01")
        assert result["status"] == "Running"
        sessions = client.get_sessions()
        assert len(sessions) == before + 1
        assert sessions[-1]["status"] == "Running"

    def test_set_schedule_enabled_flips_retirement(self):
        client = MockBPClient()
        client.set_schedule_enabled("Daily Invoice Run", False)
        sched = [s for s in client.get_schedules() if s["name"] == "Daily Invoice Run"][0]
        assert sched["isRetired"] is True

    @pytest.mark.parametrize("schedule_id", ["1", 1], ids=["str", "int"])
    def test_schedule_lookup_matches_ids_across_types(self, schedule_id):
        # Fixture ids are integers (per ScheduleSummary), but the live client
        # takes schedule_id as a str for the URL path — both forms must find
        # the schedule rather than silently no-op.
        client = MockBPClient()
        client.set_schedule_enabled(schedule_id, False)
        sched = [s for s in client.get_schedules() if s["id"] == 1][0]
        assert sched["isRetired"] is True

    def test_trigger_schedule_records_outcome(self):
        client = MockBPClient()
        client.trigger_schedule("Daily Invoice Run")
        sched = [s for s in client.get_schedules() if s["name"] == "Daily Invoice Run"][0]
        assert sched["lastOutcome"] == "Triggered"

    def test_write_on_unknown_target_is_safe(self):
        client = MockBPClient()
        # No matching item/schedule — answers None without raising or mutating.
        assert client.retry_queue_item(_queue_id(client), "ghost") is None
        assert client.set_schedule_enabled("ghost", True) is None
        assert client.trigger_schedule("ghost") is None

    def test_instances_do_not_share_fixture_mutations(self):
        # A write on one instance must not leak into a fresh instance via the
        # module-level default fixtures.
        MockBPClient().set_schedule_enabled("Daily Invoice Run", False)
        fresh = [s for s in MockBPClient().get_schedules() if s["name"] == "Daily Invoice Run"][0]
        assert fresh["isRetired"] is False


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Guard: fail loudly if any test reaches a real socket via requests."""
    import requests

    def _boom(*_a, **_k):  # pragma: no cover - only fires on a test bug
        raise AssertionError("a test attempted a real HTTP request")

    monkeypatch.setattr(requests.Session, "request", _boom)

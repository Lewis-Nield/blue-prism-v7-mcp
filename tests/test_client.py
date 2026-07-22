"""Tests for the decoupled v7 client (Phases 1–2).

Ported from the dashboard's tests/test_providers.py, adapted for the
Streamlit-ectomy: state lives on a BPClient instance, so each test builds a
fresh client (no cross-test cache/token leakage to reset) and patches that
instance's requests.Session. The wire contract pinned here — OAuth2
client-credentials auth, token paging, deepObject filters, attempt-based item
writes — follows the official v7 API specs (see DESIGN.md's ground truth).
"""

import threading
import time
from unittest.mock import MagicMock

import pytest
import requests

from blue_prism_v7_mcp.cache import TTLCache
from blue_prism_v7_mcp.client import BPClient
from blue_prism_v7_mcp.config import BPConfig
from blue_prism_v7_mcp.mock import (
    _DEMO_HISTORY_DAYS,
    MockBPClient,
    _date,
    _ts,
    demo_estate,
)
from blue_prism_v7_mcp.transport import (
    RetryPolicy,
    TokenBucket,
    TransportBudgetExceeded,
)


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

    def test_get_sessions_filters_by_status(self):
        client = MockBPClient()
        filtered = client.get_sessions(status="Completed")
        assert filtered  # the fixture estate has completed runs
        assert {s["status"] for s in filtered} == {"Completed"}

    def test_get_sessions_filters_by_a_status_set_in_one_call(self):
        client = MockBPClient()
        wanted = {"Completed", "Terminated"}
        filtered = client.get_sessions(status=sorted(wanted))
        assert {s["status"] for s in filtered} == wanted

    def test_get_sessions_filters_by_process_and_resource_name(self):
        client = MockBPClient()
        sample = client.get_sessions()[0]
        by_process = client.get_sessions(process_name=sample["processName"])
        assert {s["processName"] for s in by_process} == {sample["processName"]}
        by_resource = client.get_sessions(resource_name=sample["resourceName"])
        assert {s["resourceName"] for s in by_resource} == {sample["resourceName"]}

    def test_get_sessions_name_filters_match_exactly_like_the_live_api(self):
        # The live filter is BasicStringFilter[eq] — exact. The mock must not
        # be lenient, or a missing canonicalisation upstream would pass here
        # and fail against a real estate.
        client = MockBPClient()
        sample = client.get_sessions()[0]
        assert client.get_sessions(process_name=sample["processName"].swapcase()) == []

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

    def test_context_topology_reads_have_the_right_shape(self):
        client = MockBPClient()
        configs = client.get_queue_configurations()
        assert configs and "activeWorkQueueConfiguration" in configs[0]
        pools = client.get_resource_pools()
        assert pools and {"id", "name", "members", "databaseStatus"} <= pools[0].keys()
        env_vars = client.get_environment_variables()
        assert env_vars and {"name", "dataType", "value"} <= env_vars[0].keys()
        groups = client.get_process_groups()
        assert groups and {n["nodeType"] for n in groups} == {"Group", "Item"}

    def test_context_topology_seeds_override_defaults(self):
        client = MockBPClient(
            queue_configurations=[{"name": "C"}],
            resource_pools=[{"name": "P"}],
            environment_variables=[{"name": "V"}],
            process_groups=[{"name": "G"}],
        )
        assert client.get_queue_configurations() == [{"name": "C"}]
        assert client.get_resource_pools() == [{"name": "P"}]
        assert client.get_environment_variables() == [{"name": "V"}]
        assert client.get_process_groups() == [{"name": "G"}]

    def test_get_resource_utilization_filters_from_start_date(self):
        client = MockBPClient()
        rows = client.get_resource_utilization(_date(1))
        assert rows and all(r["utilizationDate"] >= _date(1) for r in rows)
        assert any(r["utilizationDate"] == _date(0) for r in rows)
        assert not any(r["utilizationDate"] == _date(2) for r in rows)

    def test_get_resource_utilization_seed_overrides_default(self):
        client = MockBPClient(resource_utilization=[{"utilizationDate": "2020-01-01"}])
        assert client.get_resource_utilization("2020-01-01") == [{"utilizationDate": "2020-01-01"}]


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
            "scope": "bp-api bpserver",
        }

    def test_empty_token_scope_omits_the_scope_param(self):
        # An empty scope falls back to the auth guide's documented request shape:
        # no scope param, so the server issues the client's full allowed scope set.
        client, session = make_client(token_scope="")
        client._get_token()
        assert "scope" not in session.post.call_args.kwargs["data"]

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

    def test_expiry_is_the_stated_lifetime_less_the_refresh_skew(self):
        # The skew is the point: refresh BEFORE the server would reject, so a
        # token never goes stale mid-request.
        client, session = make_client()
        session.post.return_value = _auth_resp(expires_in=3600)
        before = time.monotonic()
        client._get_token()
        assert client._token_expiry == pytest.approx(before + 3600 - 60, abs=0.5)

    def test_a_lifetime_shorter_than_the_skew_still_leaves_a_positive_window(self):
        # Otherwise the subtraction goes negative and every single request
        # re-authenticates.
        client, session = make_client()
        session.post.return_value = _auth_resp(expires_in=5)
        before = time.monotonic()
        client._get_token()
        assert client._token_expiry == pytest.approx(before + 1.0, abs=0.5)

    def test_the_token_post_honours_the_configured_tls_and_timeout(self):
        # The auth server is often an internal host with its own certificate
        # posture, and an untimed token POST can hang the whole client.
        client, session = make_client(verify_ssl=False, request_timeout=7.5)
        client._get_token()
        assert session.post.call_args.kwargs["verify"] is False
        assert session.post.call_args.kwargs["timeout"] == 7.5

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

    def test_get_sessions_unfiltered_request_is_unchanged(self):
        # The v0.17.0 filters are additive: a caller passing none must put the
        # exact same request on the wire as before they existed.
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_sessions(start_date="2026-03-01", end_date="2026-03-07")
        params = session.get.call_args.kwargs["params"]
        assert set(params) == {"startTime[gte]", "startTime[lte]", "itemsPerPage"}

    def test_get_sessions_sends_status_comma_joined_in_one_request(self):
        # style=form explode=false (7.5.1, unchanged on 7.1/7.2): a whole SET
        # of statuses is ONE param in ONE request — not one request per status.
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_sessions(status=["Running", "Stopping", "Warning"])
        assert session.get.call_count == 1
        assert session.get.call_args.kwargs["params"]["status"] == "Running,Stopping,Warning"

    def test_get_sessions_accepts_a_scalar_status(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_sessions(status="Running")
        assert session.get.call_args.kwargs["params"]["status"] == "Running"

    def test_get_sessions_sends_names_as_deepobject_eq(self):
        # BasicStringFilter, style=deepObject — NOT the comma-joined form the
        # status array uses. Two encodings in the one call.
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_sessions(process_name="HR Onboarding", resource_name="BOT-01")
        params = session.get.call_args.kwargs["params"]
        assert params["processName[eq]"] == "HR Onboarding"
        assert params["resourceName[eq]"] == "BOT-01"

    def test_get_sessions_status_set_order_shares_one_cache_entry(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_sessions(status=["Warning", "Running"])
        client.get_sessions(status=["Running", "Warning"])
        session.get.assert_called_once()  # sorted tuple key — same set, same entry

    def test_get_sessions_filter_combinations_get_separate_cache_entries(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_sessions(start_date="2026-03-01")
        client.get_sessions(start_date="2026-03-01", status="Running")
        client.get_sessions(start_date="2026-03-01", process_name="HR Onboarding")
        client.get_sessions(start_date="2026-03-01", resource_name="BOT-01")
        assert session.get.call_count == 4

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

    def test_an_injected_cache_is_the_one_used(self):
        # The embeddable core (Phase 8) lets a host inject a shared cache; prove
        # the client reads/writes through the injected instance, not a default.
        from blue_prism_v7_mcp.cache import MISS

        class SpyCache:
            def __init__(self):
                self.store = {}
                self.gets, self.sets = 0, 0

            def get(self, key):
                self.gets += 1
                return self.store.get(key, MISS)

            def set(self, key, value):
                self.sets += 1
                self.store[key] = value

            def clear(self):
                self.store.clear()

        cache = SpyCache()
        session = MagicMock()
        session.post.return_value = _auth_resp()
        session.get.return_value = _resp([{"name": "BOT-01"}])
        client = BPClient(make_config(), session=session, cache=cache)
        client.get_resources()
        client.get_resources()  # served from the injected cache
        session.get.assert_called_once()
        # keys are namespaced by estate, so the bare label is nested under one
        assert cache.sets == 1 and cache.gets == 2
        assert any("resources" in key for key in cache.store)

    def test_a_shared_cache_does_not_leak_reads_across_estates(self):
        # Two clients for different estates sharing one injected store must not
        # serve estate A's reads to estate B — keys are namespaced by base_url.
        cache = TTLCache(ttl=30)
        session_a, session_b = MagicMock(), MagicMock()
        session_a.post.return_value = session_b.post.return_value = _auth_resp()
        session_a.get.return_value = _resp([{"name": "ESTATE-A-BOT"}])
        session_b.get.return_value = _resp([{"name": "ESTATE-B-BOT"}])
        client_a = BPClient(
            make_config(base_url="https://a.example/api/v7"), session=session_a, cache=cache
        )
        client_b = BPClient(
            make_config(base_url="https://b.example/api/v7"), session=session_b, cache=cache
        )
        assert client_a.get_resources()[0]["name"] == "ESTATE-A-BOT"
        assert client_b.get_resources()[0]["name"] == "ESTATE-B-BOT"  # not A's
        session_b.get.assert_called_once()  # B really fetched, not a cache hit


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

    def test_max_records_stops_paging_once_satisfied(self):
        client, session = make_client()
        session.get.side_effect = [
            _resp({"items": [{"id": 1}, {"id": 2}], "pagingToken": "p2"}),
            _resp({"items": [{"id": 3}, {"id": 4}], "pagingToken": "p3"}),
            _resp({"items": [{"id": 5}]}),
        ]
        result = client._get_collection("/sessions", max_records=2)
        assert result == [{"id": 1}, {"id": 2}]
        assert session.get.call_count == 1  # first page already satisfies max_records

    def test_max_records_does_not_slice_within_a_page(self):
        # A fetch-time cap, not a truncation: an overshooting page is kept whole.
        client, session = make_client()
        session.get.side_effect = [
            _resp({"items": [{"id": 1}, {"id": 2}, {"id": 3}], "pagingToken": "p2"}),
            _resp({"items": [{"id": 4}]}),
        ]
        result = client._get_collection("/sessions", max_records=1)
        assert result == [{"id": 1}, {"id": 2}, {"id": 3}]
        assert session.get.call_count == 1

    def test_max_records_none_pages_to_exhaustion_as_before(self):
        client, session = make_client()
        session.get.side_effect = [
            _resp({"items": [{"id": 1}], "pagingToken": "p2"}),
            _resp({"items": [{"id": 2}]}),
        ]
        result = client._get_collection("/sessions", max_records=None)
        assert result == [{"id": 1}, {"id": 2}]
        assert session.get.call_count == 2

    def test_page_size_override_replaces_the_configured_size(self):
        client, session = make_client(page_size=500)
        session.get.return_value = _resp({"items": [{"id": 1}]})
        client._get_collection("/sessions", page_size=5)
        assert session.get.call_args.kwargs["params"]["itemsPerPage"] == 5

    def test_page_size_override_omitted_uses_the_configured_size(self):
        client, session = make_client(page_size=500)
        session.get.return_value = _resp({"items": [{"id": 1}]})
        client._get_collection("/sessions")
        assert session.get.call_args.kwargs["params"]["itemsPerPage"] == 500

    def test_page_size_override_sets_the_offset_short_page_threshold(self):
        # A short page ends offset paging — "short" must mean short of the
        # OVERRIDE, not of the configured size, or the loop never terminates.
        client, session = make_client(paging_mode="offset", page_size=100)
        session.get.side_effect = [_resp([{"id": 1}, {"id": 2}]), _resp([{"id": 3}])]
        result = client._get_collection("/sessions", page_size=2)
        assert result == [{"id": 1}, {"id": 2}, {"id": 3}]
        assert session.get.call_count == 2


class TestPageNumberPagination:
    """_get_paged_by_number — the resourceUtilization-only pageNumber/pageSize scheme."""

    def test_single_short_page_stops_immediately(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [{"id": 1}]})
        result = client._get_paged_by_number("/dashboards/resourceUtilization", page_size=5)
        assert result == [{"id": 1}]
        assert session.get.call_count == 1

    def test_follows_full_pages_until_a_short_one(self):
        client, session = make_client()
        session.get.side_effect = [
            _resp({"items": [{"id": 1}, {"id": 2}]}),
            _resp({"items": [{"id": 3}]}),
        ]
        result = client._get_paged_by_number("/dashboards/resourceUtilization", page_size=2)
        assert result == [{"id": 1}, {"id": 2}, {"id": 3}]
        assert session.get.call_count == 2

    def test_page_number_starts_at_one_and_increments(self):
        # The client mutates and reuses one params dict across calls, so the
        # params must be snapshotted (copied) at call time, not read back off
        # call_args_list afterwards (which would all alias the final state).
        client, session = make_client()
        seen_page_numbers: list[int] = []
        seen_page_sizes: list[int] = []

        def _answer(url, headers, params, json, verify, timeout):
            seen_page_numbers.append(params["pageNumber"])
            seen_page_sizes.append(params["pageSize"])
            return _resp(
                {"items": [{"id": 1}, {"id": 2}]} if len(seen_page_numbers) == 1 else {"items": []}
            )

        session.get.side_effect = _answer
        client._get_paged_by_number("/dashboards/resourceUtilization", page_size=2)
        assert seen_page_numbers == [1, 2]
        assert seen_page_sizes == [2, 2]

    def test_max_pages_cap(self):
        client, session = make_client(max_pages=2)
        session.get.return_value = _resp({"items": [{"id": 1}, {"id": 2}]})  # always full
        result = client._get_paged_by_number("/dashboards/resourceUtilization", page_size=2)
        assert session.get.call_count == 2
        assert len(result) == 4

    def test_base_params_pass_through(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": []})
        client._get_paged_by_number(
            "/dashboards/resourceUtilization", base_params={"startDate": "2026-01-01"}
        )
        assert session.get.call_args.kwargs["params"]["startDate"] == "2026-01-01"


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

    def test_get_queue_items_within_sla_sends_equals_filter(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_queue_items("q-uuid-1", within_sla=False)
        assert session.get.call_args.kwargs["params"]["withinSla[eq]"] == "false"

        session.get.return_value = _resp([])
        client.get_queue_items("q-uuid-1", within_sla=True)
        assert session.get.call_args.kwargs["params"]["withinSla[eq]"] == "true"

    def test_get_queue_items_sla_before_sends_range_upper_bound(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_queue_items("q-uuid-1", sla_before="2026-03-07T09:30:00Z")
        params = session.get.call_args.kwargs["params"]
        assert params["slaDateTime[lte]"] == "2026-03-07T09:30:00Z"

    def test_get_queue_items_sort_by_passes_through_raw(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_queue_items("q-uuid-1", sort_by="LoadedDateAsc")
        assert session.get.call_args.kwargs["params"]["sortBy"] == "LoadedDateAsc"

    def test_get_queue_items_max_records_stops_paging_early(self):
        client, session = make_client()
        session.get.side_effect = [
            _resp({"items": [{"id": "i1"}], "pagingToken": "p2"}),
            _resp({"items": [{"id": "i2"}]}),
        ]
        result = client.get_queue_items("q-uuid-1", sort_by="LoadedDateAsc", max_records=1)
        assert result == [{"id": "i1"}]
        assert session.get.call_count == 1

    def test_get_queue_items_caches_per_max_records(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "i1"}])
        client.get_queue_items("Q", max_records=1)
        client.get_queue_items("Q", max_records=5)
        assert session.get.call_count == 2  # distinct cache keys, not shared

    def test_get_queue_items_max_records_slices_an_overshooting_page(self):
        # The one request that satisfies max_records may still hold more rows
        # than asked for (a fetch-time cap, not a truncation at that layer) —
        # get_queue_items must slice the result down to exactly max_records.
        client, session = make_client()
        session.get.return_value = _resp([{"id": "i1"}, {"id": "i2"}, {"id": "i3"}])
        result = client.get_queue_items("q-uuid-1", sort_by="LoadedDateAsc", max_records=1)
        assert result == [{"id": "i1"}]
        assert session.get.call_count == 1

    def test_get_queue_items_shrinks_the_page_to_a_sorted_max_records(self):
        client, session = make_client(page_size=1000)
        session.get.return_value = _resp([{"id": "i1"}])
        client.get_queue_items("q-uuid-1", sort_by="LoadedDateAsc", max_records=1)
        assert session.get.call_args.kwargs["params"]["itemsPerPage"] == 1

    def test_get_queue_items_max_records_without_sort_does_not_shrink_the_page(self):
        # The v0.15.0 guard applied to page size: unsorted, an early-stopped
        # fetch is an arbitrary subset, and a smaller page only shrinks it.
        client, session = make_client(page_size=1000)
        session.get.return_value = _resp([{"id": "i1"}])
        client.get_queue_items("q-uuid-1", max_records=1)
        assert session.get.call_args.kwargs["params"]["itemsPerPage"] == 1000

    def test_get_queue_items_without_sla_params_sends_no_sla_params(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "i1"}])
        client.get_queue_items("q-uuid-1")
        params = session.get.call_args.kwargs["params"]
        assert "withinSla[eq]" not in params
        assert "slaDateTime[lte]" not in params
        assert "sortBy" not in params

    def test_get_queue_items_caches_per_sla_filter(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "i1"}])
        client.get_queue_items("Q", within_sla=False)
        client.get_queue_items("Q", within_sla=True)
        client.get_queue_items("Q", sla_before="2026-03-07")
        client.get_queue_items("Q", sort_by="LoadedDateAsc")
        assert session.get.call_count == 4  # each is a distinct cache key

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

    def test_get_license_entitlement(self):
        client, session = make_client()
        session.get.return_value = _resp({"activeLicenseTypes": ["Enterprise"]})
        assert client.get_license_entitlement() == {"activeLicenseTypes": ["Enterprise"]}
        assert session.get.call_args.args[0].endswith("/dashboards/licensesEntitlement")
        client.get_license_entitlement()
        session.get.assert_called_once()  # cached

    def test_get_resource_utilization(self):
        client, session = make_client()
        session.get.return_value = _resp(
            {
                "items": [
                    {
                        "resourceId": "r1",
                        "digitalWorkerName": "BOT-01",
                        "utilizationDate": "2026-06-01",
                        "usages": [0] * 24,
                    }
                ]
            }
        )
        result = client.get_resource_utilization("2026-06-01")
        assert result == [
            {
                "resourceId": "r1",
                "digitalWorkerName": "BOT-01",
                "utilizationDate": "2026-06-01",
                "usages": [0] * 24,
            }
        ]
        assert session.get.call_args.args[0].endswith("/dashboards/resourceUtilization")
        assert session.get.call_args.kwargs["params"]["startDate"] == "2026-06-01"
        assert session.get.call_args.kwargs["params"]["pageNumber"] == 1
        client.get_resource_utilization("2026-06-01")
        session.get.assert_called_once()  # cached

    def test_get_queue_configurations(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [{"id": "q1", "name": "Invoices"}]})
        assert client.get_queue_configurations() == [{"id": "q1", "name": "Invoices"}]
        assert session.get.call_args.args[0].endswith("/workqueues/configurations")
        client.get_queue_configurations()
        session.get.assert_called_once()  # cached

    def test_get_resource_pools_is_a_bare_unpaged_array(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "p1", "name": "Pool", "members": 3}])
        assert client.get_resource_pools() == [{"id": "p1", "name": "Pool", "members": 3}]
        assert session.get.call_args.args[0].endswith("/resources/pools")
        client.get_resource_pools()
        session.get.assert_called_once()  # cached

    def test_get_resource_pools_coerces_empty_body_to_list(self):
        # The endpoint answers a bare array, so a 204/empty body must still
        # return a list rather than None for the tool layer to iterate.
        client, session = make_client()
        session.get.return_value = _resp(None, status_code=204)
        assert client.get_resource_pools() == []

    def test_get_environment_variables(self):
        client, session = make_client()
        session.get.return_value = _resp(
            {"items": [{"id": "v1", "name": "Mailbox", "dataType": "Text", "value": "x"}]}
        )
        assert client.get_environment_variables() == [
            {"id": "v1", "name": "Mailbox", "dataType": "Text", "value": "x"}
        ]
        assert session.get.call_args.args[0].endswith("/environmentvariables")
        client.get_environment_variables()
        session.get.assert_called_once()  # cached

    def test_get_process_groups(self):
        client, session = make_client()
        session.get.return_value = _resp(
            {"items": [{"id": "g1", "name": "Finance", "nodeType": "Group"}]}
        )
        assert client.get_process_groups() == [{"id": "g1", "name": "Finance", "nodeType": "Group"}]
        assert session.get.call_args.args[0].endswith("/processgroups/root/descendants")
        client.get_process_groups()
        session.get.assert_called_once()  # cached

    def test_get_queue_compositions_sends_repeated_id_params(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "q1", "deferred": 3}])
        result = client.get_queue_compositions(["q1", "q2"])
        assert result == [{"id": "q1", "deferred": 3}]
        assert session.get.call_args.args[0].endswith("/dashboards/workQueueCompositions")
        # The array goes as the OpenAPI form/explode default (repeated key).
        assert session.get.call_args.kwargs["params"] == {"workQueueIds": ["q1", "q2"]}

    def test_get_queue_compositions_caches_per_id_set(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "q1"}])
        client.get_queue_compositions(["q1"])
        client.get_queue_compositions(["q1"])
        session.get.assert_called_once()  # same ids => one request

    def test_get_queue_compositions_empty_ids_makes_no_request(self):
        client, session = make_client()
        assert client.get_queue_compositions([]) == []
        session.get.assert_not_called()

    def test_get_queue_compositions_coerces_empty_body_to_list(self):
        # A 204/empty body makes _get return None; the method must still answer
        # a list so the tool layer never iterates None.
        client, session = make_client()
        session.get.return_value = _resp(None, status_code=204)
        assert client.get_queue_compositions(["q1"]) == []

    def test_get_queue_compositions_cache_key_is_order_insensitive(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": "q1"}])
        client.get_queue_compositions(["q1", "q2"])
        client.get_queue_compositions(["q2", "q1"])  # same set, reversed
        session.get.assert_called_once()  # one request, shared cache entry

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

    def test_get_queue_item_hits_queue_less_path_and_carries_data(self):
        # The single-item read uses the queue-less path (the item UUID is
        # globally unique) and returns WorkQueueItem WITH its `data` payload.
        client, session = make_client()
        session.get.return_value = _resp({"id": "i1", "data": {"rows": []}})
        assert client.get_queue_item("i1") == {"id": "i1", "data": {"rows": []}}
        assert session.get.call_args.args[0].endswith("/workqueues/items/i1")

    def test_get_queue_item_is_cached_per_id(self):
        client, session = make_client()
        session.get.return_value = _resp({"id": "i1"})
        client.get_queue_item("i1")
        client.get_queue_item("i1")
        session.get.assert_called_once()

    def test_get_item_attempts_hits_queue_scoped_path(self):
        # Attempts are queue-scoped (both ids in the path), unlike the
        # queue-less single-item read.
        client, session = make_client()
        session.get.return_value = _resp([{"attemptNumber": 1}, {"attemptNumber": 2}])
        assert client.get_item_attempts("q1", "i1") == [
            {"attemptNumber": 1},
            {"attemptNumber": 2},
        ]
        assert session.get.call_args.args[0].endswith("/workqueues/q1/items/i1/attempts")

    def test_get_item_attempts_caches_per_queue_and_item(self):
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_item_attempts("q1", "i1")
        client.get_item_attempts("q1", "i2")
        assert session.get.call_count == 2  # distinct cache keys, not shared

    def test_get_session_hits_single_session_path(self):
        client, session = make_client()
        session.get.return_value = _resp({"sessionId": "s1", "status": "Completed"})
        assert client.get_session("s1") == {"sessionId": "s1", "status": "Completed"}
        assert session.get.call_args.args[0].endswith("/sessions/s1")

    def test_get_session_is_cached_per_id(self):
        client, session = make_client()
        session.get.return_value = _resp({"sessionId": "s1"})
        client.get_session("s1")
        client.get_session("s1")
        session.get.assert_called_once()


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

    def test_filters_are_pushed_as_server_side_query_params(self):
        client, session = make_client()
        session.get.return_value = _resp([{"stageName": "Raise"}])
        client.get_session_log(
            "sess-1",
            errors_only=True,
            start_date="2026-03-02T10:00:00Z",
            end_date="2026-03-02T11:00:00Z",
        )
        params = session.get.call_args.kwargs["params"]
        assert params["sortBy"] == "LogNumberDesc"
        assert params["stageType"] == "Exception,Recover,Resume"
        assert params["resourceStartTime[gte]"] == "2026-03-02T10:00:00Z"
        assert params["resourceStartTime[lte]"] == "2026-03-02T11:00:00Z"

    def test_default_read_sends_sort_but_no_filters(self):
        client, session = make_client()
        session.get.return_value = _resp([{"stageName": "Start"}])
        client.get_session_log("sess-1")
        params = session.get.call_args.kwargs["params"]
        assert params["sortBy"] == "LogNumberDesc"
        assert "stageType" not in params
        assert "resourceStartTime[gte]" not in params

    def test_filters_take_part_in_the_cache_key(self):
        client, session = make_client()
        session.get.return_value = _resp([{"stageName": "Start"}])
        client.get_session_log("sess-1")
        client.get_session_log("sess-1", errors_only=True)
        assert session.get.call_count == 2  # a different filter set is a cache miss

    def test_filters_survive_the_logslight_to_logs_fallback(self):
        # The filters must reach /logs too — both on the 404 fallback and once
        # the instance has pinned /logs (a pre-7.4 estate must still narrow).
        client, session = make_client()
        session.get.side_effect = [
            _http_error_resp(404),  # logslight absent → fall back to /logs
            _resp([{"stageName": "Raise"}]),  # /logs (fallback) → pin
            _resp([{"stageName": "Raise"}]),  # next read goes straight to /logs
        ]
        client.get_session_log("sess-1", errors_only=True)
        fallback = session.get.call_args_list[1]
        assert fallback.args[0].endswith("/sessions/sess-1/logs")
        assert fallback.kwargs["params"]["stageType"] == "Exception,Recover,Resume"
        client.clear_cache()
        client.get_session_log("sess-1", errors_only=True)  # pinned /logs path
        assert session.get.call_args.kwargs["params"]["stageType"] == "Exception,Recover,Resume"


class TestScheduleRunLog:
    def test_reads_latest_run_from_the_current_endpoint(self):
        client, session = make_client()
        session.get.return_value = _resp(
            {"items": [{"scheduleLogId": 11, "status": "completed"}], "pagingToken": None}
        )
        run = client.get_last_schedule_run(1)
        assert run == {"scheduleLogId": 11, "status": "completed"}
        call = session.get.call_args
        assert call.args[0].endswith("/scheduleLogs/1")  # not the deprecated /schedules/{id}/logs
        assert call.kwargs["params"] == {"sortBy": "StartTimeDesc", "itemsPerPage": 1}

    def test_never_run_schedule_is_none(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [], "pagingToken": None})
        assert client.get_last_schedule_run(2) is None

    def test_is_cached_per_schedule_id(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [{"scheduleLogId": 1}]})
        client.get_last_schedule_run(1)
        client.get_last_schedule_run(1)
        session.get.assert_called_once()

    def test_distinct_schedule_ids_do_not_share_a_cache_entry(self):
        # The cache key carries the schedule id, so one schedule's last run is
        # never served for another.
        client, session = make_client()
        session.get.return_value = _resp({"items": [{"scheduleLogId": 1}]})
        client.get_last_schedule_run(1)
        client.get_last_schedule_run(2)
        assert session.get.call_count == 2


class TestScheduleDepthReads:
    """The v0.11.0 schedule reads: single definition, task chain, run history."""

    def test_get_schedule_reads_the_single_schedule_path(self):
        client, session = make_client()
        session.get.return_value = _resp(
            {"id": 1, "name": "Daily Invoice Run", "intervalType": "Day"}
        )
        detail = client.get_schedule(1)
        assert detail["intervalType"] == "Day"
        assert session.get.call_args.args[0].endswith("/schedules/1")

    def test_get_schedule_is_cached_per_id(self):
        client, session = make_client()
        session.get.return_value = _resp({"id": 1})
        client.get_schedule(1)
        client.get_schedule(1)
        session.get.assert_called_once()
        client.get_schedule(2)
        assert session.get.call_count == 2

    def test_get_schedule_tasks_answers_the_bare_array(self):
        client, session = make_client()
        session.get.return_value = _resp([{"id": 11, "name": "Process Invoices"}])
        tasks = client.get_schedule_tasks(1)
        assert tasks == [{"id": 11, "name": "Process Invoices"}]
        assert session.get.call_args.args[0].endswith("/schedules/1/tasks")

    def test_get_schedule_tasks_coerces_an_empty_body_to_a_list(self):
        client, session = make_client()
        session.get.return_value = _resp(None)
        assert client.get_schedule_tasks(1) == []

    def test_get_task_sessions_uses_the_schedule_less_path(self):
        # The 7.0 form: task ids are unique, so no schedule id in the path.
        client, session = make_client()
        session.get.return_value = _resp(
            [{"processName": "Invoice Processing", "resourceName": "BOT-01", "taskSessionId": 1}]
        )
        sessions = client.get_task_sessions(11)
        assert sessions[0]["processName"] == "Invoice Processing"
        assert session.get.call_args.args[0].endswith("/schedules/tasks/11/sessions")

    def test_get_task_sessions_coerces_an_empty_body_to_a_list(self):
        client, session = make_client()
        session.get.return_value = _resp(None)
        assert client.get_task_sessions(11) == []

    def test_schedule_logs_sweep_the_plural_endpoint_with_filters(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [{"scheduleLogId": 1}], "pagingToken": None})
        client.get_schedule_logs(
            status="Terminated",
            start_date="2026-06-01",
            end_date="2026-06-30",
        )
        call = session.get.call_args
        assert call.args[0].endswith("/scheduleLogs")  # the current family, estate-wide
        params = call.kwargs["params"]
        assert params["sortBy"] == "StartTimeDesc"
        assert params["scheduleLogStatus"] == "Terminated"  # Capitalised query enum
        assert params["startTime[gte]"] == "2026-06-01"
        assert params["startTime[lte]"] == "2026-06-30"

    def test_schedule_logs_scope_to_one_schedule_when_given(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [], "pagingToken": None})
        client.get_schedule_logs(schedule_id=3)
        assert session.get.call_args.args[0].endswith("/scheduleLogs/3")

    def test_schedule_logs_are_cached_per_filter_set(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [], "pagingToken": None})
        client.get_schedule_logs(status="Completed")
        client.get_schedule_logs(status="Completed")
        session.get.assert_called_once()
        client.get_schedule_logs(status="Terminated")
        assert session.get.call_count == 2

    def test_schedule_tasks_and_sessions_cache_per_id(self):
        # Distinct schedules/tasks must never share a cache entry — one
        # schedule's chain served for another would misroute a triage.
        client, session = make_client()
        session.get.return_value = _resp([])
        client.get_schedule_tasks(1)
        client.get_schedule_tasks(2)
        assert session.get.call_count == 2
        client.get_task_sessions(11)
        client.get_task_sessions(12)
        assert session.get.call_count == 4

    def test_schedule_logs_scoped_and_estate_wide_are_distinct_entries(self):
        client, session = make_client()
        session.get.return_value = _resp({"items": [], "pagingToken": None})
        client.get_schedule_logs(schedule_id=1)
        client.get_schedule_logs()  # the estate-wide sweep is not the scoped read
        assert session.get.call_count == 2


class TestLatestScheduleRuns:
    """The last-run sweep: one plural read for the estate, fallback for stragglers."""

    def test_one_sweep_covers_every_schedule_first_row_wins(self):
        client, session = make_client()
        session.get.return_value = _resp(
            {
                "items": [
                    {"scheduleId": 1, "scheduleLogId": 5, "startTime": "2026-07-01T06:00:00Z"},
                    {"scheduleId": 2, "scheduleLogId": 4, "startTime": "2026-07-01T02:00:00Z"},
                    # An older run for schedule 1 — newest-first means the FIRST
                    # row seen per schedule is its last run; this must not clobber.
                    {"scheduleId": 1, "scheduleLogId": 3, "startTime": "2026-06-30T06:00:00Z"},
                ],
                "pagingToken": None,
            }
        )
        runs = client.get_latest_schedule_runs([1, 2])
        assert runs["1"]["scheduleLogId"] == 5
        assert runs["2"]["scheduleLogId"] == 4
        session.get.assert_called_once()
        call = session.get.call_args
        assert call.args[0].endswith("/scheduleLogs")
        assert call.kwargs["params"]["sortBy"] == "StartTimeDesc"

    def test_rows_for_unwanted_schedules_are_ignored(self):
        client, session = make_client()
        session.get.side_effect = [
            # The sweep sees only another schedule's runs (not wanted)...
            _resp({"items": [{"scheduleId": 9, "scheduleLogId": 7}], "pagingToken": None}),
            # ...so the wanted schedule falls back per-schedule: never run.
            _resp({"items": [], "pagingToken": None}),
        ]
        assert client.get_latest_schedule_runs([1]) == {}

    def test_stops_early_once_every_wanted_schedule_is_seen(self):
        # Page 1 carries both wanted schedules AND a next-page token — the
        # sweep must stop on coverage, not walk the whole run history.
        client, session = make_client()
        session.get.return_value = _resp(
            {
                "items": [
                    {"scheduleId": 1, "scheduleLogId": 5},
                    {"scheduleId": 2, "scheduleLogId": 4},
                ],
                "pagingToken": "more",
            }
        )
        runs = client.get_latest_schedule_runs([1, 2])
        assert set(runs) == {"1", "2"}
        session.get.assert_called_once()

    def test_page_budget_exhausted_falls_back_per_schedule(self):
        # Three busy pages never reach dormant schedule 2 — the sweep stops at
        # its budget and only the straggler pays a per-schedule read.
        client, session = make_client()
        busy_page = _resp({"items": [{"scheduleId": 1, "scheduleLogId": 5}], "pagingToken": "t"})
        session.get.side_effect = [
            busy_page,
            busy_page,
            busy_page,
            _resp({"items": [{"scheduleId": 2, "scheduleLogId": 9}], "pagingToken": None}),
        ]
        runs = client.get_latest_schedule_runs([1, 2])
        assert runs["1"]["scheduleLogId"] == 5
        assert runs["2"]["scheduleLogId"] == 9
        assert session.get.call_count == 4
        assert session.get.call_args_list[3].args[0].endswith("/scheduleLogs/2")

    def test_never_run_schedule_is_absent_not_fabricated(self):
        client, session = make_client()
        session.get.side_effect = [
            _resp({"items": [{"scheduleId": 1, "scheduleLogId": 5}], "pagingToken": None}),
            # The straggler fallback for schedule 2 answers an empty page.
            _resp({"items": [], "pagingToken": None}),
        ]
        runs = client.get_latest_schedule_runs([1, 2])
        assert "2" not in runs and "1" in runs

    def test_empty_id_list_short_circuits_to_no_request(self):
        client, session = make_client()
        assert client.get_latest_schedule_runs([]) == {}
        assert client.get_latest_schedule_runs([None]) == {}
        session.get.assert_not_called()

    def test_cached_per_id_set_regardless_of_order(self):
        client, session = make_client()
        session.get.return_value = _resp(
            {"items": [{"scheduleId": 1}, {"scheduleId": 2}], "pagingToken": None}
        )
        client.get_latest_schedule_runs([1, 2])
        client.get_latest_schedule_runs([2, 1])
        session.get.assert_called_once()

    def test_distinct_id_sets_do_not_share_a_cache_entry(self):
        client, session = make_client()
        session.get.return_value = _resp(
            {"items": [{"scheduleId": 1}, {"scheduleId": 2}], "pagingToken": None}
        )
        client.get_latest_schedule_runs([1])
        client.get_latest_schedule_runs([1, 2])
        assert session.get.call_count == 2

    def test_paging_token_absent_on_page_one_and_forwarded_on_page_two(self):
        # The client reuses one params dict across the sweep, so snapshot the
        # params at call time: page 1 must carry NO pagingToken (a spurious
        # empty token is not the spec's first-page request), and page 2 must
        # forward exactly the token page 1 answered.
        client, session = make_client()
        prime_token(client)
        seen: list[dict] = []
        pages = iter(
            [
                _resp({"items": [{"scheduleId": 1, "scheduleLogId": 5}], "pagingToken": "t2"}),
                _resp({"items": [{"scheduleId": 2, "scheduleLogId": 4}], "pagingToken": None}),
            ]
        )

        def record(url, headers=None, params=None, **kwargs):
            seen.append(dict(params or {}))
            return next(pages)

        session.get.side_effect = record
        runs = client.get_latest_schedule_runs([1, 2])
        assert set(runs) == {"1", "2"}
        assert "pagingToken" not in seen[0]
        assert seen[1]["pagingToken"] == "t2"


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
        session.put.assert_not_called()  # no params → no parameters PUT

    def test_start_process_puts_parameters_before_running(self):
        client, session = make_client()
        prime_token(client)
        session.post.return_value = _resp("sess-uuid-1")
        session.put.return_value = _resp(None, 204)  # parameters PUT answers 204
        session.patch.return_value = _resp(None, 204)
        params = {"InvoiceDate": {"valueType": "Date", "value": "2026-03-01"}}
        client.start_process("proc-1", "res-1", parameters=params)
        put_call = session.put.call_args
        assert put_call.args[0].endswith("/sessions/sess-uuid-1/parameters")
        assert put_call.kwargs["json"] == {"parameters": params}

    def test_stop_session_patches_status_to_stopped(self):
        client, session = make_client()
        prime_token(client)
        session.patch.return_value = _resp(None, 202)
        result = client.stop_session("sess-uuid-1")
        assert result == {"sessionId": "sess-uuid-1", "status": "Stopped"}
        patch_call = session.patch.call_args
        assert patch_call.args[0].endswith("/sessions/sess-uuid-1")
        assert patch_call.kwargs["json"] == {"status": "Stopped"}

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

    def test_create_queue_items_posts_the_array_body(self):
        client, session = make_client()
        prime_token(client)
        session.post.return_value = _resp({"ids": ["id-1", "id-2"]}, 201)
        items = [{"data": {"rows": []}}, {"priority": 1}]
        result = client.create_queue_items("q-1", items)
        assert result == {"ids": ["id-1", "id-2"]}
        call = session.post.call_args
        assert call.args[0].endswith("/workqueues/q-1/items")
        assert call.kwargs["json"] == items

    def test_create_queue_items_clears_the_cache(self):
        client, session = make_client()
        prime_token(client)
        session.post.return_value = _resp({"ids": ["id-1"]}, 201)
        client._cache.set("resources", [{"id": "r1"}])
        client.create_queue_items("q-1", [{}])
        from blue_prism_v7_mcp.cache import MISS

        assert client._cache.get("resources") is MISS

    def test_stop_schedule_deletes_the_active_runs(self):
        # trigger_schedule's incident sibling: DELETE /schedules/{id}/runs/active,
        # answering 202 with no body (→ None).
        client, session = make_client()
        prime_token(client)
        session.delete.return_value = _resp(None, 202)
        assert client.stop_schedule("sched-1") is None
        assert session.delete.call_args.args[0].endswith("/schedules/sched-1/runs/active")

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

    def test_create_queue_items_unknown_queue_raises(self):
        with pytest.raises(ValueError, match="not found"):
            MockBPClient().create_queue_items("no-such-queue", [{}])

    def test_get_current_limits_and_usage(self):
        usage = MockBPClient().get_current_limits_and_usage()
        assert "concurrentSessionsUsed" in usage
        assert "concurrentSessionsLimit" in usage

    def test_get_license_entitlement(self):
        entitlement = MockBPClient().get_license_entitlement()
        assert entitlement["activeLicenseTypes"] == ["Enterprise"]
        assert entitlement["enterpriseEntitlement"]["concurrentsessionslimit"] == 10

    def test_get_queue_compositions_carries_deferred_for_known_queues(self):
        client = MockBPClient()
        qid = client.get_queues()[0]["id"]  # Invoices
        [row] = client.get_queue_compositions([qid])
        assert row["id"] == qid
        assert row["deferred"] == 3  # the datum WorkQueueSummary lacks

    def test_get_queue_compositions_skips_unknown_ids(self):
        assert MockBPClient().get_queue_compositions(["no-such-queue"]) == []

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
            qid, state="Exceptioned", start_date=_date(60), end_date=_date(0)
        )
        assert items and all(i["queue"] == qid for i in items)
        assert all(i["state"] == "Exceptioned" for i in items)

    def test_get_queue_items_end_date_includes_the_whole_day(self):
        # Items carry full timestamps; a date-only end bound must include
        # items updated later that same day. The exceptioned Invoices item sits
        # 7 days back, the completed one 8 — so a single-day window isolates it.
        client = MockBPClient()
        items = client.get_queue_items(_queue_id(client), start_date=_date(7), end_date=_date(7))
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

    def test_get_queue_items_within_sla_false_narrows_to_breached(self):
        # Every Invoices item's slaDatetime is in the past (both the
        # completed and the exceptioned one) — both read as breached.
        # The Onboarding item's slaDatetime is a day ahead — still within SLA.
        client = MockBPClient()
        breached = client.get_queue_items(_queue_id(client), within_sla=False)
        assert {i["keyValue"] for i in breached} == {"INV-1001", "INV-1002"}

        onboarding_qid = _queue_id(client, "Onboarding")
        ahead = client.get_queue_items(onboarding_qid, within_sla=True)
        assert ahead and all(i["keyValue"] == "CUST-0042" for i in ahead)
        assert client.get_queue_items(onboarding_qid, within_sla=False) == []

    def test_get_queue_items_within_sla_needs_no_date_window(self):
        client = MockBPClient()
        # No start_date/end_date at all — withinSla is scope enough.
        breached = client.get_queue_items(_queue_id(client), within_sla=False)
        assert breached

    def test_get_queue_items_sla_before_narrows_to_approaching_deadline(self):
        client = MockBPClient()
        qid = _queue_id(client)
        # The exceptioned item's slaDatetime sits 7 days back; a far-future
        # upper bound includes it, a far-past one excludes it.
        assert client.get_queue_items(qid, sla_before=_date(-3650))
        assert client.get_queue_items(qid, sla_before=_date(3650)) == []

    def test_get_queue_items_sort_by_loaded_date_asc(self):
        client = MockBPClient()
        items = client.get_queue_items(_queue_id(client), sort_by="LoadedDateAsc")
        loaded = [i["loadedDate"] for i in items]
        assert loaded == sorted(loaded)

    def test_get_queue_items_unknown_sort_by_is_a_no_op(self):
        client = MockBPClient()
        qid = _queue_id(client)
        assert client.get_queue_items(qid) == client.get_queue_items(qid, sort_by="Bogus")

    def test_get_queue_items_max_records_caps_the_oldest_first_slice(self):
        client = MockBPClient()
        qid = _queue_id(client)
        full = client.get_queue_items(qid, sort_by="LoadedDateAsc")
        capped = client.get_queue_items(qid, sort_by="LoadedDateAsc", max_records=1)
        assert capped == full[:1]

    def test_get_queue_items_max_records_none_returns_everything(self):
        client = MockBPClient()
        qid = _queue_id(client)
        assert client.get_queue_items(qid, max_records=None) == client.get_queue_items(qid)

    def test_get_queue_items_within_sla_true_includes_items_with_no_sla(self):
        # An item with no slaDatetime at all has nothing to breach — it reads
        # as within SLA (true), and is excluded from a breach (false) query.
        client = MockBPClient(queue_items=[{"queue": "Q", "id": "i1", "state": "Pending"}])
        assert [i["id"] for i in client.get_queue_items("Q", within_sla=True)] == ["i1"]
        assert client.get_queue_items("Q", within_sla=False) == []

    def test_get_session_log(self):
        client = MockBPClient()
        failed = next(s for s in client.get_sessions() if s["status"] == "Terminated")
        log = client.get_session_log(failed["sessionId"])
        # Newest-stage-first (sortBy=LogNumberDesc), and the failing run carries
        # an Exception stage.
        assert [e["logNumber"] for e in log] == sorted((e["logNumber"] for e in log), reverse=True)
        assert any(e.get("stageType") == "Exception" for e in log)

    def test_get_session_log_errors_only_keeps_exception_handling_stages(self):
        client = MockBPClient()
        failed = next(s for s in client.get_sessions() if s["status"] == "Terminated")
        log = client.get_session_log(failed["sessionId"], errors_only=True)
        assert log and all(e["stageType"] in {"Exception", "Recover", "Resume"} for e in log)

    def test_get_session_log_window_bounds_stage_time(self):
        client = MockBPClient()
        failed = next(s for s in client.get_sessions() if s["status"] == "Terminated")
        # The Exception stage runs at 10:01:05; a window ending 10:01 excludes it.
        early = client.get_session_log(
            failed["sessionId"], start_date=_ts(7, "10:00:00"), end_date=_ts(7, "10:01:00")
        )
        assert early and all(e["stageType"] != "Exception" for e in early)

    def test_get_session_log_unknown_session_is_empty(self):
        assert MockBPClient().get_session_log("nope") == []

    def test_get_last_schedule_run(self):
        client = MockBPClient()
        run = client.get_last_schedule_run(1)
        # The most recent run by startTime (the day-1 completion, not the
        # earlier day-4 one).
        assert run["startTime"] == _ts(1, "06:00:00")
        assert run["status"] == "completed"

    def test_get_last_schedule_run_picks_the_latest_regardless_of_order(self):
        # The selection is by startTime, not list position — seed the older run
        # last so a position-based pick would get it wrong.
        client = MockBPClient(
            schedule_logs={
                "1": [
                    {
                        "scheduleLogId": 9,
                        "startTime": "2026-03-06T06:00:00Z",
                        "status": "completed",
                    },
                    {
                        "scheduleLogId": 11,
                        "startTime": "2026-03-09T06:00:00Z",
                        "status": "terminated",
                    },
                ]
            }
        )
        run = client.get_last_schedule_run(1)
        assert run["startTime"] == "2026-03-09T06:00:00Z"
        assert run["status"] == "terminated"

    def test_get_last_schedule_run_never_run_is_none(self):
        client = MockBPClient(schedule_logs={})
        assert client.get_last_schedule_run(1) is None

    def test_get_latest_schedule_runs_keys_by_string_and_omits_never_run(self):
        runs = MockBPClient().get_latest_schedule_runs([1, 2, 9, None])
        assert set(runs) == {"1", "2"}  # 9 never ran; None is skipped
        assert runs["1"]["status"] == "completed"

    def test_get_schedule_returns_the_definition_and_is_strict_on_unknown(self):
        client = MockBPClient()
        detail = client.get_schedule(1)
        assert detail["name"] == "Daily Invoice Run"
        assert detail["dailyDetails"] == {"period": 1, "calendarId": 1}
        with pytest.raises(LookupError):
            client.get_schedule(99)

    def test_get_schedule_tasks_serves_copies_and_is_strict_on_unknown(self):
        client = MockBPClient()
        tasks = client.get_schedule_tasks(1)
        assert tasks and tasks[0]["name"] == "Process Invoices"
        tasks[0]["name"] = "INJECTED"
        assert client.get_schedule_tasks(1)[0]["name"] == "Process Invoices"
        with pytest.raises(LookupError):
            client.get_schedule_tasks(99)

    def test_get_task_sessions_answers_names_and_empty_on_unknown(self):
        client = MockBPClient()
        sessions = client.get_task_sessions(11)
        assert sessions == [
            {"processName": "Invoice Processing", "resourceName": "BOT-01", "taskSessionId": 111}
        ]
        assert client.get_task_sessions(999) == []

    def test_get_schedule_logs_sweeps_every_schedule_newest_first(self):
        rows = MockBPClient().get_schedule_logs()
        assert {r["scheduleId"] for r in rows} == {1, 2}
        starts = [r["startTime"] for r in rows]
        assert starts == sorted(starts, reverse=True)

    def test_get_schedule_logs_scopes_and_filters(self):
        client = MockBPClient()
        scoped = client.get_schedule_logs(schedule_id=2)
        assert scoped and all(r["scheduleId"] == 2 for r in scoped)
        # The query enum is Capitalised; response rows are lowercase — the
        # match is case-insensitive like the live server's own enum handling.
        failed = client.get_schedule_logs(status="Terminated")
        assert failed and all(r["status"] == "terminated" for r in failed)
        windowed = client.get_schedule_logs(start_date=_ts(2), end_date=_date(0))
        assert windowed and all(r["startTime"] >= _ts(2) for r in windowed)

    def test_get_queue_item_carries_data_and_drops_internal_queue_key(self):
        # The single-item read returns WorkQueueItem (WITH `data`); the
        # mock-internal `queue` plumbing key never surfaces.
        client = MockBPClient()
        qid = _queue_id(client)
        item = client.get_queue_items(qid, state="Exceptioned")[0]
        full = client.get_queue_item(item["id"])
        assert "data" in full and "rows" in full["data"]
        assert "queue" not in full

    def test_get_queue_item_defaults_to_an_empty_collection(self):
        # An item with no payload fixture still answers a `data` field, like
        # the live schema (which always carries one).
        client = MockBPClient()
        qid = _queue_id(client)
        plain = client.get_queue_items(qid, state="Completed")[0]
        assert client.get_queue_item(plain["id"])["data"] == {"rows": []}

    def test_get_queue_item_unknown_id_raises(self):
        with pytest.raises(LookupError):
            MockBPClient().get_queue_item("no-such-item")

    def test_get_item_attempts_returns_history_for_the_item(self):
        client = MockBPClient()
        qid = _queue_id(client)
        item = client.get_queue_items(qid, state="Exceptioned")[0]
        attempts = client.get_item_attempts(qid, item["id"])
        assert [a["attemptNumber"] for a in attempts] == [1, 2]

    def test_get_item_attempts_is_queue_scoped(self):
        # An item not in the named queue answers an empty history (the live
        # endpoint 404s on the mismatch).
        client = MockBPClient()
        item = client.get_queue_items(_queue_id(client), state="Exceptioned")[0]
        assert client.get_item_attempts("other-queue", item["id"]) == []

    def test_get_item_attempts_defaults_to_empty_history(self):
        client = MockBPClient()
        qid = _queue_id(client)
        plain = client.get_queue_items(qid, state="Completed")[0]
        assert client.get_item_attempts(qid, plain["id"]) == []

    def test_get_session_returns_the_matching_session(self):
        client = MockBPClient()
        sid = client.get_sessions()[0]["sessionId"]
        assert client.get_session(sid)["sessionId"] == sid

    def test_get_session_unknown_id_raises(self):
        # The live endpoint 404s; the mock fails loudly rather than None.
        with pytest.raises(LookupError):
            MockBPClient().get_session("no-such-session")

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

    def test_trigger_schedule_appends_running_log_row(self):
        client = MockBPClient()
        client.trigger_schedule("Daily Invoice Run")
        logs = client.get_schedule_logs(schedule_id="1")
        running = [r for r in logs if r["status"] == "running"]
        assert len(running) == 1
        row = running[0]
        assert row["scheduleName"] == "Daily Invoice Run"
        assert row["scheduleId"] == 1
        assert row["endTime"] is None
        assert row["duration"] is None
        assert row["serverName"] == "BP-APP-01"

    def test_stop_schedule_terminates_running_log_and_returns_none(self):
        client = MockBPClient()
        client.trigger_schedule("Daily Invoice Run")
        assert client.stop_schedule("Daily Invoice Run") is None
        logs = client.get_schedule_logs(schedule_id="1")
        terminated = [r for r in logs if r["status"] == "terminated"]
        assert len(terminated) >= 1
        row = terminated[-1]
        assert row["endTime"] is not None
        assert row["duration"] is not None

    def test_write_on_unknown_target_is_safe(self):
        client = MockBPClient()
        # No matching item/schedule — answers None without raising or mutating.
        assert client.retry_queue_item(_queue_id(client), "ghost") is None
        assert client.set_schedule_enabled("ghost", True) is None
        assert client.trigger_schedule("ghost") is None
        assert client.stop_schedule("ghost") is None

    def test_instances_do_not_share_fixture_mutations(self):
        # A write on one instance must not leak into a fresh instance via the
        # module-level default fixtures.
        MockBPClient().set_schedule_enabled("Daily Invoice Run", False)
        fresh = [s for s in MockBPClient().get_schedules() if s["name"] == "Daily Invoice Run"][0]
        assert fresh["isRetired"] is False


class TestDemoEstate:
    """The populated demo estate: richer than the lean defaults, relative-dated,
    and varied enough that severity bands and collapsed-healthy summaries have
    real content to show."""

    def test_is_materially_larger_than_the_lean_defaults(self):
        demo, lean = demo_estate(), MockBPClient()
        assert len(demo.get_resources()) > len(lean.get_resources())
        assert len(demo.get_queues()) > len(lean.get_queues())
        assert len(demo.get_sessions()) > len(lean.get_sessions())
        assert len(demo.get_processes()) > len(lean.get_processes())
        assert len(demo.get_schedules()) > len(lean.get_schedules())

    def test_every_session_reads_recent(self):
        # The whole point of the anchor: nothing dated to a stale calendar month.
        # The horizon is the history backlog's span — a quarter's worth of days is
        # still legitimate recent operational history, not a stale month.
        starts = [s["startTime"][:10] for s in demo_estate().get_sessions()]
        assert starts and all(d >= _date(_DEMO_HISTORY_DAYS) for d in starts)

    def test_history_volume_varies_weekday_vs_weekend(self):
        # The chart needs shape: weekdays must out-run weekends on average, so a
        # flat series can never pass. Group the backlog by day, classify each, and
        # compare the means.
        from collections import Counter
        from datetime import date

        by_day = Counter(s["startTime"][:10] for s in demo_estate().get_sessions())
        weekday = [n for d, n in by_day.items() if date.fromisoformat(d).weekday() < 5]
        weekend = [n for d, n in by_day.items() if date.fromisoformat(d).weekday() >= 5]
        assert weekday and weekend
        assert sum(weekday) / len(weekday) > sum(weekend) / len(weekend)

    def test_history_carries_terminations_beyond_the_foreground(self):
        # The STP-rate KPI can only move if the backlog itself terminates runs —
        # not just the three explicit foreground failures. Look past the recent
        # foreground window for a generated Terminated run.
        terminated = [
            s
            for s in demo_estate().get_sessions()
            if s["status"] == "Terminated" and s["startTime"][:10] < _date(7)
        ]
        assert terminated
        # Both failure modes appear, so throughput_summary's process-vs-internal
        # error split is exercised rather than one reason being dead.
        reasons = {s["terminationReason"] for s in terminated}
        assert {"ProcessError", "InternalError"} <= reasons

    def test_workers_are_pooled_and_span_every_status(self):
        workers = demo_estate().get_resources()
        assert all(w["poolName"] for w in workers)
        assert len({w["poolName"] for w in workers}) >= 3
        statuses = {w["displayStatus"] for w in workers}
        assert {"Working", "Idle", "Offline"} <= statuses

    def test_queues_span_breach_paused_and_a_healthy_bulk(self):
        queues = demo_estate().get_queues()
        assert any(q["exceptionedItemCount"] > 10 for q in queues)  # an SLA breach
        assert any(q["status"] == "Paused" for q in queues)
        healthy = [q for q in queues if q["exceptionedItemCount"] == 0]
        assert len(healthy) >= 3  # a real bulk for a collapsed-healthy summary

    def test_has_in_flight_and_silently_stale_running_sessions(self):
        running = [s for s in demo_estate().get_sessions() if s["status"] == "Running"]
        assert len(running) >= 2 and all(s["endTime"] is None for s in running)
        # At least one has been running for days — the stuck-session severity case.
        assert any(s["startTime"][:10] <= _date(3) for s in running)

    def test_carries_a_failed_schedule(self):
        client = demo_estate()
        outcomes = {
            s["name"]: (client.get_last_schedule_run(s["id"]) or {}).get("status")
            for s in client.get_schedules()
        }
        assert "terminated" in outcomes.values()
        assert "completed" in outcomes.values()

    def test_every_schedule_has_a_walkable_task_chain(self):
        # Each schedule's initialTaskId must open a real task fixture, every
        # chain link must land on a task in the same schedule, and every task
        # must say what it runs and where — so a consumer can walk from a
        # failed schedule to the task/process/worker without dead ends.
        client = demo_estate()
        for schedule in client.get_schedules():
            tasks = {t["id"]: t for t in client.get_schedule_tasks(schedule["id"])}
            assert schedule["initialTaskId"] in tasks, schedule["name"]
            assert len(tasks) == schedule["tasksCount"], schedule["name"]
            for task in tasks.values():
                for link in ("onSuccessTaskId", "onFailureTaskId"):
                    assert task[link] is None or task[link] in tasks, schedule["name"]
                sessions = client.get_task_sessions(task["id"])
                assert len(sessions) == task["sessionsCount"], task["name"]
                assert all(s["processName"] and s["resourceName"] for s in sessions)

    def test_the_failed_schedule_chain_carries_a_failure_branch(self):
        # The headline Nightly Payment Run is a branching chain: at least one
        # task routes somewhere different on failure, so the triage story
        # ("which task, what does it run, what happens when it fails") is real.
        client = demo_estate()
        failed = next(s for s in client.get_schedules() if s["name"] == "Nightly Payment Run")
        tasks = client.get_schedule_tasks(failed["id"])
        assert any(t["onFailureTaskId"] is not None for t in tasks)

    def test_task_session_names_reference_this_estate_only(self):
        # Like the drill-in items: task sessions must name this estate's own
        # processes and workers, or the slide-over cross-references dead ends.
        client = demo_estate()
        processes = {p["processName"] for p in client.get_processes()}
        workers = {r["name"] for r in client.get_resources()}
        for schedule in client.get_schedules():
            for task in client.get_schedule_tasks(schedule["id"]):
                for s in client.get_task_sessions(task["id"]):
                    assert s["processName"] in processes
                    assert s["resourceName"] in workers

    def test_no_session_reads_as_starting_in_the_future(self):
        # Today's sessions anchor to wall-clock now (_recent), not a fixed time
        # of day, so they never read as the future whatever hour the estate boots.
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        starts = [s["startTime"] for s in demo_estate().get_sessions()]
        assert starts and all(start <= now for start in starts)

    def test_drill_in_items_reference_this_estate_only(self):
        # Seeded explicitly rather than inherited from the lean fixtures, so a
        # queue-item drill-in never names a worker that is not in this estate.
        client = demo_estate()
        workers = {w["name"] for w in client.get_resources()}
        for queue in client.get_queues():
            for item in client.get_queue_items(queue["id"]):
                assert item["resource"] is None or item["resource"] in workers

    def test_notable_sessions_carry_a_stage_log(self):
        client = demo_estate()
        logged = [
            s["sessionId"] for s in client.get_sessions() if client.get_session_log(s["sessionId"])
        ]
        assert logged  # the lean-fixture fallback (different ids) would leave this empty
        assert any(
            e["stageType"] == "Exception" for sid in logged for e in client.get_session_log(sid)
        )


class TestWriteFidelityJourneys:
    """End-to-end journeys exercising the full write→settle→observe loop."""

    @staticmethod
    def _clock(start_iso=None):
        from datetime import datetime, timedelta, timezone

        if start_iso is None:
            start_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        current = [datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)]

        def now():
            return current[0]

        def advance(minutes=0, seconds=0):
            current[0] += timedelta(minutes=minutes, seconds=seconds)

        return now, advance

    def test_start_then_settle_completes_run(self):
        now, advance = self._clock()
        start_ts = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        usage_before = client.get_current_limits_and_usage()["concurrentSessionsUsed"]
        result = client.start_process(
            "7c0e4f2b-93d1-4b66-a2af-000000000201", "5d2c8e0a-71b4-4a8e-9f30-000000000001"
        )
        sid = result["sessionId"]

        sessions = client.get_sessions()
        new = next(s for s in sessions if s["sessionId"] == sid)
        assert new["status"] == "Running"
        assert new["processName"] == "Invoice Processing"
        assert new["resourceName"] == "BOT-01"
        assert new["startTime"] == start_ts

        bot = next(r for r in client.get_resources() if r["name"] == "BOT-01")
        assert bot["displayStatus"] == "Working"
        assert bot["activeSessionCount"] >= 1
        assert client.get_current_limits_and_usage()["concurrentSessionsUsed"] == usage_before + 1

        log = client.get_session_log(sid)
        assert log[-1]["stageType"] == "Start"

        advance(minutes=6)
        sessions = client.get_sessions()
        settled = next(s for s in sessions if s["sessionId"] == sid)
        assert settled["status"] == "Completed"
        assert settled["endTime"] is not None

        bot = next(r for r in client.get_resources() if r["name"] == "BOT-01")
        assert bot["displayStatus"] == "Idle"
        assert bot["activeSessionCount"] == 0
        assert client.get_current_limits_and_usage()["concurrentSessionsUsed"] == usage_before

        log = client.get_session_log(sid)
        assert log[0]["stageType"] == "End"

    def test_seeded_fixtures_untouched_by_settle(self):
        now, advance = self._clock()
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        seeded_running = [s for s in client.get_sessions() if s["status"] == "Running"]
        assert seeded_running

        advance(minutes=60)
        still_running = [s for s in client.get_sessions() if s["status"] == "Running"]
        seeded_ids = {s["sessionId"] for s in seeded_running}
        assert seeded_ids <= {s["sessionId"] for s in still_running}

    def test_stop_before_settle_releases_immediately(self):
        now, advance = self._clock()
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        usage_before = client.get_current_limits_and_usage()["concurrentSessionsUsed"]
        result = client.start_process(
            "7c0e4f2b-93d1-4b66-a2af-000000000201", "5d2c8e0a-71b4-4a8e-9f30-000000000001"
        )
        sid = result["sessionId"]

        advance(minutes=2)
        client.stop_session(sid)

        session = next(s for s in client.get_sessions() if s["sessionId"] == sid)
        assert session["status"] == "Stopped"
        assert session["endTime"] is not None

        bot = next(r for r in client.get_resources() if r["name"] == "BOT-01")
        assert bot["displayStatus"] == "Idle"
        assert client.get_current_limits_and_usage()["concurrentSessionsUsed"] == usage_before

        log = client.get_session_log(sid)
        assert log[0]["stageType"] == "End"

        advance(minutes=10)
        session = next(s for s in client.get_sessions() if s["sessionId"] == sid)
        assert session["status"] == "Stopped"

    def test_retry_flips_to_pending_and_adjusts_counts(self):
        now, _ = self._clock()
        client = MockBPClient(now_fn=now)
        qid = _queue_id(client)
        queue_before = next(q for q in client.get_queues() if q["id"] == qid)
        pending_before = queue_before["pendingItemCount"]
        exc_before = queue_before["exceptionedItemCount"]

        item = client.get_queue_items(qid, state="Exceptioned")[0]
        client.retry_queue_item(qid, item["id"])

        queue_after = next(q for q in client.get_queues() if q["id"] == qid)
        assert queue_after["pendingItemCount"] == pending_before + 1
        assert queue_after["exceptionedItemCount"] == exc_before - 1
        assert queue_after["totalItemCount"] == queue_before["totalItemCount"]

        retried = next(i for i in client.get_queue_items(qid) if i["id"] == item["id"])
        assert retried["state"] == "Pending"
        assert retried["exceptionReason"] is None

        attempts = client.get_item_attempts(qid, item["id"])
        assert len(attempts) >= 2

    def test_defer_decrements_pending_count(self):
        now, _ = self._clock()
        client = MockBPClient(now_fn=now)
        qid = _queue_id(client)

        exc_item = client.get_queue_items(qid, state="Exceptioned")[0]
        result = client.retry_queue_item(qid, exc_item["id"])
        new_attempt = result["attemptId"]

        queue_before = next(q for q in client.get_queues() if q["id"] == qid)
        pending_before = queue_before["pendingItemCount"]
        assert pending_before >= 1

        client.defer_queue_item(qid, exc_item["id"], new_attempt, "2026-08-01T00:00:00Z")

        queue_after = next(q for q in client.get_queues() if q["id"] == qid)
        assert queue_after["pendingItemCount"] == pending_before - 1

        deferred = client.get_queue_items(qid, state="Deferred")
        assert any(i["id"] == exc_item["id"] for i in deferred)

    def test_trigger_schedule_then_settle_completes(self):
        now, advance = self._clock()
        start_ts = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        client.trigger_schedule("Daily Invoice Run")

        last = client.get_last_schedule_run(1)
        assert last is not None
        assert last["status"] == "running"
        assert last["startTime"] == start_ts

        advance(minutes=6)
        last = client.get_last_schedule_run(1)
        assert last["status"] == "completed"
        assert last["endTime"] is not None
        assert last["duration"] is not None

    def test_stop_schedule_mid_run_terminates(self):
        now, advance = self._clock()
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        client.trigger_schedule("Daily Invoice Run")

        advance(minutes=2)
        client.stop_schedule("Daily Invoice Run")

        last = client.get_last_schedule_run(1)
        assert last["status"] == "terminated"
        assert last["endTime"] is not None
        assert last["duration"] is not None

        advance(minutes=10)
        last = client.get_last_schedule_run(1)
        assert last["status"] == "terminated"

    def test_trigger_schedule_normalises_offset_start_time(self):
        # start_time is derived from the injected clock, not a hardcoded
        # calendar literal: get_last_schedule_run picks the MOST RECENT run
        # across the whole schedule, fixture history included, and the
        # fixture's own seeded runs (_ts(1, ...) etc.) are anchored to real
        # wall-clock time — a fixed past literal eventually drifts behind
        # them and the assertion starts reading the seeded row instead.
        now, advance = self._clock()
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        canonical = now().strftime("%Y-%m-%dT%H:%M:%SZ")
        offset_form = now().strftime("%Y-%m-%dT%H:%M:%S") + "+00:00"
        client.trigger_schedule("Daily Invoice Run", start_time=offset_form)

        last = client.get_last_schedule_run(1)
        assert last["startTime"] == canonical

        # Subsequent settling reads must not raise on the canonicalised row.
        client.get_sessions()
        advance(minutes=6)
        last = client.get_last_schedule_run(1)
        assert last["status"] == "completed"

    def test_trigger_schedule_normalises_date_only_start_time(self):
        # Same clock-relative reasoning as the offset test above — a fixed
        # future calendar literal would itself become a past date (and start
        # losing to fresher seeded fixture runs) once real time caught up.
        now, _ = self._clock()
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        future_date = (now() + __import__("datetime").timedelta(days=30)).strftime("%Y-%m-%d")
        client.trigger_schedule("Daily Invoice Run", start_time=future_date)

        # Must not raise on a date-only start_time.
        last = client.get_last_schedule_run(1)
        assert last["startTime"] == f"{future_date}T00:00:00Z"
        client.get_sessions()
        client.get_schedule_logs()

    def test_stop_schedule_future_start_time_duration_never_negative(self):
        now, _ = self._clock()
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        future = (now() + __import__("datetime").timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        client.trigger_schedule("Daily Invoice Run", start_time=future)

        client.stop_schedule("Daily Invoice Run")

        last = client.get_last_schedule_run(1)
        assert last["status"] == "terminated"
        assert last["duration"] == "00:00:00"

    def test_settle_discards_orphaned_run_id(self):
        now, advance = self._clock()
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        client._live_run_ids.add("ghost-session-id")
        advance(minutes=6)
        client.get_sessions()
        assert "ghost-session-id" not in client._live_run_ids

    def test_settle_discards_orphaned_schedule_log_id(self):
        now, advance = self._clock()
        client = MockBPClient(now_fn=now, settle_after=__import__("datetime").timedelta(minutes=5))
        client._live_schedule_log_ids.add(99999)
        advance(minutes=6)
        client.get_schedule_logs()
        assert 99999 not in client._live_schedule_log_ids

    def test_release_worker_tolerates_unknown_resource(self):
        client = MockBPClient()
        client._release_worker("no-such-id", "no-such-name")

    def test_occupy_worker_tolerates_unknown_resource(self):
        client = MockBPClient()
        client._occupy_worker("no-such-id", "no-such-name")


# --- Phase 12: transport governance -------------------------------------------


class RecordingLimiter:
    """A RateLimiter that records the wait budget it was handed."""

    def __init__(self, grant: bool = True) -> None:
        self.grant = grant
        self.timeouts: list[float] = []

    def acquire(self, timeout: float) -> bool:
        self.timeouts.append(timeout)
        return self.grant


def _resp_with(status_code: int, *, headers=None, content=b"{}", body=None):
    """A response mock with real headers/content, for the transport paths."""
    return MagicMock(
        status_code=status_code,
        headers=headers if headers is not None else {},
        content=content,
        json=MagicMock(return_value=body if body is not None else {}),
        raise_for_status=MagicMock(),
    )


class TestTransportDefaultsAreOff:
    """An unconfigured client emits exactly what it did before Phase 12."""

    def test_no_limiter_no_semaphore_no_retries_by_default(self):
        client, _ = make_client()
        assert client._limiter is None
        assert client._semaphore is None
        assert client._retry.max_retries == 0

    def test_no_adapter_is_mounted_unless_sized(self):
        client, session = make_client()
        assert session.mount.call_count == 0

    def test_retry_settings_are_wired_from_config(self):
        client, _ = make_client(max_retries=3, retry_base_delay=2.0)
        assert client._retry.max_retries == 3
        client._retry._jitter = lambda: 0.0
        assert client._retry.delay_for(503, 0) == 1.0  # half of the 2.0 window

    def test_a_limiter_is_built_only_when_a_rate_is_configured(self):
        client, _ = make_client(max_requests_per_second=5.0, max_burst=10)
        assert isinstance(client._limiter, TokenBucket)

    def test_an_injected_limiter_wins_over_config(self):
        limiter = RecordingLimiter()
        session = MagicMock()
        session.post.return_value = _auth_resp()
        client = BPClient(
            make_config(max_requests_per_second=5.0), session=session, limiter=limiter
        )
        assert client._limiter is limiter


class TestConnectionPoolSizing:
    def test_adapter_mounted_on_both_schemes_when_configured(self):
        client, session = make_client(pool_maxsize=12)
        schemes = [call.args[0] for call in session.mount.call_args_list]
        assert schemes == ["https://", "http://"]

        adapters = {id(call.args[1]) for call in session.mount.call_args_list}
        assert len(adapters) == 1  # one adapter, shared

    def test_pool_is_never_narrower_than_the_concurrency_ceiling(self):
        # Otherwise requests opens and discards connections above the pool size
        # — TLS re-handshakes against the estate at the worst moment.
        _, session = make_client(pool_maxsize=2, max_concurrency=8)
        adapter = session.mount.call_args_list[0].args[1]
        assert adapter._pool_maxsize == 8

    def test_configured_pool_size_is_honoured_when_it_is_the_larger(self):
        _, session = make_client(pool_maxsize=20, max_concurrency=8)
        adapter = session.mount.call_args_list[0].args[1]
        assert adapter._pool_maxsize == 20

    def test_concurrency_alone_sizes_the_pool(self):
        _, session = make_client(max_concurrency=6)
        adapter = session.mount.call_args_list[0].args[1]
        assert adapter._pool_maxsize == 6


class TestConcurrencyCeiling:
    def test_semaphore_caps_requests_in_flight(self):
        client, session = make_client(max_concurrency=2, limiter_timeout_seconds=5.0)
        prime_token(client)
        in_flight = 0
        peak = 0
        lock = threading.Lock()

        def slow_get(*_a, **_k):
            nonlocal in_flight, peak
            with lock:
                in_flight += 1
                peak = max(peak, in_flight)
            time.sleep(0.02)
            with lock:
                in_flight -= 1
            return _resp_with(200, body={"items": []})

        session.get = slow_get
        threads = [threading.Thread(target=client.get_resources) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert peak <= 2

    def test_exhausting_the_wait_budget_raises_rather_than_dropping(self):
        client, session = make_client(max_concurrency=1, limiter_timeout_seconds=0.05)
        prime_token(client)
        client._semaphore.acquire()  # occupy the only slot
        try:
            # The message names the call that was refused — a budget error with
            # no endpoint in it tells an operator nothing.
            with pytest.raises(TransportBudgetExceeded, match="concurrency slot.*/resources"):
                client.get_resources()
        finally:
            client._semaphore.release()

        stats = client.transport_stats()
        assert stats["errors"]["limiter_exhausted"] == 1
        assert stats["requests"] == 0  # nothing was sent
        assert session.get.call_count == 0

    def test_the_slot_is_released_when_the_request_raises(self):
        client, session = make_client(max_concurrency=1, limiter_timeout_seconds=0.05)
        prime_token(client)
        session.get.side_effect = requests.ConnectionError("boom")

        for _ in range(3):
            with pytest.raises(requests.ConnectionError):
                client.get_resources()
        # A leaked slot would turn the second call into a budget error instead.
        assert client.transport_stats()["errors"]["connection_error"] == 3


class TestRateLimiterWiring:
    def test_a_refused_token_raises_and_sends_nothing(self):
        session = MagicMock()
        session.post.return_value = _auth_resp()
        client = BPClient(make_config(), session=session, limiter=RecordingLimiter(grant=False))
        prime_token(client)

        with pytest.raises(TransportBudgetExceeded, match="rate-limit token.*/resources"):
            client.get_resources()

        assert session.get.call_count == 0
        assert client.transport_stats()["errors"]["limiter_exhausted"] == 1

    def test_limiter_and_semaphore_share_one_wait_budget(self):
        # limiter_timeout_seconds is the END-TO-END bound. Time spent waiting on
        # the semaphore must come out of the limiter's allowance, or the
        # configured number silently becomes two stacked timeouts.
        limiter = RecordingLimiter()
        session = MagicMock()
        session.post.return_value = _auth_resp()
        session.get.return_value = _resp_with(200, body={"items": []})
        client = BPClient(
            make_config(max_concurrency=1, limiter_timeout_seconds=0.5),
            session=session,
            limiter=limiter,
        )
        prime_token(client)

        client._semaphore.acquire()
        releaser = threading.Timer(0.1, client._semaphore.release)
        releaser.start()
        client.get_resources()
        releaser.join()

        assert len(limiter.timeouts) == 1
        # It waited ~0.1s on the semaphore, so the limiter got the remainder —
        # visibly less than the full budget, and still positive.
        assert 0.0 < limiter.timeouts[0] < 0.45

    def test_the_full_budget_reaches_the_limiter_when_uncontended(self):
        limiter = RecordingLimiter()
        session = MagicMock()
        session.post.return_value = _auth_resp()
        session.get.return_value = _resp_with(200, body={"items": []})
        client = BPClient(
            make_config(limiter_timeout_seconds=10.0), session=session, limiter=limiter
        )
        prime_token(client)
        client.get_resources()

        assert limiter.timeouts[0] == pytest.approx(10.0, abs=0.05)


class TestRetryLayer:
    def _client(self, **cfg):
        client, session = make_client(max_retries=2, **cfg)
        prime_token(client)
        self.slept: list[float] = []
        client._retry = RetryPolicy(2, base_delay=1.0, jitter=lambda: 0.0, sleep=self.slept.append)
        return client, session

    def test_retry_after_is_honoured_on_429(self):
        client, session = self._client()
        session.get.side_effect = [
            _resp_with(429, headers={"Retry-After": "3"}),
            _resp_with(200, body={"items": [{"id": "r1"}]}),
        ]

        assert client.get_resources() == [{"id": "r1"}]
        assert self.slept == [3.0]
        assert client.transport_stats()["retries"] == 1
        assert client.transport_stats()["errors"]["rate_limited"] == 1

    def test_transient_gateway_errors_back_off_and_stop_at_max_retries(self):
        client, session = self._client()
        session.get.return_value = _http_error_resp(503)

        with pytest.raises(requests.HTTPError):
            client.get_resources()

        assert session.get.call_count == 3  # the original plus two retries
        assert self.slept == [0.5, 1.0]  # doubling window, jitter pinned to 0
        stats = client.transport_stats()
        assert stats["retries"] == 2
        assert stats["errors"]["server_error"] == 3

    def test_a_non_transient_error_is_not_retried(self):
        client, session = self._client()
        session.get.return_value = _http_error_resp(500)

        with pytest.raises(requests.HTTPError):
            client.get_resources()

        assert session.get.call_count == 1
        assert self.slept == []

    def test_writes_are_never_retried(self):
        # A retried write is a duplicate estate mutation — a second
        # start_process is a second live run.
        client, session = self._client()
        session.patch.return_value = _http_error_resp(503)

        with pytest.raises(requests.HTTPError):
            client.stop_session("3f2504e0-4f89-11d3-9a0c-0305e82c3301")

        assert session.patch.call_count == 1
        assert self.slept == []
        assert client.transport_stats()["retries"] == 0

    def test_the_retry_opt_in_is_fail_closed_by_default(self):
        # Every current call site is explicit, so this pins the DEFAULT: a call
        # site added later without thinking gets the safe behaviour, not the
        # one that could duplicate an estate mutation.
        client, session = self._client()
        session.post.return_value = _http_error_resp(503)

        with pytest.raises(requests.HTTPError):
            client._request("POST", "/sessions")

        assert session.post.call_count == 1
        assert self.slept == []

    def test_the_401_reauth_is_untouched_and_is_not_counted_as_a_retry(self):
        client, session = make_client(max_retries=2)
        session.get.side_effect = [
            _resp_with(401),
            _resp_with(200, body={"items": [{"id": "r1"}]}),
        ]

        assert client.get_resources() == [{"id": "r1"}]
        assert session.get.call_count == 2
        stats = client.transport_stats()
        assert stats["retries"] == 0  # in-band re-auth, not a transient retry
        assert stats["token_fetches"] == 2  # the initial fetch plus the re-auth

    def test_a_429_without_headers_falls_back_to_calculated_backoff(self):
        client, session = self._client()
        headless = MagicMock(status_code=429, content=b"{}", spec=["status_code", "content"])
        session.get.side_effect = [headless, _resp_with(200, body={"items": []})]

        assert client.get_resources() == []
        assert self.slept == [0.5]  # the bounded backoff, not a server instruction

    def test_a_retry_still_re_auths_within_each_attempt(self):
        client, session = self._client()
        session.get.side_effect = [
            _resp_with(503),
            _resp_with(401),
            _resp_with(200, body={"items": []}),
        ]

        assert client.get_resources() == []
        assert session.get.call_count == 3
        assert client.transport_stats()["retries"] == 1


class TestRequestAccounting:
    def test_requests_are_tallied_by_path_template(self):
        client, session = make_client()
        prime_token(client)
        session.get.return_value = _resp_with(200, body={"items": []})
        client.get_resources()
        client.get_queue("3f2504e0-4f89-11d3-9a0c-0305e82c3301")

        stats = client.transport_stats()
        assert stats["requests"] == 2
        assert stats["by_path"] == {"/resources": 1, "/workqueues/{id}": 1}

    def test_response_bytes_are_counted(self):
        client, session = make_client()
        prime_token(client)
        session.get.return_value = _resp_with(200, content=b'{"items": []}', body={"items": []})
        client.get_resources()

        assert client.transport_stats()["bytes_received"] == 13

    def test_a_body_that_is_not_bytes_contributes_nothing_rather_than_raising(self):
        client, session = make_client()
        prime_token(client)
        session.get.return_value = _resp(  # a plain MagicMock content
            {"items": []}
        )
        client.get_resources()

        assert client.transport_stats()["bytes_received"] == 0
        assert client.transport_stats()["requests"] == 1

    def test_timeouts_and_connection_errors_are_classified(self):
        client, session = make_client()
        prime_token(client)
        session.get.side_effect = requests.Timeout("slow")
        with pytest.raises(requests.Timeout):
            client.get_resources()

        session.get.side_effect = requests.ConnectionError("refused")
        with pytest.raises(requests.ConnectionError):
            client.get_queues()

        stats = client.transport_stats()
        assert stats["errors"]["timeout"] == 1
        assert stats["errors"]["connection_error"] == 1
        # A failed request still cost the estate a connection, so it is counted
        # against the endpoint that made it.
        assert stats["by_path"] == {"/resources": 1, "/workqueues": 1}

    def test_reads_carry_the_configured_tls_and_timeout(self):
        client, session = make_client(verify_ssl=False, request_timeout=7.5)
        prime_token(client)
        session.get.return_value = _resp_with(200, body={"items": []})
        client.get_resources()

        assert session.get.call_args.kwargs["verify"] is False
        assert session.get.call_args.kwargs["timeout"] == 7.5

    def test_a_204_means_no_content_whatever_arrived_with_it(self):
        client, session = make_client()
        prime_token(client)
        session.get.return_value = _resp_with(204, content=b'{"stray": true}')
        assert client._request("GET", "/resources") is None

    def test_token_fetches_stay_out_of_the_api_budget(self):
        client, session = make_client()
        session.get.return_value = _resp_with(200, body={"items": []})
        client.get_resources()

        stats = client.transport_stats()
        assert stats["token_fetches"] == 1
        assert stats["requests"] == 1  # the /resources GET only
        assert "/connect/token" not in stats["by_path"]


class TestSingleFlight:
    """A cache miss under load is one upstream read, not one per caller."""

    def test_concurrent_missers_share_one_production(self):
        client, _ = make_client()
        calls: list[int] = []
        barrier = threading.Barrier(8)
        results: list[object] = []
        lock = threading.Lock()

        def produce():
            calls.append(1)
            time.sleep(0.05)  # a genuinely slow upstream read
            return ["the-value"]

        def worker():
            barrier.wait()
            value = client._cached("k", produce)
            with lock:
                results.append(value)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(calls) == 1
        assert results == [["the-value"]] * 8

    def test_the_value_is_cached_for_callers_arriving_afterwards(self):
        client, _ = make_client()
        calls: list[int] = []

        def produce():
            calls.append(1)
            return ["v"]

        assert client._cached("k", produce) == ["v"]
        assert client._cached("k", produce) == ["v"]
        assert len(calls) == 1

    def test_a_failing_producer_does_not_wedge_the_waiters(self):
        # The losers re-raise rather than each re-attempting: a failing upstream
        # call is the last thing to multiply by the number of waiting threads.
        client, _ = make_client()
        calls: list[int] = []
        barrier = threading.Barrier(6)
        raised: list[BaseException] = []
        lock = threading.Lock()

        def produce():
            calls.append(1)
            time.sleep(0.05)
            raise RuntimeError("upstream is down")

        def worker():
            barrier.wait()
            try:
                client._cached("k", produce)
            except BaseException as exc:  # noqa: BLE001 - the point of the test
                with lock:
                    raised.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not any(t.is_alive() for t in threads)
        assert len(calls) == 1
        assert len(raised) == 6
        assert all(isinstance(exc, RuntimeError) for exc in raised)

    def test_a_failed_key_is_released_so_the_next_call_retries(self):
        client, _ = make_client()
        calls: list[int] = []

        def produce():
            calls.append(1)
            raise RuntimeError("down")

        for _ in range(3):
            with pytest.raises(RuntimeError):
                client._cached("k", produce)
        assert len(calls) == 3

    def test_distinct_keys_do_not_block_each_other(self):
        # The registry lock guards bookkeeping only — never the production —
        # so a slow read of one key must not stall every other key.
        client, _ = make_client()
        overlapped = threading.Barrier(2, timeout=5)

        def produce():
            overlapped.wait()  # only clears if both producers run at once
            return ["v"]

        threads = [
            threading.Thread(target=client._cached, args=(key, produce)) for key in ("a", "b")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert not any(t.is_alive() for t in threads)

    def test_a_write_still_clears_the_cache(self):
        client, session = make_client()
        prime_token(client)
        session.get.return_value = _resp_with(200, body={"items": [{"id": "r1"}]})
        client.get_resources()
        session.patch.return_value = _resp_with(204, content=b"")

        client.stop_session("3f2504e0-4f89-11d3-9a0c-0305e82c3301")

        client.get_resources()
        assert session.get.call_count == 2


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Guard: fail loudly if any test reaches a real socket via requests."""
    import requests

    def _boom(*_a, **_k):  # pragma: no cover - only fires on a test bug
        raise AssertionError("a test attempted a real HTTP request")

    monkeypatch.setattr(requests.Session, "request", _boom)

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


@pytest.fixture(autouse=True)
def _no_real_http(monkeypatch):
    """Guard: fail loudly if any test reaches a real socket via requests."""
    import requests

    def _boom(*_a, **_k):  # pragma: no cover - only fires on a test bug
        raise AssertionError("a test attempted a real HTTP request")

    monkeypatch.setattr(requests.Session, "request", _boom)

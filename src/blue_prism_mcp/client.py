"""BPClient — the Blue Prism v7 REST client (the extraction core).

This is the dashboard's `data/bp_api_provider.py` after the Streamlit-ectomy:

    import streamlit                 -> deleted
    @st.cache_data(ttl=30)          -> a per-instance TTL cache (self._cache)
    auth token in st.session_state  -> instance state (self._token)
    module-level config globals     -> the injected BPConfig (self._config)
    module-level requests.Session   -> a per-instance Session (self._session)

Everything that was process-global in the dashboard now lives on the instance,
so two estates / two server instances never collide on a shared token or cache.

Phase 1 lifts the reads the dashboard already proved against the live v7 API:
resources, work queues, schedules, and sessions, with the auth/401-retry and
pagination plumbing. Phase 2 extends the surface (queue-ITEM listing,
/processes, the session stage-log, and the gated Tier 3 writes).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import requests

from .config import BPConfig

logger = logging.getLogger("blue_prism_mcp.client")

# Cache sentinel — distinguishes "absent" from a cached falsy value (e.g. []).
_MISS = object()


class _TTLCache:
    """A tiny time-to-live cache keyed by an arbitrary hashable.

    Replaces Streamlit's @st.cache_data: same intent (don't re-hit the live API
    on every read within a short window), but bound to one client instance
    instead of a process-global store.
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._store: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any) -> Any:
        entry = self._store.get(key)
        if entry is None:
            return _MISS
        stored_at, value = entry
        if time.monotonic() - stored_at > self._ttl:
            del self._store[key]
            return _MISS
        return value

    def set(self, key: Any, value: Any) -> None:
        self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        self._store.clear()


class BPClient:
    """Stateful client for one Blue Prism v7 estate.

    Holds the injected config, the bearer token, a reused HTTP session, and a
    per-instance TTL cache. Read methods mirror the v7 entities; write methods
    (Phase 2/5) are only surfaced as MCP tools when config.enable_actions is True.
    """

    def __init__(
        self, config: BPConfig, session: requests.Session | None = None
    ) -> None:
        self._config = config
        self._session = session or requests.Session()  # pools TCP connections
        self._token: str | None = None
        self._cache = _TTLCache(config.cache_ttl)

    def clear_cache(self) -> None:
        """Drop all cached reads (mirrors st.cache_data.clear())."""
        self._cache.clear()

    # --- Auth ---------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a bearer token, authenticating on first use and caching it."""
        if self._token:
            return self._token
        resp = self._session.post(
            f"{self._config.base_url}/auth/token",
            json={
                "username": self._config.username,
                "password": self._config.password,
            },
            verify=self._config.verify_ssl,
            timeout=10,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _invalidate_token(self) -> None:
        self._token = None

    # --- HTTP ---------------------------------------------------------------

    def _get(self, path: str, params: dict | None = None) -> list | dict:
        """GET a path, re-authenticating once on a 401 and retrying."""
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        resp = self._session.get(
            f"{self._config.base_url}{path}",
            headers=headers,
            params=params,
            verify=self._config.verify_ssl,
            timeout=15,
        )
        if resp.status_code == 401:
            # Token may have expired — re-auth once and retry.
            self._invalidate_token()
            headers = {"Authorization": f"Bearer {self._get_token()}"}
            resp = self._session.get(
                f"{self._config.base_url}{path}",
                headers=headers,
                params=params,
                verify=self._config.verify_ssl,
                timeout=15,
            )
        resp.raise_for_status()
        return resp.json()

    # --- Pagination ---------------------------------------------------------
    # Collection endpoints are fetched page-by-page so a large estate cannot
    # silently return only the first page. The response shape and paging
    # mechanism vary by BP v7 deployment, so both are config-driven. Handles
    # plain-list responses, item-key envelopes ({"items": [...]}), token paging,
    # and offset paging.

    def _unpack_page(self, body: Any) -> tuple[list, str | None]:
        """Return (items, next_token) from a response body of any supported shape."""
        if isinstance(body, list):
            return body, None
        if isinstance(body, dict):
            items: list = []
            for key in self._config.page_items_keys:
                if isinstance(body.get(key), list):
                    items = body[key]
                    break
            token: str | None = None
            for key in self._config.page_token_keys:
                val = body.get(key)
                if val:
                    token = str(val)
                    break
            return items, token
        return [], None

    def _get_collection(self, path: str, base_params: dict | None = None) -> list:
        """Fetch every page of a collection endpoint and return a flat list.

        Paging behaviour is governed by config.paging_mode:
          none   — single request
          token  — follow next-page tokens until exhausted
          offset — advance the offset by items fetched until a short page
          auto   — detect token vs offset from the first response (default)
        """
        cfg = self._config
        params = dict(base_params or {})

        if cfg.paging_mode == "none":
            items, _ = self._unpack_page(self._get(path, params=params or None))
            return items

        params[cfg.page_size_param] = cfg.page_size
        collected: list = []
        token: str | None = None
        detected = cfg.paging_mode  # "auto" resolves to token/offset after page 1

        for page in range(cfg.max_pages):
            if token is not None:
                params[cfg.page_token_param] = token
            elif detected == "offset" and page > 0:
                params[cfg.page_offset_param] = len(collected)

            page_items, next_token = self._unpack_page(self._get(path, params=params))
            collected.extend(page_items)

            if detected == "auto":
                detected = "token" if next_token else "offset"

            if detected == "token":
                token = next_token
                if not token:
                    break
            else:  # offset
                if len(page_items) < cfg.page_size:
                    break
        else:
            logger.warning(
                "Pagination hit max_pages (%d) for %s — results may be incomplete",
                cfg.max_pages,
                path,
            )

        return collected

    def _cached(self, key: Any, produce: Callable[[], list]) -> list:
        """Return a cached read for `key`, computing and storing it on a miss."""
        hit = self._cache.get(key)
        if hit is not _MISS:
            return hit
        value = produce()
        self._cache.set(key, value)
        return value

    # --- Tier 1 reads -------------------------------------------------------

    def get_resources(self) -> list[dict]:
        """GET /resources — digital workers and their status."""
        return self._cached("resources", lambda: self._get_collection("/resources"))

    def get_queues(self) -> list[dict]:
        """GET /workqueues — work-queue health."""
        return self._cached("queues", lambda: self._get_collection("/workqueues"))

    def get_schedules(self) -> list[dict]:
        """GET /schedules — schedules, next runs, last outcome."""
        return self._cached("schedules", lambda: self._get_collection("/schedules"))

    def get_sessions(
        self, start_date: str | None = None, end_date: str | None = None
    ) -> list[dict]:
        """GET /sessions — run history, with optional server-side date filtering.

        Passing a date window avoids loading the entire session history; the
        tool layer (Phase 4) requires it and ISO-validates the bounds.
        """
        params: dict[str, str] = {}
        if start_date:
            params["startdatefrom"] = start_date
        if end_date:
            params["startdateto"] = end_date
        return self._cached(
            ("sessions", start_date, end_date),
            lambda: self._get_collection("/sessions", base_params=params or None),
        )

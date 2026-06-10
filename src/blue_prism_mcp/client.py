"""BPClient — the Blue Prism v7 REST client (the extraction core).

This is the dashboard's `data/bp_api_provider.py` after the Streamlit-ectomy:

    import streamlit                 -> deleted
    @st.cache_data(ttl=30)          -> a per-instance TTL cache (self._cache)
    auth token in st.session_state  -> instance state (self._token)
    module-level config globals     -> the injected BPConfig (self._config)
    module-level requests.Session   -> a per-instance Session (self._session)

Everything that was process-global in the dashboard now lives on the instance,
so two estates / two server instances never collide on a shared token or cache.

Phase 1 lifted the reads the dashboard proved out: resources, work queues,
schedules, and sessions, with the auth/401-retry and pagination plumbing.
Phase 2 extends the surface beyond what the dashboard needed — queue-ITEM
listing, the /processes catalogue, the session stage-log, and the Tier 3 writes
(present but exposed as tools only once Phase 5 wires the enable_actions gate
around them) — and aligns the whole surface with the official v7 API OpenAPI
specs (verified against 7.0.1–7.5.1; see the ground-truth section in DESIGN.md):
OAuth2 client-credentials auth, deepObject filter encoding, token paging, and
the attempt-based item write model.
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

# Refresh the OAuth2 token this many seconds before its stated expiry, so a
# token never goes stale mid-request. The 401 retry in _request remains the
# safety net for clock skew or server-side revocation.
_TOKEN_EXPIRY_SKEW = 60.0


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
        if time.monotonic() - stored_at >= self._ttl:
            # >= so an entry expires exactly at the TTL boundary, and ttl=0
            # always misses regardless of clock resolution (no stale read can
            # slip through on a tied monotonic() reading).
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

    def __init__(self, config: BPConfig, session: requests.Session | None = None) -> None:
        self._config = config
        self._session = session or requests.Session()  # pools TCP connections
        self._token: str | None = None
        self._token_expiry: float = 0.0  # monotonic deadline; 0 → no token
        self._cache = _TTLCache(config.cache_ttl)

    def clear_cache(self) -> None:
        """Drop all cached reads (mirrors st.cache_data.clear())."""
        self._cache.clear()

    # --- Auth ---------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a bearer token, fetching a fresh one when absent or near expiry.

        v7 auth is OAuth2 client-credentials against the Blue Prism
        Authentication Server — a form-encoded POST to <auth_url>/connect/token
        with scope bp-api, returning a JWT (the only scheme the API documents;
        identical across 7.0–7.5). expires_in is honoured with a skew so the
        token is refreshed before the server would reject it.
        """
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        resp = self._session.post(
            f"{self._config.auth_url}/connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "scope": self._config.token_scope,
            },
            verify=self._config.verify_ssl,
            timeout=self._config.request_timeout,
        )
        resp.raise_for_status()
        payload = resp.json()
        self._token = payload["access_token"]
        expires_in = float(payload.get("expires_in") or 0)
        # No expires_in → trust the token until a 401 proves otherwise. A tiny
        # expires_in still yields a positive lifetime so we don't re-auth on
        # every single request.
        self._token_expiry = (
            time.monotonic() + max(expires_in - _TOKEN_EXPIRY_SKEW, 1.0)
            if expires_in
            else float("inf")
        )
        return self._token

    def _invalidate_token(self) -> None:
        self._token = None
        self._token_expiry = 0.0

    # --- HTTP ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: list | dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        """Issue an authenticated request, re-authing once on a 401 and retrying.

        Shared by every read and write so the bearer-token plumbing lives in one
        place. Dispatches on the verb-specific session method (session.get/post/
        put/patch), which keeps the reused connection pool and lets tests stub a
        single verb at a time.

        Returns the decoded JSON body, which per the v7 spec is not always an
        object: writes answer 204/empty (→ None here) and POST /sessions returns
        a bare UUID string. The body may also be a JSON Patch *list*.
        """
        send = getattr(self._session, method.lower())
        url = f"{self._config.base_url}{path}"

        def _send():
            # Caller headers take precedence (the JSON Patch endpoint needs
            # its own Content-Type; requests fills in application/json for
            # json= bodies otherwise). The bearer token is only fetched when
            # the caller didn't bring an Authorization of their own — an
            # override must not trigger a needless auth round-trip.
            send_headers = dict(headers or {})
            if not any(k.lower() == "authorization" for k in send_headers):
                send_headers["Authorization"] = f"Bearer {self._get_token()}"
            return send(
                url,
                headers=send_headers,
                params=params,
                json=json,
                verify=self._config.verify_ssl,
                timeout=self._config.request_timeout,
            )

        resp = _send()
        if resp.status_code == 401:
            # Token may have expired — re-auth once and retry.
            self._invalidate_token()
            resp = _send()
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _get(self, path: str, params: dict | None = None) -> Any:
        """GET a path through the shared request/auth/retry path."""
        return self._request("GET", path, params=params)

    # --- Pagination ---------------------------------------------------------
    # Collection endpoints are fetched page-by-page so a large estate cannot
    # silently return only the first page. v7 is token-paged everywhere
    # (itemsPerPage + pagingToken, responses {"items": [...], "pagingToken":
    # ...} — verified across 7.0–7.5), which is the configured default; the
    # offset/auto modes and the key names stay config-driven as an escape hatch
    # for gateways or proxies that reshape responses.

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

    def _has_token_key(self, body: Any) -> bool:
        """True if the body carries any configured paging-token key.

        Auto-detection keys off the *presence* of a token key, not its value: a
        token endpoint signals its last page with an empty/null token, so a
        truthiness test would misread that final page as offset-paged and issue
        a spurious offset follow-up. An absent/empty token then means "no next
        page" via _unpack_page, which stops the loop cleanly.
        """
        if not isinstance(body, dict):
            return False
        return any(key in body for key in self._config.page_token_keys)

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

            body = self._get(path, params=params)
            page_items, next_token = self._unpack_page(body)
            collected.extend(page_items)

            if detected == "auto":
                detected = "token" if self._has_token_key(body) else "offset"

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
        tool layer (Phase 4) requires it and ISO-validates the bounds. v7
        filters are deepObject-encoded — the window goes as startTime[gte] /
        startTime[lte] (per the spec's RangeOrEqualFilter).
        """
        params: dict[str, str] = {}
        if start_date:
            params["startTime[gte]"] = start_date
        if end_date:
            params["startTime[lte]"] = end_date
        return self._cached(
            ("sessions", start_date, end_date),
            lambda: self._get_collection("/sessions", base_params=params or None),
        )

    def get_processes(self) -> list[dict]:
        """GET /processes — the published process catalogue."""
        return self._cached("processes", lambda: self._get_collection("/processes"))

    def get_queue_items(
        self,
        queue_id: str,
        state: str | None = None,
        status: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """GET /workqueues/{id}/items — items in one queue, optionally filtered.

        A single queue can hold millions of items, so the tool layer (Phase 4)
        requires a state and a date window before calling this. The client stays
        thin and speaks the spec's filter vocabulary: `state` is the lifecycle
        enum (Pending/Locked/Deferred/Completed/Exceptioned), `status` is the
        free user-supplied text (FullStringFilter → status[eq]), and the date
        window goes on lastUpdated[gte]/[lte] — the one timestamp every item
        carries regardless of state (completedDate is null for pending items).

        List responses are WorkQueueItemNoData: the API excludes item payload
        data from lists by design; exceptionReason is still present (scrubbed
        at the tool boundary).
        """
        params: dict[str, str] = {}
        if state:
            params["state"] = state
        if status:
            params["status[eq]"] = status
        if start_date:
            params["lastUpdated[gte]"] = start_date
        if end_date:
            params["lastUpdated[lte]"] = end_date
        return self._cached(
            ("queue_items", queue_id, state, status, start_date, end_date),
            lambda: self._get_collection(
                f"/workqueues/{queue_id}/items", base_params=params or None
            ),
        )

    def get_session_log(self, session_id: str) -> list[dict]:
        """GET /sessions/{id}/logs — the stage-level log for one session.

        The highest-value agentic read ("why did this run fail?"). Stage data can
        carry item payloads, so the tool layer routes the result through the PII
        scrubber (Phase 3/4); the client returns it raw.
        """
        return self._cached(
            ("session_log", session_id),
            lambda: self._get_collection(f"/sessions/{session_id}/logs"),
        )

    # --- Tier 3 writes ------------------------------------------------------
    # Designed in, shipped disabled: these issue real v7 writes, but no MCP tool
    # exposes them until Phase 5 wires the enable_actions gate, capability
    # resolver, audit log, and dry-run around them. Each mutates estate state, so
    # _write drops the read cache afterwards to avoid serving a stale view.
    # Paths and bodies follow the verified 7.5.1 spec (all writes exist from
    # 7.2; session create/control from 7.1) — see DESIGN.md's ground-truth
    # section, including the two spots the spec underdocuments.

    def _write(
        self,
        method: str,
        path: str,
        body: list | dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        """Issue a mutating request and invalidate the read cache on success."""
        result = self._request(method, path, json=body, headers=headers)
        self._cache.clear()
        return result

    def retry_queue_item(self, queue_id: str, item_id: str) -> Any:
        """POST .../items/{id}/attempts — force a new attempt for a failed item.

        The v7 item lifecycle is attempt-based: there is no retry verb, retrying
        IS creating an attempt. Answers 201 with {"attemptId": n}.
        """
        return self._write("POST", f"/workqueues/{queue_id}/items/{item_id}/attempts")

    def defer_queue_item(
        self, queue_id: str, item_id: str, attempt_id: int, defer_until: str
    ) -> Any:
        """PATCH .../attempts/{attemptId} — hold an attempt until `defer_until`.

        The endpoint takes an RFC 6902 JSON Patch document, sent with the
        media type the RFC defines (application/json-patch+json) — servers
        may reject a patch list arriving as plain application/json. The spec
        does not enumerate the patchable paths; /deferredDate mirrors the item
        schema's field name and needs day-one verification against a live
        estate.
        """
        return self._write(
            "PATCH",
            f"/workqueues/{queue_id}/items/{item_id}/attempts/{attempt_id}",
            body=[{"op": "replace", "path": "/deferredDate", "value": defer_until}],
            headers={"Content-Type": "application/json-patch+json"},
        )

    def start_process(self, process_id: str, resource_id: str) -> dict:
        """Create a session for the process on the resource, then start it.

        Two-step by API design (both 7.1+): POST /sessions creates a Pending
        session and answers a bare session UUID; PATCH /sessions/{id} with
        {"status": "Running"} requests the run. The split suits Phase 5's
        dry-run: a dry run can stop after the POST.
        """
        session_id = self._write(
            "POST",
            "/sessions",
            body={"processId": process_id, "resourceId": resource_id},
        )
        self._write("PATCH", f"/sessions/{session_id}", body={"status": "Running"})
        return {"sessionId": session_id, "status": "Running"}

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> Any:
        """PUT /schedules/{id} — retire (disable) or unretire a schedule.

        v7's enable concept is retirement: ScheduleSummary carries isRetired and
        the PUT documents retire/unretire permissions, but the published request
        schema omits the flag — verify the accepted body against a live estate
        on day one.
        """
        return self._write(
            "PUT",
            f"/schedules/{schedule_id}",
            body={"isRetired": not enabled},
        )

    def trigger_schedule(self, schedule_id: str, start_time: str | None = None) -> Any:
        """POST /schedules/{id}/runs — run a schedule now, or at `start_time`."""
        body = {"startTime": start_time} if start_time else {}
        return self._write("POST", f"/schedules/{schedule_id}/runs", body=body)

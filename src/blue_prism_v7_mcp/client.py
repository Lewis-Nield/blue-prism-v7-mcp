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

from .cache import MISS, Cache, TTLCache
from .config import BPConfig

logger = logging.getLogger("blue_prism_v7_mcp.client")

# Refresh the OAuth2 token this many seconds before its stated expiry, so a
# token never goes stale mid-request. The 401 retry in _request remains the
# safety net for clock skew or server-side revocation.
_TOKEN_EXPIRY_SKEW = 60.0

# The session-log "errors-only" filter. The spec exposes no boolean error flag;
# the Blue Prism error markers are the exception-handling stage types, so an
# errors-only read narrows server-side to the stages where an exception was
# raised (Exception), caught (Recover), or resumed past (Resume). stageType is
# a form-style array param, so the values go comma-joined.
_ERROR_STAGE_TYPES = "Exception,Recover,Resume"

# Page budget for the last-run sweep (get_latest_schedule_runs). Sorted
# newest-first, the first few pages of the plural schedule log cover every
# schedule that has run recently — the common case — so the sweep answers the
# whole estate in one paged read instead of one request per schedule. Schedules
# still unseen after this many pages are long-dormant outliers; they fall back
# to the bounded per-schedule read rather than walking the full run history.
_LATEST_RUNS_MAX_PAGES = 3


class BPClient:
    """Stateful client for one Blue Prism v7 estate.

    Holds the injected config, the bearer token, a reused HTTP session, and a
    read cache. The cache is injectable (DESIGN Phase 8): the default is a
    thread-safe per-instance `TTLCache`, but a long-lived multi-threaded host
    embedding the engine can supply a shared store behind the `Cache` protocol.
    Read methods mirror the v7 entities; write methods (Phase 2/5) are only
    surfaced as MCP tools when config.enable_actions is True.
    """

    def __init__(
        self,
        config: BPConfig,
        session: requests.Session | None = None,
        cache: Cache | None = None,
    ) -> None:
        self._config = config
        self._session = session or requests.Session()  # pools TCP connections
        self._token: str | None = None
        self._token_expiry: float = 0.0  # monotonic deadline; 0 → no token
        self._cache = cache if cache is not None else TTLCache(config.cache_ttl)
        # Cache keys are namespaced by estate so an injected store shared across
        # clients (e.g. one Redis backing several processes) never serves one
        # estate's reads to another — keys like "resources" would otherwise
        # collide. The default per-instance TTLCache doesn't need this, but the
        # injectable seam invites sharing, so the isolation lives at the key.
        self._cache_ns = config.base_url
        # Session-log endpoint pin: None until /logs is proven the only option,
        # then "logs" for the life of the instance (see get_session_log).
        self._log_path: str | None = None

    def clear_cache(self) -> None:
        """Drop all cached reads (mirrors st.cache_data.clear()).

        Clears the whole backing store. With the default per-instance cache that
        is exactly this client's reads; with an injected store shared across
        clients it also evicts the others' (the minimal ``Cache`` protocol has no
        scoped clear) — fresh-but-broad, the safe direction after a mutation.
        """
        self._cache.clear()

    # --- Auth ---------------------------------------------------------------

    def _get_token(self) -> str:
        """Return a bearer token, fetching a fresh one when absent or near expiry.

        v7 auth is OAuth2 client-credentials against the Blue Prism
        Authentication Server — a form-encoded POST to <auth_url>/connect/token
        returning a JWT (the only scheme the API documents; identical across
        7.0–7.5). The API's security requires the "bp-api bpserver" scope pair;
        an empty token_scope omits the param so the server issues all scopes the
        client registration allows (the auth guide's documented request shape).
        expires_in is honoured with a skew so the token is refreshed before the
        server would reject it.
        """
        if self._token and time.monotonic() < self._token_expiry:
            return self._token
        data = {
            "grant_type": "client_credentials",
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        }
        # Empty scope → omit the param (documented fallback); otherwise send the
        # cleaned value so stray whitespace never reaches the token endpoint.
        scope = self._config.token_scope.strip()
        if scope:
            data["scope"] = scope
        resp = self._session.post(
            f"{self._config.auth_url}/connect/token",
            data=data,
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

    def _get_collection(
        self, path: str, base_params: dict | None = None, max_records: int | None = None
    ) -> list:
        """Fetch every page of a collection endpoint and return a flat list.

        Paging behaviour is governed by config.paging_mode:
          none   — single request
          token  — follow next-page tokens until exhausted
          offset — advance the offset by items fetched until a short page
          auto   — detect token vs offset from the first response (default)

        `max_records`, if given, stops the loop as soon as at least that many
        rows are collected — a fetch-time cap, not a truncation of the final
        list (the caller may still hold more than `max_records` rows if the
        last page overshoots). Only sound when the endpoint's ordering
        already puts the wanted rows first (e.g. a server-side `sortBy`); it
        does not itself impose an order. Under `paging_mode="none"`, there is
        only one request regardless — `max_records` saves nothing at fetch
        time there, only at the caller's own result-size cap.
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

            if max_records is not None and len(collected) >= max_records:
                break

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

    def _get_paged_by_number(
        self, path: str, base_params: dict | None = None, *, page_size: int | None = None
    ) -> list:
        """Fetch every page of a page-NUMBER-paged endpoint and return a flat list.

        `resourceUtilization` is the API's one endpoint paged by pageNumber/
        pageSize rather than the itemsPerPage/pagingToken token scheme every
        other collection uses (see get_resource_utilization) — kept generic,
        not a one-off, so a future page-number-paged read can reuse it. Pages
        start at 1 (the API's convention; distinct from the offset scheme's
        0-based start) and the loop stops on a short page (fewer items than
        requested) or an empty one, capped by max_pages like the token/offset
        loop.
        """
        cfg = self._config
        size = page_size or cfg.page_size
        params = dict(base_params or {})
        params["pageSize"] = size
        collected: list = []
        for page in range(1, cfg.max_pages + 1):
            params["pageNumber"] = page
            body = self._get(path, params=params)
            items, _ = self._unpack_page(body)
            collected.extend(items)
            if len(items) < size:
                break
        else:
            logger.warning(
                "Page-number pagination hit max_pages (%d) for %s — results may be incomplete",
                cfg.max_pages,
                path,
            )
        return collected

    def _cached(self, key: Any, produce: Callable[[], Any]) -> Any:
        """Return a cached read for `key`, computing and storing it on a miss.

        The key is namespaced by estate (see ``_cache_ns``) so a shared cache
        never crosses estates.
        """
        namespaced = (self._cache_ns, key)
        hit = self._cache.get(namespaced)
        if hit is not MISS:
            return hit
        value = produce()
        self._cache.set(namespaced, value)
        return value

    # --- Tier 1 reads -------------------------------------------------------

    def get_resources(self) -> list[dict]:
        """GET /resources — digital workers and their status."""
        return self._cached("resources", lambda: self._get_collection("/resources"))

    def get_queues(self) -> list[dict]:
        """GET /workqueues — work-queue health."""
        return self._cached("queues", lambda: self._get_collection("/workqueues"))

    def get_queue(self, queue_id: str) -> dict:
        """GET /workqueues/{id} — one queue's full detail (WorkQueueSummary).

        WorkQueueSummary carries pending/completed/locked/exceptioned/total
        counts and averageWorkTime, but NOT deferred. The /dashboards/
        workQueueCompositions aggregate is consumed only for that one missing
        count, which the list_queues tool folds in over its result set (this
        single-queue read does not — see get_queue_compositions and DESIGN.md).
        """
        return self._cached(("queue", queue_id), lambda: self._get(f"/workqueues/{queue_id}"))

    def get_schedules(self) -> list[dict]:
        """GET /schedules — schedules, next runs, last outcome."""
        return self._cached("schedules", lambda: self._get_collection("/schedules"))

    def get_last_schedule_run(self, schedule_id) -> dict | None:
        """GET /scheduleLogs/{id} — one schedule's most recent run, or None.

        The schedule definition (get_schedules) carries no run history; per-run
        outcomes live in the schedule logs. This reads the *current* endpoint
        (`/scheduleLogs/{scheduleId}`, ScheduleLogSummary) — the parallel
        `/schedules/{id}/logs` is the spec's *deprecated* variant. Ordered
        newest-first (sortBy=StartTimeDesc) and capped to a single row
        (itemsPerPage=1), so the answer is just the schedule's last run —
        scheduleLogId, start/end, duration, and status (pending/running/
        terminated/completed/partExceptioned) — without dragging its whole
        history. A schedule that has never run answers an empty page (→ None);
        the list_schedules tool folds the outcome in only where one is present.
        """

        def fetch() -> dict | None:
            body = self._get(
                f"/scheduleLogs/{schedule_id}",
                params={"sortBy": "StartTimeDesc", "itemsPerPage": 1},
            )
            items, _ = self._unpack_page(body)
            return items[0] if items else None

        return self._cached(("last_schedule_run", str(schedule_id)), fetch)

    def get_latest_schedule_runs(self, schedule_ids: list) -> dict[str, dict]:
        """Map schedule id (as str) → its most recent run, in one paged sweep.

        The per-schedule read (get_last_schedule_run) costs one request per
        schedule per refresh — the N-calls bottleneck at hundreds of schedules.
        The plural `GET /scheduleLogs` (7.1+) carries scheduleId/scheduleName on
        every ScheduleLogSummary row, so one newest-first sweep yields the
        latest run for every schedule that has run recently: the first row seen
        per schedule IS its last run. The sweep stops as soon as every wanted
        schedule is covered (or the page budget runs out — see
        _LATEST_RUNS_MAX_PAGES), and only the leftovers — long-dormant
        schedules whose last run predates the swept window, or a gateway that
        doesn't token-page — fall back to the per-schedule read, so the worst
        case is the old behaviour and the common case is one request. A
        schedule that has never run appears in no log and is absent from the
        result (the caller folds nothing in — never a fabricated outcome).
        Keys are strings both ways (schedule ids are integers in the API, but
        interpolate into paths as strings everywhere in this client).
        """
        wanted = {str(sid) for sid in schedule_ids if sid is not None}
        if not wanted:
            return {}

        def fetch() -> dict[str, dict]:
            cfg = self._config
            runs: dict[str, dict] = {}
            params: dict = {"sortBy": "StartTimeDesc", cfg.page_size_param: cfg.page_size}
            token: str | None = None
            for _ in range(_LATEST_RUNS_MAX_PAGES):
                if token is not None:
                    params[cfg.page_token_param] = token
                body = self._get("/scheduleLogs", params=params)
                items, token = self._unpack_page(body)
                for row in items:
                    sid = str(row.get("scheduleId"))
                    if sid in wanted and sid not in runs:
                        runs[sid] = row
                if not token or wanted <= runs.keys():
                    break
            for sid in sorted(wanted - runs.keys()):
                run = self.get_last_schedule_run(sid)
                if isinstance(run, dict):
                    runs[sid] = run
            return runs

        return self._cached(("latest_schedule_runs", tuple(sorted(wanted))), fetch)

    def get_schedule(self, schedule_id) -> dict:
        """GET /schedules/{id} — one schedule's full definition (7.1+).

        Richer than a /schedules list row: ScheduleDefinitionResponseModel adds
        the complete interval definition — intervalType, start/end dates,
        timeZoneId, the daylight-saving flag, and the per-interval detail
        objects (hourly/minutely/daily/weekly/monthly/yearly, each carrying its
        calendarId) — everything needed to reason about when the schedule
        should run. Schedule ids are integers (the one non-UUID id in the API).
        """
        return self._cached(
            ("schedule", str(schedule_id)),
            lambda: self._get(f"/schedules/{schedule_id}"),
        )

    def get_schedule_tasks(self, schedule_id) -> list[dict]:
        """GET /schedules/{id}/tasks — the schedule's task chain (7.0+).

        A bare array of ScheduledTask (no paging): each task's integer id,
        name, description, failFastOnError, delayAfterEnd, sessionsCount, and
        the onSuccessTaskId/onFailureTaskId links that chain tasks together
        from the schedule's initialTaskId. An empty/204 body coerces to [] so
        the return is always a list.
        """
        return self._cached(
            ("schedule_tasks", str(schedule_id)),
            lambda: self._get(f"/schedules/{schedule_id}/tasks") or [],
        )

    def get_task_sessions(self, task_id) -> list[dict]:
        """GET /schedules/tasks/{taskId}/sessions — what a task runs, and where.

        A bare array of {processName, resourceName, taskSessionId} — the
        process each scheduled session executes and the resource it targets.
        The schedule-less path is used by design (task ids are unique, and it
        is the 7.0 form; the schedule-scoped twin adds nothing). Note the
        response carries names, not ids. An empty/204 body coerces to [].
        """
        return self._cached(
            ("task_sessions", str(task_id)),
            lambda: self._get(f"/schedules/tasks/{task_id}/sessions") or [],
        )

    def get_schedule_logs(
        self,
        schedule_id=None,
        status: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """GET /scheduleLogs (or /scheduleLogs/{id}) — schedule run history.

        The *current* schedule-log family (the parallel /schedules/logs is the
        spec's deprecated variant). With a schedule_id the read scopes to that
        schedule's runs; without one it sweeps every schedule (each row carries
        scheduleId and scheduleName). Filters go server-side: scheduleLogStatus
        takes the Capitalised query enum (Pending/Running/Terminated/Completed/
        PartExceptioned — responses spell status lowercase), and the window is
        a deepObject range on startTime. Ordered newest-first
        (sortBy=StartTimeDesc), token-paged like the other collections.
        """
        params: dict[str, str] = {"sortBy": "StartTimeDesc"}
        if status:
            params["scheduleLogStatus"] = status
        if start_date:
            params["startTime[gte]"] = start_date
        if end_date:
            params["startTime[lte]"] = end_date
        path = f"/scheduleLogs/{schedule_id}" if schedule_id is not None else "/scheduleLogs"
        return self._cached(
            ("schedule_logs", str(schedule_id), status, start_date, end_date),
            lambda: self._get_collection(path, base_params=params),
        )

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
        within_sla: bool | None = None,
        sla_before: str | None = None,
        sort_by: str | None = None,
        max_records: int | None = None,
    ) -> list[dict]:
        """GET /workqueues/{id}/items — items in one queue, optionally filtered.

        A single queue can hold millions of items, so the tool layer (Phase 4)
        requires a state and a date window UNLESS the query is already scoped
        by `within_sla`/`sla_before` (see below). The client stays thin and
        speaks the spec's filter vocabulary: `state` is the lifecycle enum
        (Pending/Locked/Deferred/Completed/Exceptioned), `status` is the free
        user-supplied text (FullStringFilter → status[eq]), and the date
        window goes on lastUpdated[gte]/[lte] — the one timestamp every item
        carries regardless of state (completedDate is null for pending items).

        List responses are WorkQueueItemNoData: the API excludes item payload
        data from lists by design; exceptionReason is still present (scrubbed
        at the tool boundary). Rows also carry `sessionId` — the session that
        worked the item, the item→session/resource correlation (there is no
        item-level exception-type field; that classification lives on the
        session, via `get_session(row["sessionId"])`) — plus `sla`/
        `slaDatetime`, `loadedDate`, `processName`, and `tags`.

        Three narrowing/ordering params beyond the base filters (v0.12.0):
        `within_sla` sends the computed EqualsFilter `withinSla[eq]` (`true`/
        `false`) — a breach query (`within_sla=False`) is itself scope enough
        that the tool layer does not also require a date window. `sla_before`
        sends the RangeOrEqualFilter upper bound `slaDateTime[lte]` (an
        approaching-SLA window). `sort_by` passes straight through to the
        API's `sortBy` enum (e.g. `LoadedDateAsc`) so a max-pages-capped fetch
        still returns the true oldest/most-relevant items first, rather than
        relying on a local re-sort of a possibly-truncated page set.

        `max_records` (v0.15.0) is an exact cap on the returned list — it
        stops paging once that many rows are in hand (e.g.
        `sort_by="LoadedDateAsc"` + `max_records=1` for "the single oldest
        item"), then slices the result down to exactly that count, since the
        last page fetched may overshoot. It is only meaningful paired with a
        `sort_by` that puts the wanted rows first; without one, the API's own
        default order is unspecified and an early-stopped fetch is an
        arbitrary subset, not a top-N. Not exposed on the MCP tool surface
        for that reason — it is a domain/embeddable-core primitive for
        callers that control ordering.
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
        if within_sla is not None:
            params["withinSla[eq]"] = "true" if within_sla else "false"
        if sla_before:
            params["slaDateTime[lte]"] = sla_before
        if sort_by:
            params["sortBy"] = sort_by

        def fetch() -> list:
            collected = self._get_collection(
                f"/workqueues/{queue_id}/items", base_params=params or None, max_records=max_records
            )
            return collected[:max_records] if max_records is not None else collected

        return self._cached(
            (
                "queue_items",
                queue_id,
                state,
                status,
                start_date,
                end_date,
                within_sla,
                sla_before,
                sort_by,
                max_records,
            ),
            fetch,
        )

    def get_queue_item(self, item_id: str) -> dict:
        """GET /workqueues/items/{id} — one item WITH its payload `data`.

        The only read in the surface that returns WorkQueueItem (the `data`
        DataCollection); list/attempt reads return WorkQueueItemNoData. The
        queue-less path is used by design — the item UUID is globally unique,
        so the caller needs only the id it already has from a list. The tool
        layer scrubs the payload before it reaches the model.

        Spec caveat: this endpoint does not support application-server-based
        encryption keys — only unencrypted or database-key-encrypted queues
        answer here; an app-server-encrypted queue's item returns a 4xx (raised
        as an HTTPError), which the tool surfaces rather than masks.

        Carries the same `sessionId` correlation field as the list read, plus
        `sla`/`slaDateTime` (this single-item shape spells it with a capital
        T; the list/attempt NoData rows spell it `slaDatetime`), `loadedDate`,
        `processName`, and `tags`.
        """
        return self._cached(
            ("queue_item", item_id), lambda: self._get(f"/workqueues/items/{item_id}")
        )

    def get_item_attempts(self, queue_id: str, item_id: str) -> list[dict]:
        """GET /workqueues/{queueId}/items/{itemId}/attempts — an item's attempt
        history.

        Queue-scoped (the attempts path needs both ids, unlike the queue-less
        single-item read). Answers an array of WorkQueueItemNoData — one row per
        attempt, newest carrying the live state — with no payload `data` but
        with exceptionReason present (scrubbed at the tool boundary). Each row
        carries the `sessionId` of the session that worked THAT attempt (None
        for an attempt no session has picked up yet) alongside `resource` —
        the item→session→resource correlation per attempt, not just per item.
        Attempt counts are small (bounded by the queue's maxAttempts), so this
        is a single unpaged request.
        """
        return self._cached(
            ("item_attempts", queue_id, item_id),
            lambda: self._get(f"/workqueues/{queue_id}/items/{item_id}/attempts"),
        )

    def get_session(self, session_id: str) -> dict:
        """GET /sessions/{id} — one session's detail (SessionSummary).

        The single-session sibling of get_sessions: same SessionSummary shape as
        a list row (status, timings, latestStage, exceptionMessage/Type,
        terminationReason), addressed directly by id with no date window. The
        tool layer scrubs exceptionMessage before it reaches the model.
        """
        return self._cached(("session", session_id), lambda: self._get(f"/sessions/{session_id}"))

    def get_session_log(
        self,
        session_id: str,
        errors_only: bool = False,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """GET /sessions/{id}/logslight, falling back to /logs — the stage log.

        The highest-value agentic read ("why did this run fail?"). Stage results
        can carry item payloads, so the tool layer routes the result through the
        PII scrubber; the client returns it raw.

        The read pushes the v7 server-side filters the endpoint supports so a
        long run need not be dragged back in full: ``errors_only`` narrows to the
        exception-handling stage types (Exception/Recover/Resume — see
        _ERROR_STAGE_TYPES), and start_date/end_date bound the per-stage
        execution time as a deepObject range on resourceStartTime
        (resourceStartTime[gte]/[lte], the spec's RangeOrEqualFilter). The pages
        are ordered newest-stage-first server-side (sortBy=LogNumberDesc) so a
        consumer reading the raw client output sees failures — which end a log —
        first; the tool/engine still ranks for the embeddable guarantee.

        logslight (7.4+) is shape-identical to /logs — same parameters, same
        SessionLogSummary items (verified field-by-field against the 7.5.1
        spec); the "light" is a cheaper server-side query. So probe it first
        and fall back on a 404. The fallback only PINS /logs for the life of
        the instance when /logs then succeeds: a 404 with a failing fallback
        means the session id itself is unknown (both endpoints 404 on that),
        and must not demote a 7.4+ estate to the slow path forever.
        """
        params: dict[str, str] = {"sortBy": "LogNumberDesc"}
        if errors_only:
            params["stageType"] = _ERROR_STAGE_TYPES
        if start_date:
            params["resourceStartTime[gte]"] = start_date
        if end_date:
            params["resourceStartTime[lte]"] = end_date

        def produce() -> list:
            if self._log_path == "logs":
                return self._get_collection(f"/sessions/{session_id}/logs", base_params=params)
            try:
                return self._get_collection(f"/sessions/{session_id}/logslight", base_params=params)
            except requests.HTTPError as exc:
                if exc.response is None or exc.response.status_code != 404:
                    raise
                items = self._get_collection(f"/sessions/{session_id}/logs", base_params=params)
                self._log_path = "logs"
                return items

        return self._cached(("session_log", session_id, errors_only, start_date, end_date), produce)

    def get_current_limits_and_usage(self) -> dict:
        """GET /dashboards/currentLimitsAndUsage — licence limits vs usage.

        One of three /dashboards reads this server consumes (with
        licensesEntitlement and workQueueCompositions; the resource-utilization
        endpoints stay out as dashboard MI — see DESIGN.md). Limits are nullable
        (null = unlimited); usages are current counts. Backs the estate_health
        tool's licence block.
        """
        return self._cached(
            "limits_and_usage",
            lambda: self._get("/dashboards/currentLimitsAndUsage"),
        )

    def get_license_entitlement(self) -> dict:
        """GET /dashboards/licensesEntitlement — what the estate is licensed for.

        The entitlement side of the licence picture, complementing
        currentLimitsAndUsage (limits vs current usage): the active licence
        types plus per-tier ceilings (enterprise and desktop) for published
        processes, concurrent sessions, runtime resources, and process-alert
        machines. No params, single object (LicensesEntitlement). Requires the
        "System - License" permission. Backs the license_entitlement tool.
        """
        return self._cached(
            "license_entitlement",
            lambda: self._get("/dashboards/licensesEntitlement"),
        )

    def get_resource_utilization(self, start_date: str) -> list[dict]:
        """GET /dashboards/resourceUtilization — per-worker daily heat-map.

        The third `/dashboards` read this server consumes (with
        currentLimitsAndUsage and licensesEntitlement): one row per worker per
        day from `start_date` onward — {resourceId, digitalWorkerName,
        utilizationDate, usages} where `usages` is 24 ints of minutes worked
        per hour. The API's only page-NUMBER-paged read (pageNumber/pageSize,
        not the itemsPerPage/pagingToken scheme everywhere else — see
        _get_paged_by_number) and its only param is `start_date`; there is no
        end-bound param, so the resource_utilization tool filters the returned
        rows down to its window client-side. Backs the resource_utilization
        Tier-2 tool's aggregation; left out as a raw read (see DESIGN.md) since
        a heat-map feed is chart MI, not an LLM-shaped fact.

        Cached on `start_date` alone (unlike the other windowed reads, which
        key on both bounds) because that is the read's whole input — the fetch
        itself is already unbounded above `start_date`, so a second call with
        the same start and a different end_date correctly reuses it rather
        than repeating the same page walk.
        """
        return self._cached(
            ("resource_utilization", start_date),
            lambda: self._get_paged_by_number(
                "/dashboards/resourceUtilization", base_params={"startDate": start_date}
            ),
        )

    def get_queue_compositions(self, queue_ids: list[str]) -> list[dict]:
        """GET /dashboards/workQueueCompositions — per-queue item-state counts.

        The one datum /workqueues' WorkQueueSummary does not carry is the
        per-queue *deferred* count; this aggregate adds it (id, name, plus
        completed/pending/deferred/locked/exceptioned). workQueueIds is a
        REQUIRED array, so callers pass the ids they already hold from
        get_queues — there is no estate-wide form. The array goes as repeated
        query params (workQueueIds=a&workQueueIds=b), the OpenAPI form/explode
        default. Ids are sorted so the same id *set* shares one cache entry
        regardless of request order. An empty id list short-circuits to no
        request; an empty/204 body coerces to [] so the return is always a list.
        """
        if not queue_ids:
            return []
        ids = tuple(sorted(queue_ids))
        return self._cached(
            ("queue_compositions", ids),
            lambda: (
                self._get(
                    "/dashboards/workQueueCompositions",
                    params={"workQueueIds": list(ids)},
                )
                or []
            ),
        )

    def get_queue_configurations(self) -> list[dict]:
        """GET /workqueues/configurations — the active queues' process/resource map.

        Each active work queue's assigned process and resource group, plus its
        live activity (active sessions, available resources, time/ETA estimates)
        — the topology that links a queue to the work that drains it, which the
        plain /workqueues listing does not carry. Token-paged like the other
        collections.

        Introduced at 7.4 (the one endpoint in this surface above the 7.2 floor):
        a 7.2/7.3 estate 404s here, which the tool layer degrades to a visible
        "unavailable" note rather than letting it fail the read surface.
        """
        return self._cached(
            "queue_configurations",
            lambda: self._get_collection("/workqueues/configurations"),
        )

    def get_resource_pools(self) -> list[dict]:
        """GET /resources/pools — the resource pools and their membership.

        Each pool's name, member count, and reported database status. Unlike the
        other collection reads this endpoint answers a bare array with no paging
        envelope, so it is a single unpaged request; an empty/204 body coerces to
        [] so the return is always a list.
        """
        return self._cached(
            "resource_pools",
            lambda: self._get("/resources/pools") or [],
        )

    def get_environment_variables(self) -> list[dict]:
        """GET /environmentvariables — the estate's environment-variable catalogue.

        Each variable's name, description, Blue Prism data type, and value. The
        value is a configuration payload that can hold a secret (a Password-typed
        variable) or personal data (free text), so the tool layer scrubs it
        type-aware before it reaches the model — the same fail-closed policy as
        the queue-item payload. Token-paged.
        """
        return self._cached(
            "environment_variables",
            lambda: self._get_collection("/environmentvariables"),
        )

    def get_process_groups(self) -> list[dict]:
        """GET /processgroups/root/descendants — the process-group tree.

        A flat list of every node under the process tree root — each a process
        (an Item) or a folder (a Group) with its name and last-modified time —
        so an agent can see how the process catalogue is organised. Token-paged.
        """
        return self._cached(
            "process_groups",
            lambda: self._get_collection("/processgroups/root/descendants"),
        )

    def get_user_permissions(self) -> list[str]:
        """GET /user/permissions — the service account's Blue Prism permissions.

        Answers a flat JSON array of permission-name strings (verified against
        the 7.5.1 spec; the endpoint exists from 7.1, so the whole 7.2+ floor
        has it). The Phase 5 capability resolver reads this once at startup
        and registers only the action tools the account can actually execute,
        so the shape is validated strictly: a gateway envelope, an error page,
        or a future schema change must refuse loudly rather than gate the
        action surface on garbage (a dict would silently iterate as its keys).
        """

        def fetch() -> list[str]:
            body = self._get("/user/permissions")
            if not isinstance(body, list):
                raise ValueError(
                    f"GET /user/permissions returned {type(body).__name__}, "
                    "expected a JSON array of permission-name strings: "
                    f"{str(body)[:120]!r}"
                )
            bad = [p for p in body if not isinstance(p, str) or not p.strip()]
            if bad:
                raise ValueError(
                    "GET /user/permissions returned entries that are not "
                    f"non-empty strings: {bad[:5]!r} — expected a JSON array "
                    "of permission-name strings"
                )
            return body

        return self._cached("user_permissions", fetch)

    # --- Tier 3 writes ------------------------------------------------------
    # Designed in, shipped disabled: these issue real v7 writes, and the MCP
    # tools over them (tools/tier3.py) register only behind the enable_actions
    # gate, capability resolver, audit log, and dry-run default (see
    # governance.py). Each mutates estate state, so
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

    def start_process(
        self, process_id: str, resource_id: str, parameters: dict | None = None
    ) -> dict:
        """Create a session for the process on the resource, set any start-up
        parameters, then start it.

        Up to three steps by API design (all 7.1+): POST /sessions creates a
        Pending session and answers a bare session UUID; when `parameters` are
        given, PUT /sessions/{id}/parameters sets them while the session is
        still Pending (the API answers 204, no body); PATCH /sessions/{id} with
        {"status": "Running"} requests the run. The split suits Phase 5's
        dry-run: a dry run can stop before the POST.
        """
        session_id = self._write(
            "POST",
            "/sessions",
            body={"processId": process_id, "resourceId": resource_id},
        )
        if parameters:
            self._write(
                "PUT",
                f"/sessions/{session_id}/parameters",
                body={"parameters": parameters},
            )
        return self._set_session_status(session_id, "Running")

    def stop_session(self, session_id: str) -> dict:
        """Request a running session stop — start_process's control sibling.

        Same endpoint and shape start_process drives for Running (PATCH
        /sessions/{id}, 7.1+): {"status": "Stopped"} asks Blue Prism to stop
        the session. The stop is a request the runtime honours when the process
        next yields, not an instant kill; the answering status reflects what
        was requested.
        """
        return self._set_session_status(session_id, "Stopped")

    def _set_session_status(self, session_id: str, status: str) -> dict:
        """PATCH /sessions/{id} {status} — the one session-control write both
        start_process (Running) and stop_session (Stopped) drive, kept in one
        place so the endpoint contract can't drift between the two."""
        self._write("PATCH", f"/sessions/{session_id}", body={"status": status})
        return {"sessionId": session_id, "status": status}

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

    def stop_schedule(self, schedule_id: str) -> Any:
        """DELETE /schedules/{id}/runs/active — request a running schedule to stop.

        trigger_schedule's incident sibling: it cancels the schedule's active
        runs. Like the runtime stop_session, this is a request the server
        honours when the work next yields, not an instant kill. Answers 202
        with no body (→ None).
        """
        return self._write("DELETE", f"/schedules/{schedule_id}/runs/active")

    def create_queue_items(self, queue_id: str, items: list[dict]) -> dict:
        """POST /workqueues/{id}/items — inject work items into a queue.

        Batch-first: the API body is always an array (a single create is a
        batch of one). Answers 201 with {"ids": [...]}, one UUID per item
        created, in the same order as the request array.
        """
        return self._write("POST", f"/workqueues/{queue_id}/items", body=items)

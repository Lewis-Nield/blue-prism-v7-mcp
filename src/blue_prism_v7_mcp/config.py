"""Per-deployment configuration.

Unlike the dashboard (module-level globals read from the environment at import),
this is an INJECTED config object so two estates / two server instances never
collide. BPClient takes one of these on construction; nothing in the client
reads the environment directly.

The connection contract follows the official v7 API OpenAPI specs (verified
against 7.0.1–7.5.1, identical throughout): OAuth2 client-credentials against
the Blue Prism Authentication Server, token paging via itemsPerPage/pagingToken.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BPConfig:
    """Connection + behaviour settings for a single deployment."""

    # The API and the Authentication Server are separate services (typically
    # separate hosts/ports in a standard install), hence two base URLs.
    base_url: str = ""  # e.g. https://bpapi.example.com/api/v7
    auth_url: str = ""  # e.g. https://auth.example.com — token endpoint base

    # OAuth2 client-credentials (the only scheme the v7 API documents). The
    # client posts these form-encoded to <auth_url>/connect/token and uses the
    # returned JWT as a bearer token.
    client_id: str = ""
    # repr=False: an embedding host that logs or reprs its config must never
    # echo the credential.
    client_secret: str = field(default="", repr=False)
    # The API's OpenAPI security requires BOTH scopes on every endpoint
    # ("bp-api bpserver"); requesting only "bp-api" yields a token the API
    # rejects. Set empty to omit the scope param entirely — matching the auth
    # guide's documented token request, which lets the Authentication Server
    # issue all scopes the client registration allows.
    token_scope: str = "bp-api bpserver"

    verify_ssl: bool = True

    # Per-request timeout (seconds). A 1000-item page from a busy estate can be
    # slow, so this is deliberately configurable rather than hard-coded.
    request_timeout: float = 30.0

    # Read-cache TTL (seconds). Replaces the dashboard's @st.cache_data(ttl=30):
    # repeated reads within this window reuse the prior result instead of
    # re-hitting the live API. Per-instance, so two estates never share a cache.
    cache_ttl: float = 30.0

    # Pagination — v7 is token-paged on every collection endpoint (verified
    # across 7.0–7.5), so "token" is the default. The offset/auto modes and the
    # param/key names remain configurable as an escape hatch for gateways or
    # proxies that reshape responses; only the names change here, never the
    # client code.
    paging_mode: str = "token"  # token | offset | none | auto
    page_size: int = 1000
    max_pages: int = 1000  # runaway-loop safety cap

    # Request param names carrying the page size, next-page token, and offset.
    page_size_param: str = "itemsPerPage"
    page_token_param: str = "pagingToken"
    page_offset_param: str = "startIndex"

    # --- Transport governance (DESIGN Phase 12) --------------------------------
    # The engine ships the mechanism; a deployment supplies the numbers. Every
    # knob here defaults to OFF, so an unconfigured server emits exactly the
    # traffic it did before transport governance existed — a distributable
    # artifact must not quietly change its posture on upgrade. A host that knows
    # its estate (concurrent users, poll cadence, how much headroom the BP
    # application server has) sets these; the engine has no basis to guess.

    # Sustained request ceiling against base_url, requests/second. 0 disables
    # the limiter entirely (no token bucket is constructed).
    max_requests_per_second: float = 0.0
    # Idle headroom the limiter may release at once. Only meaningful when
    # max_requests_per_second is set.
    max_burst: int = 10
    # Ceiling on requests in flight at once, enforced by a semaphore. 0 means
    # unbounded. Matters for an embedding host whose thread pool is far wider
    # than anything the estate wants to field concurrently.
    max_concurrency: int = 0
    # The single end-to-end budget a caller will wait for a request slot —
    # covering the concurrency semaphore and the limiter token together, so
    # these never become two stacked timeouts. Exhausting it raises
    # TransportBudgetExceeded rather than dropping or queueing the request.
    limiter_timeout_seconds: float = 10.0

    # Connection-pool size for the mounted HTTPAdapter. 0 leaves requests'
    # own default in place (no adapter is mounted). The effective size is
    # raised to max_concurrency where that is larger, so the pool can never be
    # the narrower limit — above pool_maxsize, requests opens and discards
    # extra connections, which is TLS re-handshakes against the estate at
    # exactly the wrong moment.
    pool_maxsize: int = 0

    # Retries per request for transient failures (429 honouring Retry-After;
    # 502/503/504 with jittered exponential backoff). 0 disables the retry
    # layer. Reads only: a write is never retried, since a repeated
    # start_process is a duplicate estate run. The single 401 re-auth is
    # orthogonal and always active.
    max_retries: int = 0
    retry_base_delay: float = 0.5

    # Feature flags.
    enable_actions: bool = False  # gates the Tier 3 control tools

    # What the server runs against: "live" (BPClient over the v7 REST API),
    # "mock" (MockBPClient — the lean in-memory fixtures, no estate, no
    # credentials), or "demo" (the same client seeded with a larger, populated
    # estate — see mock.demo_estate). Mock and demo modes exist so the server
    # can be evaluated end-to-end in an MCP client with zero estate access.
    # Unknown values fail loud in the server's client factory, mirroring how
    # pii_backend fails in build_scrubber.
    data_source: str = "live"

    # JSON-lines audit file for the action surface. REQUIRED when
    # enable_actions is true (build_audit_log fails loud without it) — an
    # enabled action surface must never run unaudited. Never stdout.
    audit_log_path: str = ""

    # PII scrubbing backend: "null" (pass-through, the light-install default),
    # "regex" (zero-dependency UK FS patterns), or "presidio" (NER, needs the
    # [pii] extra). Selection FAILS LOUD at scrubber construction — see
    # pii.build_scrubber — never a silent fallback.
    pii_backend: str = "null"
    pii_spacy_model: str = "en_core_web_sm"
    # Deployment-specific patterns, e.g. client reference formats. Prepended to
    # the built-ins, so they win on overlapping spans (and run as score-1.0
    # recognizers under Presidio). Tuples of (ENTITY_NAME, regex).
    pii_custom_patterns: tuple[tuple[str, str], ...] = ()

    # Item-key / token-key candidates for unpacking paged responses.
    page_items_keys: tuple[str, ...] = ("items", "data", "results", "values")
    page_token_keys: tuple[str, ...] = ("pagingToken", "nextPageToken", "next")

    def __post_init__(self) -> None:
        # Strip trailing slashes so a base URL + an absolute path ("/resources",
        # "/connect/token") can't produce '//'. Some deployments/proxies route
        # '.../v7//resources' as a distinct path and fail it. Normalise once
        # here, at the single boundary where the values enter, so every call
        # site stays a plain concatenation. (frozen=True → object.__setattr__.)
        object.__setattr__(self, "base_url", self.base_url.strip().rstrip("/"))
        object.__setattr__(self, "auth_url", self.auth_url.strip().rstrip("/"))
        # Credentials: whitespace around a pasted value is never part of an
        # OAuth client id/secret, but it IS truthy — left in place it would
        # sail past the server's missing-settings check and fail only at the
        # first token fetch. Stripped here, a space-only value is correctly
        # reported as missing at startup.
        object.__setattr__(self, "client_id", self.client_id.strip())
        object.__setattr__(self, "client_secret", self.client_secret.strip())
        # Same rule for the audit path: whitespace a human pastes around an
        # env value must not change which file the audit lands in (or make a
        # space-only value look configured).
        object.__setattr__(self, "audit_log_path", self.audit_log_path.strip())

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BPConfig":
        """Build config from environment variables (the deployment contract)."""
        e = env if env is not None else os.environ
        return cls(
            base_url=e.get("BP_API_BASE_URL", ""),
            auth_url=e.get("BP_AUTH_URL", ""),
            client_id=e.get("BP_CLIENT_ID", ""),
            client_secret=e.get("BP_CLIENT_SECRET", ""),
            token_scope=e.get("BP_TOKEN_SCOPE", "bp-api bpserver"),
            verify_ssl=e.get("BP_API_VERIFY_SSL", "true").lower() == "true",
            request_timeout=float(e.get("BP_API_TIMEOUT", "30")),
            cache_ttl=float(e.get("BP_API_CACHE_TTL", "30")),
            paging_mode=e.get("BP_API_PAGING_MODE", "token"),
            page_size=int(e.get("BP_API_PAGE_SIZE", "1000")),
            max_pages=int(e.get("BP_API_MAX_PAGES", "1000")),
            page_size_param=e.get("BP_API_PAGE_SIZE_PARAM", "itemsPerPage"),
            page_token_param=e.get("BP_API_PAGE_TOKEN_PARAM", "pagingToken"),
            page_offset_param=e.get("BP_API_PAGE_OFFSET_PARAM", "startIndex"),
            max_requests_per_second=float(e.get("BP_API_MAX_REQUESTS_PER_SECOND", "0")),
            max_burst=int(e.get("BP_API_MAX_BURST", "10")),
            max_concurrency=int(e.get("BP_API_MAX_CONCURRENCY", "0")),
            limiter_timeout_seconds=float(e.get("BP_API_LIMITER_TIMEOUT", "10")),
            pool_maxsize=int(e.get("BP_API_POOL_MAXSIZE", "0")),
            max_retries=int(e.get("BP_API_MAX_RETRIES", "0")),
            retry_base_delay=float(e.get("BP_API_RETRY_BASE_DELAY", "0.5")),
            enable_actions=e.get("BP_ENABLE_ACTIONS", "false").lower() == "true",
            # Normalised like pii_backend below: casing/whitespace noise is
            # forgiven, unknown values still refuse to start in build_client.
            data_source=e.get("BP_DATA_SOURCE", "live").strip().lower(),
            audit_log_path=e.get("BP_AUDIT_LOG_PATH", ""),
            # Normalised: " ReGeX " is an obvious 'regex', and rejecting it
            # would be pedantry, not fail-loud rigour. Unknown values still
            # refuse to start in build_scrubber.
            pii_backend=e.get("BP_PII_BACKEND", "null").strip().lower(),
            pii_spacy_model=e.get("BP_PII_SPACY_MODEL", "en_core_web_sm"),
            pii_custom_patterns=_parse_pii_patterns(e.get("BP_PII_CUSTOM_PATTERNS", "")),
        )


def _parse_pii_patterns(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse BP_PII_CUSTOM_PATTERNS: a JSON array of {"name", "pattern"}.

    Mirrors the shape the dashboard used for its domain patterns. Malformed
    input raises (with the offending reason) rather than being ignored — a
    deployment that configured extra redaction must not run without it.
    """
    if not raw.strip():
        return ()
    try:
        entries = json.loads(raw)
        return tuple((str(p["name"]), str(p["pattern"])) for p in entries)
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(
            "BP_PII_CUSTOM_PATTERNS must be a JSON array of objects with "
            f'"name" and "pattern" keys, e.g. [{{"name": "CLIENT_REF", '
            f'"pattern": "CLT-\\\\d{{6}}"}}] — got {raw!r} ({exc})'
        ) from exc

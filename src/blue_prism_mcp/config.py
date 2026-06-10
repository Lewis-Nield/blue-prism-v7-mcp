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

import os
from dataclasses import dataclass


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
    client_secret: str = ""
    token_scope: str = "bp-api"

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

    # Feature flags.
    enable_actions: bool = False  # gates the Tier 3 control tools

    # Item-key / token-key candidates for unpacking paged responses.
    page_items_keys: tuple[str, ...] = ("items", "data", "results", "values")
    page_token_keys: tuple[str, ...] = ("pagingToken", "nextPageToken", "next")

    def __post_init__(self) -> None:
        # Strip trailing slashes so a base URL + an absolute path ("/resources",
        # "/connect/token") can't produce '//'. Some deployments/proxies route
        # '.../v7//resources' as a distinct path and fail it. Normalise once
        # here, at the single boundary where the values enter, so every call
        # site stays a plain concatenation. (frozen=True → object.__setattr__.)
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "auth_url", self.auth_url.rstrip("/"))

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BPConfig":
        """Build config from environment variables (the deployment contract)."""
        e = env if env is not None else os.environ
        return cls(
            base_url=e.get("BP_API_BASE_URL", ""),
            auth_url=e.get("BP_AUTH_URL", ""),
            client_id=e.get("BP_CLIENT_ID", ""),
            client_secret=e.get("BP_CLIENT_SECRET", ""),
            token_scope=e.get("BP_TOKEN_SCOPE", "bp-api"),
            verify_ssl=e.get("BP_API_VERIFY_SSL", "true").lower() == "true",
            request_timeout=float(e.get("BP_API_TIMEOUT", "30")),
            cache_ttl=float(e.get("BP_API_CACHE_TTL", "30")),
            paging_mode=e.get("BP_API_PAGING_MODE", "token"),
            page_size=int(e.get("BP_API_PAGE_SIZE", "1000")),
            max_pages=int(e.get("BP_API_MAX_PAGES", "1000")),
            page_size_param=e.get("BP_API_PAGE_SIZE_PARAM", "itemsPerPage"),
            page_token_param=e.get("BP_API_PAGE_TOKEN_PARAM", "pagingToken"),
            page_offset_param=e.get("BP_API_PAGE_OFFSET_PARAM", "startIndex"),
            enable_actions=e.get("BP_ENABLE_ACTIONS", "false").lower() == "true",
        )

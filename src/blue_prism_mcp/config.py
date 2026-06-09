"""Per-deployment configuration.

Unlike the dashboard (module-level globals read from the environment at import),
this is an INJECTED config object so two estates / two server instances never
collide. BPClient takes one of these on construction; nothing in the client
reads the environment directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BPConfig:
    """Connection + behaviour settings for a single deployment."""

    base_url: str = ""
    username: str = ""
    password: str = ""
    verify_ssl: bool = True

    # Read-cache TTL (seconds). Replaces the dashboard's @st.cache_data(ttl=30):
    # repeated reads within this window reuse the prior result instead of
    # re-hitting the live API. Per-instance, so two estates never share a cache.
    cache_ttl: float = 30.0

    # Pagination — collection endpoints vary by BP v7 deployment, so both the
    # mechanism and the response shape are configurable (ported from the
    # dashboard's BP_API_PAGING_* contract). Verify the param names and response
    # shape against your BP v7 API spec on day one; only the names change here,
    # never the client code.
    paging_mode: str = "auto"  # auto | token | offset | none
    page_size: int = 1000
    max_pages: int = 1000  # runaway-loop safety cap

    # Request param names carrying the page size, next-page token, and offset.
    page_size_param: str = "pageSize"
    page_token_param: str = "pagingToken"
    page_offset_param: str = "startIndex"

    # Feature flags.
    enable_actions: bool = False  # gates the Tier 3 control tools

    # Item-key / token-key candidates for unpacking paged responses.
    page_items_keys: tuple[str, ...] = ("items", "data", "results", "values")
    page_token_keys: tuple[str, ...] = ("pagingToken", "nextPageToken", "next")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BPConfig":
        """Build config from environment variables (the deployment contract)."""
        e = env if env is not None else os.environ
        return cls(
            base_url=e.get("BP_API_BASE_URL", ""),
            username=e.get("BP_API_USERNAME", ""),
            password=e.get("BP_API_PASSWORD", ""),
            verify_ssl=e.get("BP_API_VERIFY_SSL", "true").lower() == "true",
            cache_ttl=float(e.get("BP_API_CACHE_TTL", "30")),
            paging_mode=e.get("BP_API_PAGING_MODE", "auto"),
            page_size=int(e.get("BP_API_PAGE_SIZE", "1000")),
            max_pages=int(e.get("BP_API_MAX_PAGES", "1000")),
            page_size_param=e.get("BP_API_PAGE_SIZE_PARAM", "pageSize"),
            page_token_param=e.get("BP_API_PAGE_TOKEN_PARAM", "pagingToken"),
            page_offset_param=e.get("BP_API_PAGE_OFFSET_PARAM", "startIndex"),
            enable_actions=e.get("BP_ENABLE_ACTIONS", "false").lower() == "true",
        )

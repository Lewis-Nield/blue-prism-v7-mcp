"""Per-deployment configuration.

Unlike the dashboard (module-level globals read from the environment at import),
this is an INJECTED config object so two estates / two server instances never
collide. Phase 1 wires it into BPClient; for now this captures the intended
shape and the environment-variable contract.

TODO(Phase 1): finalise as a frozen dataclass, add `from_env()` and validation.
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

    # Pagination — collection endpoints vary by BP v7 deployment, so both the
    # mechanism and the response shape are configurable (ported from the
    # dashboard's BP_API_PAGING_* contract in Phase 1/2).
    paging_mode: str = "auto"  # auto | token | offset | none
    page_size: int = 1000
    max_pages: int = 1000

    # Feature flags.
    enable_actions: bool = False  # gates the Tier 3 control tools

    # Item-key / token-key candidates for unpacking paged responses.
    page_items_keys: tuple[str, ...] = ("items", "data", "results", "values")
    page_token_keys: tuple[str, ...] = ("pagingToken", "nextPageToken", "next")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BPConfig":
        """Build config from environment variables. TODO(Phase 1): flesh out."""
        e = env if env is not None else os.environ
        return cls(
            base_url=e.get("BP_API_BASE_URL", ""),
            username=e.get("BP_API_USERNAME", ""),
            password=e.get("BP_API_PASSWORD", ""),
            verify_ssl=e.get("BP_API_VERIFY_SSL", "true").lower() == "true",
            paging_mode=e.get("BP_API_PAGING_MODE", "auto"),
            page_size=int(e.get("BP_API_PAGE_SIZE", "1000")),
            max_pages=int(e.get("BP_API_MAX_PAGES", "1000")),
            enable_actions=e.get("BP_ENABLE_ACTIONS", "false").lower() == "true",
        )

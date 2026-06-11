"""MCP tool layer over BPClient (Phases 4–5).

Tiers (see DESIGN.md):
    Tier 1 Visibility — list_queues/get_queue, list_queue_items, list_sessions,
        get_session_log, list_resources, list_schedules, list_processes
    Tier 2 Insight   — exception_summary, throughput_summary, estate_health
    Tier 3 Control   — retry/defer queue items, start_process, schedule
        control. Registered ONLY when config.enable_actions is True (Phase 5).

Shared contract (common.py): the envelope
`{"items": [...], "meta": {total, returned, truncated, sorted_by}}` with
server-side relevance sort + limit; loud ISO date validation; name→UUID
resolution ("names in, UUIDs underneath"); lru-cached scrub at the PII
boundaries.
"""

from typing import Callable

from ..pii import Scrubber
from .common import DEFAULT_LIMIT, envelope, make_cached_scrub, resolve_id
from .tier1 import build_tier1_tools
from .tier2 import build_tier2_tools

__all__ = [
    "DEFAULT_LIMIT",
    "build_tier1_tools",
    "build_tier2_tools",
    "envelope",
    "make_cached_scrub",
    "register_tools",
    "resolve_id",
]


def register_tools(app, client, scrubber: Scrubber) -> list[str]:
    """Register every available tool on a FastMCP-style app; return their names.

    `app` is anything exposing FastMCP's decorator shape (`app.tool()(fn)`) —
    duck-typed so this layer never imports the server framework. The Phase 6
    server entrypoint calls this once with the live client and the configured
    scrubber; Phase 5 extends it to register Tier 3 behind the
    enable_actions/capability gate.
    """
    tools: list[Callable] = [
        *build_tier1_tools(client, scrubber),
        *build_tier2_tools(client, scrubber),
    ]
    for tool in tools:
        app.tool()(tool)
    return [tool.__name__ for tool in tools]

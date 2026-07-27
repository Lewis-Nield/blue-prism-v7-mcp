"""blue-prism-v7-mcp — distributable MCP server for Blue Prism v7 Enterprise.

Layer map (see DESIGN.md for the full design):
    config.py    — per-deployment configuration (Phase 0/1)
    client.py    — BPClient: the v7 REST client, decoupled from Streamlit (Phase 1-2)
    pii.py       — pluggable Scrubber protocol + Presidio backend (Phase 3)
    tools/       — Tier 1 Visibility, Tier 2 Insight, Tier 3 Control (Phase 4-5)
    engine.py    — Engine: the embeddable domain facade over the reads (Phase 8)
    cache.py     — Cache protocol + thread-safe TTLCache (Phase 8)
    transport.py — rate limiting, retry policy, request counters (Phase 12)
    server.py    — FastMCP stdio server + console entrypoint (Phase 6)
    __main__.py  — `python -m blue_prism_v7_mcp` entrypoint (Phase 7)

Embeddable core (Phase 8): a host can embed the engine in-process and consume
ranked domain records directly, applying its own representation —

    from blue_prism_v7_mcp import Engine, BPClient, BPConfig, build_scrubber
    engine = Engine(BPClient(config), build_scrubber(config))
    ranked = engine.list_queues()          # full records, no truncation
    for queue in ranked.records: ...

and inject a shared, thread-safe cache behind the ``Cache`` protocol for a
long-lived multi-threaded host (``BPClient(config, cache=...)``).

Transport governance (Phase 12) bounds what that client emits at the estate —
a rate limiter, a concurrency ceiling, bounded retry, and a load report —

    client = BPClient(config)              # all off unless config says otherwise
    client.transport_stats()               # what was actually sent

with the limiter injectable (``BPClient(config, limiter=...)``) for a host that
needs one budget shared across processes.
"""

from .cache import Cache, TTLCache
from .client import BPClient
from .config import BPConfig
from .engine import Engine
from .mock import MockBPClient, demo_estate
from .pii import Scrubber, build_scrubber
from .tools.common import Ranked
from .transport import (
    RateLimiter,
    RequestCounters,
    RetryPolicy,
    TokenBucket,
    TransportBudgetExceeded,
)

__version__ = "0.20.0"

__all__ = [
    "BPClient",
    "BPConfig",
    "Cache",
    "Engine",
    "MockBPClient",
    "Ranked",
    "RateLimiter",
    "RequestCounters",
    "RetryPolicy",
    "Scrubber",
    "TTLCache",
    "TokenBucket",
    "TransportBudgetExceeded",
    "__version__",
    "build_scrubber",
    "demo_estate",
]

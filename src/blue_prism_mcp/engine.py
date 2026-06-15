"""Engine — the embeddable core (DESIGN Phase 8 / Custera E1).

A first-class facade over the read surface: one typed method per visibility and
insight tool, returning the *domain* result — the full relevance-sorted records
(a ``Ranked`` for list tools, a dict for single reads), already scrubbed at the
PII boundaries, with no top-N truncation. A host embedding the engine in-process
consumes those records and applies its own representation; the MCP server is one
adapter over the same methods, wrapping each in the LLM-shaped envelope (see
``tools.tier1.build_tier1_tools`` / ``tools.tier2.build_tier2_tools``).

The method logic lives on the per-tier mixins (kept beside their adapters in
``tools/tier1.py`` and ``tools/tier2.py``); ``Engine`` composes them and owns the
shared state they need: the client and the cached, None-safe text scrub.
"""

from __future__ import annotations

from .pii import Scrubber
from .tools.common import make_cached_scrub
from .tools.tier1 import _Tier1ReadsMixin
from .tools.tier2 import _Tier2InsightMixin


class Engine(_Tier1ReadsMixin, _Tier2InsightMixin):
    """Domain facade over a Blue Prism v7 client, decoupled from any presentation.

    Construct with a client (live ``BPClient`` or ``MockBPClient`` — same
    surface) and a ``Scrubber``. Read methods mirror the Tier 1 visibility and
    Tier 2 insight tools and return full domain results; the Tier 3 control
    tools are governed separately (capability-gate + audit + dry-run) and are
    not part of the read facade.
    """

    def __init__(self, client, scrubber: Scrubber) -> None:
        self.client = client
        # One cached scrub shared across every method (an LLM client re-reads
        # the same rows across calls; scrubbing is deterministic).
        self.scrub_text = make_cached_scrub(scrubber)

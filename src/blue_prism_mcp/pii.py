"""Pluggable PII scrubbing.

Decision (2026-06-09): PII scrubbing is OPTIONAL and pluggable. The base install
ships the no-op `NullScrubber`; the Presidio-backed `PresidioScrubber` lives
behind the `[pii]` extra. Wired at TWO boundaries (Phase 3/4): exception
messages and session stage-logs.

Audit rule (inherited): log entity *types* found, never raw content; to files,
never stdout (stdio transport reserves stdout for JSON-RPC).

TODO(Phase 3): implement PresidioScrubber (port utils/pii_scrubber.py); add the
lru-cached per-message wrapper the dashboard server uses.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Scrubber(Protocol):
    """Removes personal data from free-text before it leaves the process."""

    def scrub(self, text: str) -> str:
        ...


class NullScrubber:
    """Default no-op scrubber. Pass text straight through.

    Used when the `[pii]` extra is not installed or a deployment opts out.
    Choosing this is an explicit, auditable deployment decision.
    """

    def scrub(self, text: str) -> str:
        return text

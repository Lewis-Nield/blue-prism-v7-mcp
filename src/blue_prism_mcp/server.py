"""FastMCP server + console entrypoint (Phase 6).

Builds the BPConfig (from env), constructs a BPClient, registers the Tier 1/2
tools (and Tier 3 only when config.enable_actions), then runs the stdio
transport. Reuse the dashboard server's stdout-hygiene pattern: silence noisy
loggers BEFORE and AFTER importing heavy deps so nothing but JSON-RPC reaches
stdout.

TODO(Phase 6): implement. `main()` is the `blue-prism-mcp` console entrypoint.
"""

from __future__ import annotations


def main() -> None:
    """Console entrypoint declared in pyproject [project.scripts]."""
    raise NotImplementedError("Server wiring lands in Phase 6 — see DESIGN.md.")

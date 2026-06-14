"""blue-prism-mcp — distributable MCP server for Blue Prism v7 Enterprise.

Layer map (see DESIGN.md for the full design):
    config.py   — per-deployment configuration (Phase 0/1)
    client.py   — BPClient: the v7 REST client, decoupled from Streamlit (Phase 1-2)
    pii.py      — pluggable Scrubber protocol + Presidio backend (Phase 3)
    tools/      — Tier 1 Visibility, Tier 2 Insight, Tier 3 Control (Phase 4-5)
    server.py   — FastMCP stdio server + console entrypoint (Phase 6)
    __main__.py — `python -m blue_prism_mcp` entrypoint (Phase 7)
"""

__version__ = "0.1.1"

"""MCP tool layer over BPClient (Phase 4-5).

Tiers (see DESIGN.md):
    Tier 1 Visibility — list_queues/get_queue, list_queue_items, list_sessions,
        get_session_log, list_resources, list_schedules, list_processes
    Tier 2 Insight   — exception_summary, throughput_summary, estate_health
    Tier 3 Control   — retry/defer/mark queue items, start_process, schedule
        control. Registered ONLY when config.enable_actions is True.

Carry over from the dashboard server (mcp_server.py): the envelope contract
`{"items": [...], "meta": {total, returned, truncated, sorted_by}}` with
server-side relevance sort + limit; loud ISO date validation; lru-cached scrub.
"""

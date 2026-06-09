# blue-prism-mcp

A distributable **Model Context Protocol (MCP) server for Blue Prism v7
Enterprise**. It gives an LLM agent governed access to a Blue Prism estate over
the supported v7 REST API — work queues, sessions, schedules, resources,
processes — with optional, governance-gated control actions.

No direct database reads. Personal data in exception messages and session logs
is scrubbed at the tool boundary (optional Presidio backend).

> **Status:** Phase 0 — scaffold. See [DESIGN.md](DESIGN.md) for the full design,
> decisions, and architecture.

## Why v7 Enterprise

SS&C is building MCP natively into Blue Prism Next Generation. v7 / Enterprise —
the large installed base — has no native agentic path. This server fills that
gap over the documented, supported REST API.

## Install

```bash
pip install blue-prism-mcp            # light base: MCP runtime + HTTP client
pip install "blue-prism-mcp[pii]"     # + Presidio PII scrubbing
python -m spacy download en_core_web_lg   # if using [pii]
```

## Configuration

Per-deployment, via environment (see `.env.example` once published):

| Variable | Purpose |
|----------|---------|
| `BP_API_BASE_URL` | v7 API base, e.g. `https://<server>/api/v7` |
| `BP_API_USERNAME` / `BP_API_PASSWORD` | API credentials |
| `BP_API_VERIFY_SSL` | TLS verification (default `true`) |
| `BP_API_PAGING_MODE` | `auto` / `token` / `offset` / `none` |
| `BP_ENABLE_ACTIONS` | gate the Tier 3 control tools (default `false`) |

## Tool surface

- **Visibility:** `list_queues`/`get_queue`, `list_queue_items`, `list_sessions`,
  `get_session_log`, `list_resources`, `list_schedules`, `list_processes`
- **Insight:** `exception_summary`, `throughput_summary`, `estate_health`
- **Control** (off by default): `retry_queue_item`, `defer_queue_item`,
  `mark_exception_resolved`, `start_process`, `set_schedule_enabled`,
  `trigger_schedule`

## Licence

Proprietary — internal distribution.

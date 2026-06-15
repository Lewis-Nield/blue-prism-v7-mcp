# blue-prism-mcp

[![CI](https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/8m7nyv54n5-ux/blue-prism-v7-mcp/actions/workflows/ci.yml)

A distributable **Model Context Protocol (MCP) server for Blue Prism v7
Enterprise**. It gives an LLM agent governed access to a Blue Prism estate over
the supported v7 REST API — work queues, sessions, schedules, resources,
processes — with optional, governance-gated control actions.

No direct database reads. Personal data in exception messages and session logs
is scrubbed at the tool boundary (optional Presidio backend).

## Status

Actively developed against the phased plan in [DESIGN.md](DESIGN.md):

- [x] Phase 0 — Scaffold
- [x] Phase 1 — Decouple the client into `BPClient`
- [x] Phase 2 — Extend the client (queue items, processes, session log, Tier 3 writes)
- [x] Phase 3 — Pluggable PII (`Scrubber` protocol; null / regex / Presidio tiers)
- [x] Phase 4 — Tier 1 + 2 tools (the envelope contract)
- [x] Phase 5 — Governance scaffold + Tier 3, shipped disabled
- [x] Phase 6 — Server + packaging
- [x] Phase 7 — Validate (stdio handshake, end-to-end, coverage gate)

## Why v7 Enterprise

SS&C is building MCP natively into Blue Prism Next Generation. v7 / Enterprise —
the large installed base — has no native agentic path. This server fills that
gap over the documented, supported REST API.

Built against the 7.5.1 API specification; supported from **v7.2** (the API
surface is stable from 7.2 through 7.5.1 — the visibility tools degrade
gracefully to 7.1, but the control tools need 7.2).

## Install

```bash
pip install blue-prism-mcp            # light base: MCP runtime + HTTP client
pip install "blue-prism-mcp[pii]"     # + Presidio PII scrubbing
python -m spacy download en_core_web_sm   # if using [pii] (lg/_trf: better recall)
```

## Run

The `blue-prism-mcp` console script speaks the MCP stdio transport — point any
MCP client at it (see [DEPLOYMENT.md](DEPLOYMENT.md) for a Claude Desktop
config example and the full rollout guide). To try the entire tool surface
with no estate and no credentials:

```bash
BP_DATA_SOURCE=mock blue-prism-mcp
```

Live mode fails loud at startup — missing connection settings, an unloadable
PII backend, or a missing audit path refuse to start rather than serve a
degraded surface.

## Configuration

Per-deployment, via environment ([.env.example](.env.example) is the annotated
template; [DEPLOYMENT.md](DEPLOYMENT.md) covers service-account permissions and
day-one verification):

| Variable | Purpose |
|----------|---------|
| `BP_API_BASE_URL` | v7 API base, e.g. `https://<server>/api/v7` |
| `BP_AUTH_URL` | Blue Prism Authentication Server, e.g. `https://<auth-server>` |
| `BP_CLIENT_ID` / `BP_CLIENT_SECRET` | OAuth2 client-credentials (service account) |
| `BP_DATA_SOURCE` | `live` (default) / `mock` — in-memory fixtures, no estate needed |
| `BP_API_VERIFY_SSL` | TLS verification (default `true`) |
| `BP_API_PAGING_MODE` | `token` (v7 default) / `offset` / `none` / `auto` |
| `BP_ENABLE_ACTIONS` | gate the Tier 3 control tools (default `false`) |
| `BP_AUDIT_LOG_PATH` | JSON-lines audit file for the action surface — REQUIRED when actions are enabled (fails loud without it) |
| `BP_PII_BACKEND` | `null` (default) / `regex` (zero-dep UK FS patterns) / `presidio` (needs `[pii]`) — fails loud at startup if the requested backend can't load |
| `BP_PII_CUSTOM_PATTERNS` | JSON array of `{"name", "pattern"}` domain identifiers; they beat the built-ins on overlap |
| `BP_PII_SPACY_MODEL` | spaCy model for `presidio` (default `en_core_web_sm`) |

## Tool surface

- **Visibility:** `list_queues`/`get_queue`, `list_queue_items`/`get_queue_item`,
  `list_item_attempts`, `list_sessions`/`get_session`, `get_session_log`,
  `list_resources`, `list_schedules`, `list_processes`
- **Insight:** `exception_summary`, `throughput_summary`, `estate_health`
- **Control** (off by default): `retry_queue_item`, `defer_queue_item`,
  `start_process`, `stop_session`, `set_schedule_enabled`, `trigger_schedule`,
  `stop_schedule`

## Licence

Proprietary — internal distribution.

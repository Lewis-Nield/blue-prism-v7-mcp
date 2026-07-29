# blue-prism-v7-mcp

[![CI](https://github.com/Lewis-Nield/blue-prism-v7-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Lewis-Nield/blue-prism-v7-mcp/actions/workflows/ci.yml)

A distributable **Model Context Protocol (MCP) server for Blue Prism v7
Enterprise**. It gives an LLM agent governed access to a Blue Prism estate over
the supported v7 REST API — work queues, sessions, schedules, resources,
processes — with optional, governance-gated control actions.

No direct database reads. Personal data in exception messages and session logs
is scrubbed at the tool boundary (optional Presidio backend).

> **Independent project.** This is an unofficial, community-built server. It is
> not affiliated with, endorsed by, or sponsored by SS&C Blue Prism. "Blue
> Prism" is a trademark of SS&C Technologies Holdings, Inc., used here only to
> describe what this software interoperates with.

## Status

**Current release: v0.20.1** — see
[CHANGELOG.md](https://github.com/Lewis-Nield/blue-prism-v7-mcp/blob/main/CHANGELOG.md)
for the full release history. The **v0.1.0 foundation** was built in eight
phases against the plan in
[DESIGN.md](https://github.com/Lewis-Nield/blue-prism-v7-mcp/blob/main/DESIGN.md):

- [x] Phase 0 — Scaffold
- [x] Phase 1 — Decouple the client into `BPClient`
- [x] Phase 2 — Extend the client (queue items, processes, session log, Tier 3 writes)
- [x] Phase 3 — Pluggable PII (`Scrubber` protocol; null / regex / Presidio tiers)
- [x] Phase 4 — Tier 1 + 2 tools (the envelope contract)
- [x] Phase 5 — Governance scaffold + Tier 3, shipped disabled
- [x] Phase 6 — Server + packaging
- [x] Phase 7 — Validate (stdio handshake, end-to-end, coverage gate)

Development continues as themed minor releases on top of that foundation — see
[CHANGELOG.md](https://github.com/Lewis-Nield/blue-prism-v7-mcp/blob/main/CHANGELOG.md)
for the release history.

## Verification status

**This has not yet been run against a live Blue Prism estate.** It is built
against the official v7 OpenAPI specifications (7.5.1, cross-checked on 7.2.0
and 7.1.0) and exercised end-to-end against in-memory clients under a 100%
coverage gate — but specification-built is not estate-verified, and the two
should not be confused. That is why it is 0.x: v1.0.0 ships when the checklist
below is cleared, not before.

What that means by tier:

- **Reads** — a wrong assumption surfaces as an error or an unexpectedly wide
  result, not as a change to your estate. The specific risk worth knowing: the
  v7 API *ignores* an unrecognised query parameter rather than rejecting it, so
  a wrongly encoded filter returns unfiltered rows and looks correct. The
  encodings are pinned against the spec; they are not yet confirmed on the wire.
- **Control actions** — these write to a real estate. They ship **disabled**
  (`BP_ENABLE_ACTIONS` defaults to `false`), behind a capability gate, an
  append-only audit, and `dry_run=true` by default. Three of the endpoint
  behaviours they depend on are underdocumented in the specification itself and
  are therefore inferred rather than known.

If you are trying this out: **point it at a development or test estate first**,
leave the action surface off until the reads look right, and treat the first
`dry_run=false` as a deliberate step rather than a default.

[docs/VERIFICATION.md](https://github.com/Lewis-Nield/blue-prism-v7-mcp/blob/main/docs/VERIFICATION.md)
is the fill-in report that closes this out — every inferred behaviour, with the
exact call that settles it. If you have an estate and the inclination, a
completed report (even Part A, which touches nothing) is the single most useful
contribution this project can receive.

## Why v7 Enterprise

SS&C is building MCP natively into Blue Prism Next Generation. v7 / Enterprise —
the large installed base — has no native agentic path. This server fills that
gap over the documented, supported REST API.

Built against the 7.5.1 API specification; supported from **v7.2** (the API
surface is stable from 7.2 through 7.5.1 — the visibility tools degrade
gracefully to 7.1, but the control tools need 7.2).

## Install

```bash
pip install blue-prism-v7-mcp            # light base: MCP runtime + HTTP client
pip install "blue-prism-v7-mcp[pii]"     # + Presidio PII scrubbing
python -m spacy download en_core_web_sm   # if using [pii] (lg/_trf: better recall)
```

## Run

The `blue-prism-v7-mcp` console script speaks the MCP stdio transport — point any
MCP client at it (see
[DEPLOYMENT.md](https://github.com/Lewis-Nield/blue-prism-v7-mcp/blob/main/DEPLOYMENT.md)
for a Claude Desktop config example and the full rollout guide). To try the
entire tool surface with no estate and no credentials:

```bash
BP_DATA_SOURCE=mock blue-prism-v7-mcp   # lean fixtures — what the unit tests assert against
BP_DATA_SOURCE=demo blue-prism-v7-mcp   # a populated estate — best for a demo or walkthrough
```

`demo` runs the same offline client seeded with a larger, lived-in estate:
worker pools across departments, queues in varied health (an SLA-breaching
backlog, a stalled one, a paused one, a healthy flowing one), an in-flight and
a silently-stale session, a failed schedule, and months of session history —
enough shape to point `throughput_summary` or `estate_health` at and get a real
answer back. `mock` stays deliberately lean; use it if you're integrating
against the tool surface rather than looking at it.

Live mode fails loud at startup — missing connection settings, an unloadable
PII backend, or a missing audit path refuse to start rather than serve a
degraded surface.

## Configuration

Per-deployment, via environment
([.env.example](https://github.com/Lewis-Nield/blue-prism-v7-mcp/blob/main/.env.example)
is the annotated template;
[DEPLOYMENT.md](https://github.com/Lewis-Nield/blue-prism-v7-mcp/blob/main/DEPLOYMENT.md)
covers service-account permissions and day-one verification):

| Variable | Purpose |
|----------|---------|
| `BP_API_BASE_URL` | v7 API base, e.g. `https://<server>/api/v7` |
| `BP_AUTH_URL` | Blue Prism Authentication Server, e.g. `https://<auth-server>` |
| `BP_CLIENT_ID` / `BP_CLIENT_SECRET` | OAuth2 client-credentials (service account) |
| `BP_DATA_SOURCE` | `live` (default) / `mock` — lean in-memory fixtures / `demo` — a larger populated estate, no estate needed |
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
  `list_resources`, `list_schedules`/`get_schedule`, `list_schedule_tasks`,
  `list_schedule_logs`, `list_processes`
- **Context & topology:** `list_queue_configurations`, `list_resource_pools`,
  `list_environment_variables`, `list_process_groups`
- **Insight:** `exception_summary`, `estate_exception_summary`,
  `throughput_summary`, `estate_health`, `license_entitlement`
- **Control** (off by default): `retry_queue_item`, `defer_queue_item`,
  `start_process`, `stop_session`, `set_schedule_enabled`, `trigger_schedule`,
  `stop_schedule`

## Embed in-process

The same logic that backs the MCP tools is exposed as a plain `Engine` facade,
so a host can embed it without the stdio transport. Each read method returns the
full relevance-sorted records — already scrubbed, with name resolution and loud
validation — leaving the representation (paging, shaping) to the host:

```python
from blue_prism_v7_mcp import Engine, BPClient, BPConfig, build_scrubber

config = BPConfig(...)            # or BPConfig.from_env()
engine = Engine(BPClient(config), build_scrubber(config))

ranked = engine.list_queues()     # Ranked: full records, no truncation
for queue in ranked.records:
    ...                           # apply your own representation
```

For a long-lived, multi-threaded host sharing one client across workers, inject
a shared store behind the `Cache` protocol (the default `TTLCache` is itself
thread-safe): `BPClient(config, cache=my_cache)`. The MCP server's envelope view
is just one adapter over the same engine (`tools.common.to_envelope`).

## Licence

[Apache-2.0](https://github.com/Lewis-Nield/blue-prism-v7-mcp/blob/main/LICENSE).

The licence grants no trademark rights (Apache-2.0 §6). "Blue Prism" is a
trademark of SS&C Technologies Holdings, Inc.; this project is independent of
and unaffiliated with SS&C Blue Prism, and uses the name only to describe the
product it interoperates with.

# Security Policy

This document states the security model the server is built to and how to
report a hole in it. Operational detail — service-account permissions, the
environment contract, the day-one verification checklist — lives in
[DEPLOYMENT.md](DEPLOYMENT.md).

## Reporting a vulnerability

Report vulnerabilities privately via GitHub: **Security → Report a
vulnerability** on this repository (GitHub private vulnerability reporting).
Please do not open a public issue for anything you believe is exploitable.

Include what you can of: the affected tool or module, a reproduction (the
`mock`/`demo` data sources need no estate and no credentials), and the impact
you see. You should hear back within a week.

## Supported versions

Pre-1.0, only the **latest minor release** receives security fixes. There are
no backports; fixes ship as a new release on top of the current line.

## Security model

### What the server touches

- **The supported v7 REST API only.** No database reads, no file-system access
  to the estate, no undocumented endpoints.
- **The server has no permission model of its own to misconfigure.** It
  inherits exactly the Blue Prism permissions of the service account it runs
  as; scoping what the model can see and do is done in Blue Prism, on a
  dedicated least-privileged account.
- **Credentials come from the environment only** (OAuth2 client-credentials
  against the Blue Prism Authentication Server). The client secret is used
  solely for the token request; it is excluded from the config's `repr` and is
  never written to logs or the audit trail.
- **TLS verification is on by default.** Disabling it (`BP_API_VERIFY_SSL`) is
  an explicit per-deployment decision.

### What leaves the process

- **PII scrubbing at the tool boundary.** Exception messages, session logs,
  and other free-text estate fields are scrubbed before text reaches the
  model. The backend is explicit config (`null` / `regex` / `presidio`) —
  choosing no scrubbing is a deliberate, auditable decision, never a fallback.
- **Fail loud, never degrade.** If the configured PII backend cannot load, the
  server refuses to start rather than serve unredacted text. The same posture
  applies to missing connection settings and a missing or unwritable audit
  path.
- **The audit trail carries no message content.** Audit lines record tool
  names, ids, names, dates, statuses, and PII entity *types* — never payloads
  or exception text (exception messages can echo response content, which is
  itself a scrub target).
- **stdout is JSON-RPC only.** The stdio transport owns stdout; all logging
  goes to stderr, and the audit trail goes to its own file.

### The action surface

Control tools (Tier 3) are **disabled by default** and sit behind three layers
that cannot be silently relaxed:

1. **Capability gating** — at startup the server resolves the service
   account's actual permissions and registers only the action tools the
   account can execute. A tool the account cannot run does not exist as far as
   the model is concerned.
2. **Audit before write** — enabling actions *requires* an audit log path
   (startup fails without one), and the attempt line is appended before the
   write is issued, so no estate mutation can outrun its audit record.
3. **Dry-run by default** — every action tool defaults to `dry_run=true`,
   returning the exact write it would issue without sending it. A mutation
   requires an explicit `dry_run=false` from the model.

### Isolation

- The read cache is per server instance and never shared between processes;
  one MCP client can point at two estates via two server entries without
  cross-talk.
- For in-process embedding, per-call actor identity is bound by the host
  (`bind_actor`), never exposed as a tool parameter — the model cannot spoof
  who invoked an action.

## Deployer responsibilities

The server governs what crosses its own boundary; the deployment owns the rest:

- Run it as a **dedicated service account** with the least Blue Prism
  permissions that cover the tools you want exposed (tables in
  [DEPLOYMENT.md](DEPLOYMENT.md)) — never an interactive user's credentials.
- Protect the environment (or `.env` file) holding the client secret, and the
  audit log file; the server only appends — rotation and retention are yours.
- Work the **day-one verification checklist** in
  [DEPLOYMENT.md](DEPLOYMENT.md) before allowing `dry_run=false` on a new
  estate.

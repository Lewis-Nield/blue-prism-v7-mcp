"""Governance for the Tier 3 control surface (Phase 5).

Two pieces gate every action tool:

1. **Capability resolution.** `GET /user/permissions` answers the service
   account's Blue Prism permission names (a flat string array — verified
   against the 7.5.1 spec). Each action tool maps to the permissions its
   endpoint documents; tools the account cannot execute are never registered,
   so the model cannot even attempt them. The spec carries the permission
   names only as prose in endpoint descriptions (no enum), so matching is
   case-insensitive against the documented display names — and the exact
   strings the live endpoint returns are a day-one verification item.

2. **Audit logging.** Every action invocation — dry-run, attempt, success,
   error — appends one JSON line to a file. The attempt line is written
   BEFORE the write is issued, so no estate mutation can ever outrun its
   audit record. Audit goes to a file and never stdout (stdio transport:
   stdout is JSON-RPC only), and must never receive message content that
   has not been scrubbed — record entity types, ids, names, and dates,
   never payload or exception text. Error lines therefore carry
   `audit_detail(exc)` — the exception class and, for HTTP errors, the
   status code — because exception *messages* can echo request/response
   content, and estate exception text is a scrub target.

Both fail loud: actions enabled without an audit path, an unwritable audit
file, or a failed permissions call refuse to start rather than running an
action surface ungoverned — the same posture as the PII backend selection.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# The documented permission names per endpoint (7.5.1 spec, endpoint
# descriptions). A tool's requirement is a tuple of clauses; every clause must
# be satisfied by holding at least one of its alternatives.
_QUEUE_FULL_ACCESS = "Full Access to Queue Management"

# PATCH /sessions/{id} documents the same permissions whether the requested
# status is Running (start_process) or Stopped (stop_session): Control Resource
# plus any one of the process permissions. Defined once so the control pair
# cannot drift apart.
_SESSION_CONTROL = (
    ("Create Process", "Edit Process", "Execute Process"),
    ("Control Resource",),
)

# POST /schedules/{id}/runs (trigger) and DELETE /schedules/{id}/runs/active
# (stop) both document `Edit Schedule` only — defined once so the run-control
# pair (start a run / stop the active runs) cannot drift apart.
_SCHEDULE_RUN_CONTROL = (("Edit Schedule",),)

TOOL_PERMISSIONS: dict[str, tuple[tuple[str, ...], ...]] = {
    "retry_queue_item": ((_QUEUE_FULL_ACCESS,),),
    "defer_queue_item": ((_QUEUE_FULL_ACCESS,),),
    "start_process": _SESSION_CONTROL,
    "stop_session": _SESSION_CONTROL,
    # PUT /schedules/{id}: retiring needs Edit + Retire; UNretiring needs
    # Create Schedule on top. Registration requires the retire pair so the
    # tool isn't hidden from accounts that can retire but not create; the
    # unretire extra is enforced at call time (see tier3).
    "set_schedule_enabled": (("Edit Schedule",), ("Retire Schedule",)),
    "trigger_schedule": _SCHEDULE_RUN_CONTROL,
    "stop_schedule": _SCHEDULE_RUN_CONTROL,
}

UNRETIRE_EXTRA_PERMISSION = "Create Schedule"


def holds(permissions: Iterable[str], required: str) -> bool:
    """True when *required* is among *permissions* (case-insensitive)."""
    wanted = required.casefold()
    return any(str(p).strip().casefold() == wanted for p in permissions)


def unsatisfied_clauses(tool: str, permissions: Iterable[str]) -> list[str]:
    """The permission clauses *tool* needs that *permissions* do not satisfy.

    Each entry renders one failed clause ("Create Process | Edit Process |
    Execute Process") for fail-loud messages and the startup audit record.
    Empty means the account can execute the tool.
    """
    held = {str(p).strip().casefold() for p in permissions}
    return [
        " | ".join(clause)
        for clause in TOOL_PERMISSIONS[tool]
        if not any(alt.casefold() in held for alt in clause)
    ]


def resolve_capabilities(permissions: Iterable[str]) -> set[str]:
    """The action tools the account's *permissions* allow it to execute."""
    held = list(permissions)
    return {tool for tool in TOOL_PERMISSIONS if not unsatisfied_clauses(tool, held)}


class AuditLog:
    """Append-only JSON-lines audit of the action surface.

    One line per event: a UTC timestamp, the tool, its arguments (ids, names,
    dates — never message content), the event status (startup / dry_run /
    attempt / success / error), and an optional detail. The file is opened
    per write so a long-running server never holds a stale handle; the
    constructor touches the file so an unwritable path fails at startup,
    not on the first action.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch()

    def record(
        self,
        tool: str,
        args: dict[str, Any],
        status: str,
        detail: str | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool,
            "status": status,
            "args": args,
        }
        if detail is not None:
            entry["detail"] = detail
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")


def audit_detail(exc: BaseException) -> str:
    """A safe audit `detail` for *exc*: the class name, never the message.

    Exception messages can echo request/response content (estate exception
    text is a scrub target), so the audit records only the exception class
    plus, when the exception carries an HTTP response (requests.HTTPError
    shape, probed duck-typed), the status code.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return f"{type(exc).__name__} (HTTP {status})"
    return type(exc).__name__


def build_audit_log(config) -> AuditLog:
    """Construct the audit log from config, failing loud when unconfigured.

    An enabled action surface without an audit trail is the governance
    equivalent of silently-degraded redaction, so there is no default path
    and no opt-out: enable_actions requires audit_log_path.
    """
    if not str(config.audit_log_path).strip():
        raise ValueError(
            "enable_actions requires an audit log: set BP_AUDIT_LOG_PATH "
            "(audit_log_path) to a writable file path. Every Tier 3 action "
            "is recorded there as JSON lines."
        )
    return AuditLog(config.audit_log_path)

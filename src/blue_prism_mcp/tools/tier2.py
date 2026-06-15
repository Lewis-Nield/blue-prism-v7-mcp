"""Tier 2 — insight tools: derived views, each its own tool.

Separate single-purpose tools rather than parameters on the primitives — tight
descriptions drive better model tool-selection. No v7 endpoint aggregates
exceptions or throughput (verified — see DESIGN.md's /dashboards verdict), so
exception_summary and throughput_summary aggregate client-side over the Tier 1
reads, under the same required-window scoping. estate_health and
license_entitlement read the two licence /dashboards endpoints this server
consumes: current limits vs usage, and the per-tier entitlement ceilings.
"""

from __future__ import annotations

from collections import Counter
from typing import Callable

from ..pii import Scrubber
from .common import (
    DEFAULT_LIMIT,
    envelope,
    make_cached_scrub,
    read_or_unavailable,
    require_window,
    resolve_id,
    resource_urgency,
)

# Resource display statuses that demand operator attention, i.e. the worker is
# down or degraded rather than merely busy/idle.
_ATTENTION_STATUSES = frozenset({"Missing", "Offline", "Warning"})

# The BaseEntitlement keys (all lowercase in the API) mapped to readable names.
_ENTITLEMENT_FIELDS = {
    "publishedprocesseslimit": "published_processes_limit",
    "concurrentsessionslimit": "concurrent_sessions_limit",
    "runtimeresourceslimit": "runtime_resources_limit",
    "processalertmachineslimit": "process_alert_machines_limit",
}


def _entitlement_tier(tier: dict | None) -> dict:
    """Reshape a BaseEntitlement into readable snake_case keys (values untouched)."""
    tier = tier or {}
    return {friendly: tier.get(raw) for raw, friendly in _ENTITLEMENT_FIELDS.items()}


def build_tier2_tools(client, scrubber: Scrubber) -> list[Callable]:
    """Build the four insight tools over *client*, scrubbing with *scrubber*."""
    scrub_text = make_cached_scrub(scrubber)

    def exception_summary(
        queue: str, start_date: str, end_date: str, limit: int = DEFAULT_LIMIT
    ) -> dict:
        """Summarise one queue's exceptions: counts grouped by exception reason.

        `queue` is a queue name (case-insensitive) or id; `start_date`/
        `end_date` (ISO, REQUIRED) bound the items' last-updated time. Each
        item gives a distinct exception reason (personal data already
        removed), how many items hit it, when it first and last occurred in
        the window, and the resources involved. Use it to find the dominant
        failure mode before drilling into list_queue_items.

        Reasons are grouped AFTER scrubbing, so messages that differ only in
        personal data (names, references) fold into one bucket — counts
        reflect failure modes, not distinct customers.

        Results come back as {"items": [...], "meta": {...}}, most frequent
        first, capped at `limit` (default 50).
        """
        require_window(start_date, end_date)
        queue_id = resolve_id(queue, client.get_queues(), entity="queue")
        items = client.get_queue_items(
            queue_id, state="Exceptioned", start_date=start_date, end_date=end_date
        )

        groups: dict[str, dict] = {}
        for item in items:
            reason = scrub_text(item.get("exceptionReason")) or "(no reason recorded)"
            when = item.get("exceptionedDate") or item.get("lastUpdated") or ""
            group = groups.setdefault(
                reason,
                {
                    "reason": reason,
                    "count": 0,
                    "first_seen": when,
                    "last_seen": when,
                    "resources": set(),
                },
            )
            group["count"] += 1
            if when:
                group["first_seen"] = min(group["first_seen"] or when, when)
                group["last_seen"] = max(group["last_seen"], when)
            if item.get("resource"):
                group["resources"].add(item["resource"])

        rows = [{**g, "resources": sorted(g["resources"])} for g in groups.values()]
        return envelope(
            rows,
            sort_key=lambda g: g.get("count", 0),
            sorted_by="count desc",
            limit=limit,
            reverse=True,
        )

    def throughput_summary(
        start_date: str,
        end_date: str,
        process: str | None = None,
        limit: int = DEFAULT_LIMIT,
    ) -> dict:
        """Summarise session outcomes per process over a date window.

        `start_date`/`end_date` (ISO) are REQUIRED. Each item gives, for one
        process: total sessions, counts by outcome (completed, terminated,
        stopped, other), the completion rate as a percentage of finished runs,
        and the terminated runs split by cause (process errors vs internal
        errors). Use it to see which processes are busiest and which are
        failing. Optionally scope to one `process` name (case-insensitive).

        Results come back as {"items": [...], "meta": {...}}, busiest first,
        capped at `limit` (default 50).
        """
        require_window(start_date, end_date)
        sessions = client.get_sessions(start_date, end_date)
        if process:
            wanted = process.strip().casefold()
            sessions = [s for s in sessions if str(s.get("processName", "")).casefold() == wanted]

        by_process: dict[str, list[dict]] = {}
        for session in sessions:
            by_process.setdefault(str(session.get("processName") or "(unknown)"), []).append(
                session
            )

        rows = []
        for name, runs in by_process.items():
            statuses = Counter(str(s.get("status")) for s in runs)
            completed = statuses.get("Completed", 0)
            terminated = statuses.get("Terminated", 0)
            stopped = statuses.get("Stopped", 0)
            finished = completed + terminated + stopped
            reasons = Counter(
                str(s.get("terminationReason")) for s in runs if s.get("status") == "Terminated"
            )
            rows.append(
                {
                    "process": name,
                    "total_sessions": len(runs),
                    "completed": completed,
                    "terminated": terminated,
                    "stopped": stopped,
                    "other": len(runs) - finished,
                    "completion_rate_pct": (
                        round(completed / finished * 100, 1) if finished else None
                    ),
                    "terminated_process_errors": reasons.get("ProcessError", 0),
                    "terminated_internal_errors": reasons.get("InternalError", 0),
                }
            )
        return envelope(
            rows,
            sort_key=lambda r: r.get("total_sessions", 0),
            sorted_by="total_sessions desc",
            limit=limit,
            reverse=True,
        )

    def estate_health(limit: int = DEFAULT_LIMIT) -> dict:
        """Roll up estate health: digital worker status plus licence headroom.

        `workers_by_status` counts every digital worker by display status;
        `workers_requiring_attention` details the ones that are Missing,
        Offline, or in Warning (most urgent first, capped at `limit`, with
        `attention_meta` reporting the full total). `license_usage` gives the
        licence limits versus current usage — concurrent sessions, runtime
        resources, published processes (a null limit means unlimited) — so you
        can spot an estate about to hit its ceiling. If the licence read is
        denied or fails, license_usage carries an `unavailable` note instead
        of failing the whole health check.
        """
        resources = client.get_resources()
        attention = [r for r in resources if (r.get("displayStatus") or "") in _ATTENTION_STATUSES]
        capped = envelope(
            attention,
            sort_key=resource_urgency,
            sorted_by="displayStatus urgency (Missing/Offline/Warning), name",
            limit=limit,
        )
        # A service account without dashboard permission — or a timeout /
        # connection drop on this one extra read — shouldn't lose worker health
        # too; the licence block degrades visibly instead (see read_or_unavailable).
        license_usage = read_or_unavailable(client.get_current_limits_and_usage, "licence read")
        return {
            "workers_total": len(resources),
            "workers_by_status": dict(
                Counter(str(r.get("displayStatus") or "(unknown)") for r in resources)
            ),
            "workers_requiring_attention": capped["items"],
            "attention_meta": capped["meta"],
            "license_usage": license_usage,
        }

    def license_entitlement() -> dict:
        """Report what the estate is licensed for: entitlement ceilings by tier.

        Complements estate_health's license_usage (limits vs current usage)
        with the entitlement side: `active_license_types` lists the licence
        types in force, and `enterprise`/`desktop` give each tier's ceilings —
        published processes, concurrent sessions, runtime resources, and
        process-alert machines. Use it to see the licensed capacity behind the
        usage figures (e.g. enterprise vs desktop runtime-resource headroom).

        Reading entitlement needs the "System - License" permission; if the
        read is denied or fails, the result carries an `unavailable` note
        instead of erroring.
        """
        raw = read_or_unavailable(client.get_license_entitlement, "licence entitlement read")
        if "unavailable" in raw:
            return raw
        return {
            "active_license_types": raw.get("activeLicenseTypes") or [],
            "enterprise": _entitlement_tier(raw.get("enterpriseEntitlement")),
            "desktop": _entitlement_tier(raw.get("desktopEntitlement")),
        }

    return [exception_summary, throughput_summary, estate_health, license_entitlement]

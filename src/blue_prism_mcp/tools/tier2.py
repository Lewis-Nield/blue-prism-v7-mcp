"""Tier 2 — insight tools: derived views, each its own tool.

Separate single-purpose tools rather than parameters on the primitives — tight
descriptions drive better model tool-selection. No v7 endpoint aggregates
exceptions or throughput (verified — see DESIGN.md's /dashboards verdict), so
exception_summary (one queue), its estate-wide sibling estate_exception_summary
(every queue in one call), and throughput_summary aggregate client-side over the
Tier 1 reads, under the same required-window scoping. estate_health and
license_entitlement read the two licence /dashboards endpoints this server
consumes: current limits vs usage, and the per-tier entitlement ceilings.
resource_utilization aggregates the third — the resourceUtilization heat-map —
into per-worker minutes-worked-vs-wall-clock, mechanically (no thresholds or
severity; that stays a consumer's L2 call).

As with Tier 1, the domain logic lives on ``_Tier2InsightMixin`` (composed into
``Engine``) returning the full ranked records, and ``build_tier2_tools`` is the
thin MCP adapter applying the envelope representation.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Callable

from .common import (
    DEFAULT_LIMIT,
    Ranked,
    rank,
    read_or_unavailable,
    require_window,
    resolve_id,
    resource_urgency,
    to_envelope,
)

# Resource display statuses that demand operator attention, i.e. the worker is
# down or degraded rather than merely busy/idle.
_ATTENTION_STATUSES = frozenset({"Missing", "Offline", "Warning"})

# Wall-clock minutes in a day — resource_utilization's honest, opinion-free
# denominator (see DESIGN.md): an offline day counts as 0% worked against the
# full day, not excluded from the window.
_MINUTES_PER_DAY = 24 * 60

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


def _fold_exceptions(groups: dict, items: list, scrub_text, queue_name: str | None = None) -> None:
    """Fold exceptioned items into ``groups`` keyed by their scrubbed reason.

    Shared by the single-queue and estate-wide summaries: grouping AFTER
    scrubbing folds messages that differ only in personal data into one bucket,
    so counts reflect failure modes, not distinct customers. Each group tracks
    its count, first/last occurrence, and the resources involved; the estate-wide
    caller passes ``queue_name`` so a group also records which queues exhibit the
    reason (the single-queue caller omits it — see _exception_rows).
    """
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
                "queues": set(),
            },
        )
        group["count"] += 1
        if when:
            group["first_seen"] = min(group["first_seen"] or when, when)
            group["last_seen"] = max(group["last_seen"], when)
        if item.get("resource"):
            group["resources"].add(item["resource"])
        if queue_name:
            group["queues"].add(queue_name)


def _exception_rows(groups: dict) -> list[dict]:
    """Materialise grouped exceptions into rows: sets → sorted lists.

    ``queues`` is included only when populated (the estate-wide summary), so the
    single-queue summary's rows keep their original shape (reason, count,
    first_seen, last_seen, resources).
    """
    rows = []
    for group in groups.values():
        row = {k: v for k, v in group.items() if k != "queues"}
        row["resources"] = sorted(group["resources"])
        if group["queues"]:
            row["queues"] = sorted(group["queues"])
        rows.append(row)
    return rows


class _Tier2InsightMixin:
    """Domain methods for the insight tools (mixed into ``Engine``).

    Expects ``self.client`` and ``self.scrub_text`` from the composing class.
    The summaries return ``Ranked``; ``license_entitlement`` returns a dict; and
    ``estate_health`` returns a composite dict whose ``workers_requiring_attention``
    is a ``Ranked`` (the one field the MCP adapter caps).
    """

    def exception_summary(self, queue: str, start_date: str, end_date: str) -> Ranked:
        """Group one queue's exceptions by (scrubbed) reason, ranked by count."""
        require_window(start_date, end_date)
        queue_id = resolve_id(queue, self.client.get_queues(), entity="queue")
        items = self.client.get_queue_items(
            queue_id, state="Exceptioned", start_date=start_date, end_date=end_date
        )
        groups: dict[str, dict] = {}
        _fold_exceptions(groups, items, self.scrub_text)
        return rank(
            _exception_rows(groups),
            sort_key=lambda g: g.get("count", 0),
            sorted_by="count desc",
            reverse=True,
        )

    def estate_exception_summary(self, start_date: str, end_date: str) -> Ranked:
        """Group exceptions across EVERY queue by (scrubbed) reason, ranked by count.

        The estate-wide form of exception_summary: no v7 endpoint aggregates
        exceptions, so it loops every queue's exceptioned items over the window
        and folds them into one ranking — the dominant failure mode across the
        estate in a single call rather than a loop over each queue. Each group
        additionally records which ``queues`` exhibit the reason. The window is
        required (the per-queue item reads refuse to run unbounded).
        """
        require_window(start_date, end_date)
        groups: dict[str, dict] = {}
        for queue in self.client.get_queues():
            queue_id = queue.get("id")
            if not queue_id:
                continue
            items = self.client.get_queue_items(
                queue_id, state="Exceptioned", start_date=start_date, end_date=end_date
            )
            _fold_exceptions(groups, items, self.scrub_text, queue_name=queue.get("name"))
        return rank(
            _exception_rows(groups),
            sort_key=lambda g: g.get("count", 0),
            sorted_by="count desc",
            reverse=True,
        )

    def throughput_summary(
        self, start_date: str, end_date: str, process: str | None = None
    ) -> Ranked:
        """Aggregate session outcomes per process over a window, ranked by volume."""
        require_window(start_date, end_date)
        sessions = self.client.get_sessions(start_date, end_date)
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
        return rank(
            rows,
            sort_key=lambda r: r.get("total_sessions", 0),
            sorted_by="total_sessions desc",
            reverse=True,
        )

    def estate_health(self) -> dict:
        """Roll up worker status plus licence headroom (attention list as Ranked)."""
        resources = self.client.get_resources()
        attention = [r for r in resources if (r.get("displayStatus") or "") in _ATTENTION_STATUSES]
        # A service account without dashboard permission — or a timeout /
        # connection drop on this one extra read — shouldn't lose worker health
        # too; the licence block degrades visibly instead (see read_or_unavailable).
        license_usage = read_or_unavailable(
            self.client.get_current_limits_and_usage, "licence read"
        )
        return {
            "workers_total": len(resources),
            "workers_by_status": dict(
                Counter(str(r.get("displayStatus") or "(unknown)") for r in resources)
            ),
            "workers_requiring_attention": rank(
                attention,
                sort_key=resource_urgency,
                sorted_by="displayStatus urgency (Missing/Offline/Warning), name",
            ),
            "license_usage": license_usage,
        }

    def license_entitlement(self) -> dict:
        """Report the estate's entitlement ceilings by tier (or an unavailable note)."""
        raw = read_or_unavailable(self.client.get_license_entitlement, "licence entitlement read")
        if "unavailable" in raw:
            return raw
        return {
            "active_license_types": raw.get("activeLicenseTypes") or [],
            "enterprise": _entitlement_tier(raw.get("enterpriseEntitlement")),
            "desktop": _entitlement_tier(raw.get("desktopEntitlement")),
        }

    def resource_utilization(self, start_date: str, end_date: str) -> dict:
        """Per-worker minutes worked vs wall-clock time, per day and over the window.

        The mechanical L1 aggregation over the resourceUtilization heat-map:
        no thresholds, no "saturated" opinion (that's a consumer's L2 call —
        see DESIGN.md). The client only takes a start bound, so rows are
        filtered down to `end_date` here. Each worker's `daily` rows keep the
        raw per-day worked-minutes and wall-clock-minutes alongside the
        percentage, so no consumer is locked into this denominator; the
        windowed roll-up sums those same two figures across the window rather
        than averaging daily percentages (which would misweight short days).
        The estate roll-up is total-worked ÷ total-wall-clock across every
        worker that reported any data — a true duty cycle, not a mean of
        per-worker percentages (which would diverge when workers' data spans
        differ).
        """
        require_window(start_date, end_date)
        start = datetime.fromisoformat(start_date).date()
        end = datetime.fromisoformat(end_date).date()
        raw = read_or_unavailable(
            lambda: self.client.get_resource_utilization(start_date),
            "resource utilization read",
        )
        if "unavailable" in raw:
            return {
                "workers": rank([], sort_key=lambda r: 0, sorted_by="window_utilization_pct desc"),
                "estate_worked_minutes": None,
                "estate_wall_clock_minutes": None,
                "estate_utilization_pct": None,
                **raw,
            }

        window_wall_clock = (end - start).days + 1
        window_wall_clock *= _MINUTES_PER_DAY

        by_worker: dict[str, dict] = {}
        for row in raw:
            try:
                row_date = datetime.fromisoformat(str(row.get("utilizationDate"))).date()
            except (ValueError, TypeError):
                continue
            if row_date < start or row_date > end:
                continue
            usages = row.get("usages") or []
            minutes = sum(usages)
            worker = by_worker.setdefault(
                row.get("resourceId"),
                {
                    "resource_id": row.get("resourceId"),
                    "worker": row.get("digitalWorkerName"),
                    "daily": [],
                    "window_worked_minutes": 0,
                },
            )
            worker["daily"].append(
                {
                    "date": row_date.isoformat(),
                    "worked_minutes": minutes,
                    "wall_clock_minutes": _MINUTES_PER_DAY,
                    "utilization_pct": round(minutes / _MINUTES_PER_DAY * 100, 1),
                }
            )
            worker["window_worked_minutes"] += minutes

        total_worked = 0
        for worker in by_worker.values():
            worker["daily"].sort(key=lambda d: d["date"])
            worker["window_wall_clock_minutes"] = window_wall_clock
            worker["window_utilization_pct"] = round(
                worker["window_worked_minutes"] / window_wall_clock * 100, 1
            )
            total_worked += worker["window_worked_minutes"]

        estate_wall_clock = window_wall_clock * len(by_worker)
        estate_pct = round(total_worked / estate_wall_clock * 100, 1) if estate_wall_clock else None

        return {
            "workers": rank(
                list(by_worker.values()),
                sort_key=lambda r: r.get("window_utilization_pct", 0),
                sorted_by="window_utilization_pct desc",
                reverse=True,
            ),
            "estate_worked_minutes": total_worked,
            "estate_wall_clock_minutes": estate_wall_clock,
            "estate_utilization_pct": estate_pct,
        }


def build_tier2_tools(engine) -> list[Callable]:
    """Build the insight tools as thin adapters over *engine*'s domain methods."""

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
        return to_envelope(engine.exception_summary(queue, start_date, end_date), limit)

    def estate_exception_summary(
        start_date: str, end_date: str, limit: int = DEFAULT_LIMIT
    ) -> dict:
        """Summarise exceptions across the WHOLE estate: counts grouped by reason.

        The estate-wide version of exception_summary — use it to find the
        dominant failure mode everywhere at once, instead of calling
        exception_summary for each queue. `start_date`/`end_date` (ISO,
        REQUIRED) bound the items' last-updated time. Each item gives a distinct
        exception reason (personal data already removed), how many items hit it
        across the estate, when it first and last occurred in the window, the
        resources involved, and the `queues` it appeared in.

        Reasons are grouped AFTER scrubbing, so messages that differ only in
        personal data fold into one bucket — counts reflect failure modes, not
        distinct customers. This reads every queue's exceptioned items, so on a
        large estate prefer a tight window.

        Results come back as {"items": [...], "meta": {...}}, most frequent
        first, capped at `limit` (default 50).
        """
        return to_envelope(engine.estate_exception_summary(start_date, end_date), limit)

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
        return to_envelope(engine.throughput_summary(start_date, end_date, process), limit)

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
        health = engine.estate_health()
        capped = to_envelope(health["workers_requiring_attention"], limit)
        return {
            "workers_total": health["workers_total"],
            "workers_by_status": health["workers_by_status"],
            "workers_requiring_attention": capped["items"],
            "attention_meta": capped["meta"],
            "license_usage": health["license_usage"],
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
        return engine.license_entitlement()

    def resource_utilization(
        start_date: str, end_date: str, limit: int = DEFAULT_LIMIT
    ) -> dict:
        """Per-worker utilisation over a window: minutes worked vs wall-clock time.

        `start_date`/`end_date` (ISO dates, REQUIRED) bound the window. Each
        worker's row gives `daily` entries (date, worked_minutes,
        wall_clock_minutes, utilization_pct) plus a windowed roll-up —
        `window_worked_minutes`, `window_wall_clock_minutes` (24h × the days in
        the window — an idle day counts as 0% worked, it is not excluded), and
        `window_utilization_pct`. Ranked highest windowed utilisation first,
        capped at `limit` (default 50). `estate_worked_minutes`/
        `estate_wall_clock_minutes`/`estate_utilization_pct` give the
        estate-wide duty cycle — total worked over total wall-clock across every
        worker that reported data, NOT an average of the per-worker
        percentages. Only raw minutes and percentages are returned; deciding
        what counts as "saturated" is a consumer judgement, not this tool's.

        The utilisation read needs a permission the account may lack; if it is
        denied or fails, the result carries an `unavailable` note instead of
        erroring, with the roll-up fields left null.
        """
        result = engine.resource_utilization(start_date, end_date)
        capped = to_envelope(result["workers"], limit)
        response = {
            "workers": capped["items"],
            "workers_meta": capped["meta"],
            "estate_worked_minutes": result["estate_worked_minutes"],
            "estate_wall_clock_minutes": result["estate_wall_clock_minutes"],
            "estate_utilization_pct": result["estate_utilization_pct"],
        }
        if "unavailable" in result:
            response["unavailable"] = result["unavailable"]
        return response

    return [
        exception_summary,
        estate_exception_summary,
        throughput_summary,
        estate_health,
        license_entitlement,
        resource_utilization,
    ]

"""Tests for the Engine facade (Phase 8 embeddable core).

These pin the *domain* contract a host embeds against: each read method returns
the FULL relevance-sorted records (a Ranked for list tools, a dict for single
reads), scrubbed at the PII boundary, with no top-N truncation and with
domain-level meta extras surfaced. The envelope/representation behaviour is
covered through the MCP adapters in test_tools.py.
"""

from __future__ import annotations

import pytest
import requests

from blue_prism_v7_mcp.engine import Engine
from blue_prism_v7_mcp.mock import MockBPClient, _date
from blue_prism_v7_mcp.pii import NullScrubber, ScrubResult
from blue_prism_v7_mcp.tools.common import Ranked, to_envelope


class MarkerScrubber:
    """Stamps every message so a test can prove scrubbing ran at the domain."""

    def scrub(self, text: str) -> ScrubResult:
        return ScrubResult(text="[SCRUBBED]", entity_types=("MARKER",))


def make_engine(client=None, scrubber=None) -> Engine:
    return Engine(client or MockBPClient(), scrubber or NullScrubber())


# A wide "everything up to now" window spanning every relative-anchored default
# fixture (oldest ~8 days back) without pinning an absolute calendar date.
WINDOW = {"start_date": _date(3650), "end_date": _date(0)}


class TestRankedShape:
    """List methods return Ranked: full records, sorted, no truncation."""

    def test_list_queues_returns_every_queue_untruncated(self):
        engine = make_engine()
        ranked = engine.list_queues()
        assert isinstance(ranked, Ranked)
        assert len(ranked.records) == len(MockBPClient().get_queues())
        # sorted by pending backlog, biggest first
        pending = [q.get("pendingItemCount", 0) for q in ranked.records]
        assert pending == sorted(pending, reverse=True)

    def test_domain_is_not_capped_by_the_representation_limit(self):
        # The whole point of the split: the domain returns everything, and the
        # envelope adapter is the only thing that truncates.
        engine = make_engine()
        ranked = engine.list_resources()
        capped = to_envelope(ranked, limit=1)
        assert len(capped["items"]) == 1
        assert len(ranked.records) > 1  # domain kept the rest
        assert capped["meta"]["total"] == len(ranked.records)
        assert capped["meta"]["truncated"] is True

    def test_single_reads_return_the_record_dict_not_a_ranked(self):
        engine = make_engine()
        queue = engine.get_queue("Invoices")
        assert isinstance(queue, dict)
        assert queue["name"] == "Invoices"

    def test_schedule_depth_reads_keep_the_domain_shapes(self):
        # The v0.11.0 reads follow the same split: single read → dict, list
        # reads → Ranked with the full chain/history for a host to consume.
        engine = make_engine()
        assert isinstance(engine.get_schedule("Daily Invoice Run"), dict)
        tasks = engine.list_schedule_tasks("Weekly Reconciliation")
        assert isinstance(tasks, Ranked)
        assert [t["id"] for t in tasks.records] == [21, 22]  # chain order
        logs = engine.list_schedule_logs()
        assert isinstance(logs, Ranked)
        assert len(logs.records) == 3  # every fixture run, untruncated


class TestDomainMeta:
    """Domain-level meta extras ride on Ranked.meta, merged by to_envelope."""

    def test_deferred_unavailable_is_domain_meta(self):
        class NoCompositions(MockBPClient):
            def get_queue_compositions(self, queue_ids):
                raise requests.HTTPError("403 Forbidden")

        ranked = make_engine(NoCompositions()).list_queues()
        assert ranked.meta == {"deferred_unavailable": True}
        # and it surfaces through the envelope
        assert to_envelope(ranked)["meta"]["deferred_unavailable"] is True

    def test_deferred_is_folded_into_every_ranked_record_when_available(self):
        ranked = make_engine().list_queues()
        # the mock reports a deferred composition for Invoices
        invoices = next(q for q in ranked.records if q["name"] == "Invoices")
        assert "deferred" in invoices

    def test_queue_configurations_degrade_to_empty_ranked_with_unavailable(self):
        class OldEstate(MockBPClient):
            def get_queue_configurations(self):
                raise requests.HTTPError("404 Not Found")

        ranked = make_engine(OldEstate()).list_queue_configurations()
        assert ranked.records == []
        assert "unavailable" in ranked.meta
        env = to_envelope(ranked)
        assert env["items"] == [] and "unavailable" in env["meta"]


class TestScrubAtTheDomainBoundary:
    """Scrubbing happens in the domain method, so an embedder gets clean records."""

    def test_get_queue_item_data_is_scrubbed(self):
        engine = make_engine(scrubber=MarkerScrubber())
        # the first exceptioned Invoices item in the window has data + a reason
        items = engine.list_queue_items(queue="Invoices", state="Exceptioned", **WINDOW)
        item_id = items.records[0]["id"]
        item = engine.get_queue_item(item_id)
        # free-text data cells are stamped by the marker scrubber
        assert any(
            "[SCRUBBED]" in str(cell) for row in item["data"]["rows"] for cell in row.values()
        )

    def test_environment_variable_value_is_scrubbed(self):
        ranked = make_engine(scrubber=MarkerScrubber()).list_environment_variables()
        # at least one free-text variable value/description is stamped
        assert any(
            v.get("value") == "[SCRUBBED]" or v.get("description") == "[SCRUBBED]"
            for v in ranked.records
        )


class TestEstateHealthComposite:
    """estate_health returns a composite whose attention list is a Ranked."""

    def test_attention_list_is_a_ranked_for_the_adapter_to_cap(self):
        health = make_engine().estate_health()
        assert isinstance(health["workers_requiring_attention"], Ranked)
        assert "workers_total" in health and "license_usage" in health


class TestResourceUtilizationComposite:
    """resource_utilization returns a composite whose workers list is a Ranked."""

    def test_workers_is_a_ranked_for_the_adapter_to_cap(self):
        result = make_engine().resource_utilization(_date(2), _date(0))
        assert isinstance(result["workers"], Ranked)
        assert "estate_utilization_pct" in result


class TestValidationStaysInTheDomain:
    """An embedder gets the same loud validation the MCP tools do."""

    def test_required_window_is_enforced(self):
        with pytest.raises(ValueError):
            make_engine().list_sessions(start_date=None, end_date=None)

    def test_unknown_state_is_rejected(self):
        with pytest.raises(ValueError):
            make_engine().list_queue_items(queue="Invoices", state="Nope", **WINDOW)


class TestQueueItemFilterWidening:
    """v0.12.0: within_sla/sla_before narrowing and loadedDate-asc sort."""

    def test_window_not_required_when_within_sla_given(self):
        engine = make_engine()
        # No start_date/end_date at all — within_sla is scope enough.
        ranked = engine.list_queue_items(queue="Invoices", state="Exceptioned", within_sla=False)
        assert ranked.records

    def test_window_not_required_when_sla_before_given(self):
        engine = make_engine()
        ranked = engine.list_queue_items(
            queue="Invoices", state="Exceptioned", sla_before=_date(-3650)
        )
        assert ranked.records

    def test_window_still_required_without_sla_scoping(self):
        with pytest.raises(ValueError):
            make_engine().list_queue_items(queue="Invoices", state="Exceptioned")

    def test_malformed_sla_before_is_rejected(self):
        with pytest.raises(ValueError):
            make_engine().list_queue_items(
                queue="Invoices", state="Exceptioned", within_sla=False, sla_before="not-a-date"
            )

    def test_unknown_sort_by_is_rejected(self):
        with pytest.raises(ValueError):
            make_engine().list_queue_items(
                queue="Invoices", state="Exceptioned", sort_by="Bogus", **WINDOW
            )

    def test_sort_by_loaded_date_asc_changes_ranking(self):
        engine = make_engine()
        ranked = engine.list_queue_items(
            queue="Invoices", state="Exceptioned", sort_by="loadedDate asc", **WINDOW
        )
        assert ranked.sorted_by == "loadedDate asc"
        loaded = [i["loadedDate"] for i in ranked.records]
        assert loaded == sorted(loaded)

    def test_default_sort_is_unchanged(self):
        engine = make_engine()
        ranked = engine.list_queue_items(queue="Invoices", state="Exceptioned", **WINDOW)
        assert ranked.sorted_by == "lastUpdated desc"


class TestMaxRecords:
    """v0.15.0: max_records is an embeddable-core-only fetch-time cap."""

    def test_max_records_one_with_oldest_first_sort_gives_the_single_oldest(self):
        engine = make_engine()
        full = engine.list_queue_items(
            queue="Invoices", state="Exceptioned", sort_by="loadedDate asc", **WINDOW
        )
        capped = engine.list_queue_items(
            queue="Invoices",
            state="Exceptioned",
            sort_by="loadedDate asc",
            max_records=1,
            **WINDOW,
        )
        assert [i["id"] for i in capped.records] == [i["id"] for i in full.records[:1]]

    def test_max_records_none_is_unaffected(self):
        engine = make_engine()
        without = engine.list_queue_items(queue="Invoices", state="Exceptioned", **WINDOW)
        with_none = engine.list_queue_items(
            queue="Invoices", state="Exceptioned", max_records=None, **WINDOW
        )
        assert [i["id"] for i in with_none.records] == [i["id"] for i in without.records]

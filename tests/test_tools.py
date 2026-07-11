"""Tests for the tool layer (Phase 4): shared contract + Tier 1 + Tier 2.

Every tool test runs over MockBPClient — the drop-in offline client whose
fixtures mirror the verified v7 response schemas — so these tests pin the
tool-facing contract (envelope, scoping, scrubbing, resolution) without any
HTTP. Scrub assertions use a marker scrubber that stamps every message, so
"was this field scrubbed?" is a string equality, not a pattern guess.
"""

import json

import pytest
import requests

from blue_prism_v7_mcp.config import BPConfig
from blue_prism_v7_mcp.engine import Engine
from blue_prism_v7_mcp.mock import MockBPClient, _date, _ts
from blue_prism_v7_mcp.pii import NullScrubber, RegexScrubber, ScrubResult
from blue_prism_v7_mcp.tools import (
    DEFAULT_LIMIT,
    build_tier1_tools,
    build_tier2_tools,
    envelope,
    make_cached_scrub,
    register_tools,
    resolve_id,
)
from blue_prism_v7_mcp.tools.common import (
    require_window,
    validate_choice,
    validate_data_value,
    validate_iso,
    validate_queue_items,
)


class MarkerScrubber:
    """Stamps every message and counts calls, so tests can assert both that a
    field passed through the scrubber and that the cache deduplicated calls."""

    def __init__(self):
        self.calls = 0

    def scrub(self, text: str) -> ScrubResult:
        self.calls += 1
        return ScrubResult(text="[SCRUBBED]", entity_types=("MARKER",))


def tier1(client=None, scrubber=None) -> dict:
    engine = Engine(client or MockBPClient(), scrubber or NullScrubber())
    return {t.__name__: t for t in build_tier1_tools(engine)}


def tier2(client=None, scrubber=None) -> dict:
    engine = Engine(client or MockBPClient(), scrubber or NullScrubber())
    return {t.__name__: t for t in build_tier2_tools(engine)}


# A deliberately wide "everything up to now" window: it spans the relative-
# anchored default fixtures (oldest ~8 days back) and any incidental dates the
# self-seeding tests below use, without pinning an absolute calendar date. The
# narrow-window tests that probe the bounds set their own off _date/_ts.
WINDOW = {"start_date": _date(3650), "end_date": _date(0)}


# --- The envelope ---------------------------------------------------------------


class TestEnvelope:
    ROWS = [{"n": i} for i in (3, 1, 2)]

    def test_sorts_and_reports_honestly(self):
        result = envelope(self.ROWS, sort_key=lambda r: r["n"], sorted_by="n", limit=2)
        assert [r["n"] for r in result["items"]] == [1, 2]
        assert result["meta"] == {
            "total": 3,
            "returned": 2,
            "truncated": True,
            "sorted_by": "n",
        }

    def test_reverse_sort(self):
        result = envelope(self.ROWS, sort_key=lambda r: r["n"], sorted_by="n", reverse=True)
        assert [r["n"] for r in result["items"]] == [3, 2, 1]

    @pytest.mark.parametrize("limit", [None, -1])
    def test_no_limit_returns_everything(self, limit):
        result = envelope(self.ROWS, sort_key=lambda r: r["n"], sorted_by="n", limit=limit)
        assert result["meta"]["returned"] == 3
        assert result["meta"]["truncated"] is False

    def test_limit_zero_returns_empty_but_truthful_meta(self):
        result = envelope(self.ROWS, sort_key=lambda r: r["n"], sorted_by="n", limit=0)
        assert result["items"] == []
        assert result["meta"]["total"] == 3
        assert result["meta"]["truncated"] is True

    def test_limit_exactly_at_total_is_not_truncated(self):
        # Boundary: a result that JUST fits must not claim truncation.
        result = envelope(self.ROWS, sort_key=lambda r: r["n"], sorted_by="n", limit=3)
        assert result["meta"]["returned"] == 3
        assert result["meta"]["truncated"] is False

    def test_empty_rows(self):
        result = envelope([], sort_key=lambda r: r.get("n", 0), sorted_by="n")
        assert result["items"] == []
        assert result["meta"] == {
            "total": 0,
            "returned": 0,
            "truncated": False,
            "sorted_by": "n",
        }


# --- Validation -----------------------------------------------------------------


class TestValidateIso:
    @pytest.mark.parametrize("value", ["2026-03-01", "2026-03-01T09:00:00", "2026-03-01T09:00:00Z"])
    def test_accepts_iso_dates_and_datetimes(self, value):
        validate_iso(value, "start_date")  # no raise

    def test_none_is_fine_when_optional(self):
        validate_iso(None, "start_date")

    def test_none_fails_when_required(self):
        with pytest.raises(ValueError, match="start_date is required"):
            validate_iso(None, "start_date", required=True)

    @pytest.mark.parametrize("value", ["yesterday", "01/03/2026", "", "2026-13-01"])
    def test_rejects_malformed_values_naming_the_field(self, value):
        with pytest.raises(ValueError, match="end_date"):
            validate_iso(value, "end_date")


class TestRequireWindow:
    def test_valid_window_passes(self):
        require_window("2026-03-01", "2026-03-31")

    def test_same_day_window_passes(self):
        require_window("2026-03-01", "2026-03-01")

    @pytest.mark.parametrize("start,end", [(None, "2026-03-31"), ("2026-03-01", None)])
    def test_missing_bound_fails(self, start, end):
        with pytest.raises(ValueError, match="required"):
            require_window(start, end)

    def test_reversed_window_fails_loudly(self):
        with pytest.raises(ValueError, match="swap the bounds"):
            require_window("2026-03-31", "2026-03-01")

    def test_mixed_aware_and_naive_bounds_do_not_crash(self):
        # An aware bound next to a naive one must not TypeError out of the
        # ordering sanity check.
        require_window("2026-03-01T00:00:00Z", "2026-03-31")


class TestValidateChoice:
    STATES = frozenset({"Pending", "Exceptioned"})

    @pytest.mark.parametrize("value", ["Exceptioned", "exceptioned", "  EXCEPTIONED "])
    def test_normalises_case_and_whitespace_to_canonical(self, value):
        assert validate_choice(value, "state", self.STATES) == "Exceptioned"

    @pytest.mark.parametrize("value", ["Done", "", None])
    def test_rejects_unknown_values_listing_the_choices(self, value):
        with pytest.raises(ValueError, match="Exceptioned, Pending"):
            validate_choice(value, "state", self.STATES)


# --- Name → UUID resolution -------------------------------------------------------


class TestResolveId:
    RECORDS = [
        {"id": "9b6f3a1c-2e45-4d07-8c11-000000000101", "name": "Invoices"},
        {"id": "9b6f3a1c-2e45-4d07-8c11-000000000102", "name": "Onboarding"},
    ]

    def test_uuid_passes_straight_through(self):
        uuid = "11111111-2222-3333-4444-555555555555"  # not even in the records
        assert resolve_id(uuid, self.RECORDS, entity="queue") == uuid

    def test_known_id_passes_through_even_if_not_uuid_shaped(self):
        records = [{"id": "q-legacy-1", "name": "Old"}]
        assert resolve_id("q-legacy-1", records, entity="queue") == "q-legacy-1"

    @pytest.mark.parametrize("value", ["Invoices", "invoices", "  INVOICES "])
    def test_name_resolves_case_insensitively(self, value):
        assert resolve_id(value, self.RECORDS, entity="queue") == self.RECORDS[0]["id"]

    def test_miss_suggests_close_names(self):
        with pytest.raises(ValueError, match="Did you mean: Invoices"):
            resolve_id("Invocies", self.RECORDS, entity="queue")

    def test_miss_without_close_match_lists_known_names(self):
        with pytest.raises(ValueError, match="Known queues: Invoices, Onboarding"):
            resolve_id("zzz", self.RECORDS, entity="queue")

    def test_miss_against_no_records_still_fails_cleanly(self):
        with pytest.raises(ValueError, match="No queue named 'anything'."):
            resolve_id("anything", [], entity="queue")

    def test_duplicate_names_fail_listing_every_id(self):
        records = [
            {"id": "a" * 8, "name": "Dup"},
            {"id": "b" * 8, "name": "dup"},
        ]
        with pytest.raises(ValueError, match="ambiguous — 2 match"):
            resolve_id("Dup", records, entity="queue")

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_empty_input_fails(self, value):
        with pytest.raises(ValueError, match="must be a name or id"):
            resolve_id(value, self.RECORDS, entity="queue")

    def test_alternate_key_names(self):
        # Process is keyed processId/processName, not id/name.
        records = [{"processId": "p-1", "processName": "Invoice Processing"}]
        resolved = resolve_id(
            "invoice processing",
            records,
            entity="process",
            id_key="processId",
            name_key="processName",
        )
        assert resolved == "p-1"


# --- The cached scrub boundary -----------------------------------------------------


class TestCachedScrub:
    def test_scrubs_through_the_backend(self):
        scrub = make_cached_scrub(MarkerScrubber())
        assert scrub("Call John on 07700 900123") == "[SCRUBBED]"

    def test_identical_messages_hit_the_backend_once(self):
        scrubber = MarkerScrubber()
        scrub = make_cached_scrub(scrubber)
        for _ in range(3):
            scrub("same message")
        assert scrubber.calls == 1

    @pytest.mark.parametrize("value", [None, ""])
    def test_null_and_empty_pass_through_untouched(self, value):
        # exceptionReason: null must survive as null, not become a scrubbed
        # empty string.
        scrubber = MarkerScrubber()
        scrub = make_cached_scrub(scrubber)
        assert scrub(value) == value
        assert scrubber.calls == 0


# --- Tier 1 -----------------------------------------------------------------------


class TestListQueues:
    def test_envelope_sorted_by_backlog(self):
        result = tier1()["list_queues"]()
        names = [q["name"] for q in result["items"]]
        assert names == ["Invoices", "Onboarding"]  # 12 pending before 0
        assert result["meta"]["sorted_by"] == "pendingItemCount desc"
        assert result["meta"]["truncated"] is False

    def test_limit_truncates_with_honest_meta(self):
        result = tier1()["list_queues"](limit=1)
        assert len(result["items"]) == 1
        assert result["meta"] == {
            "total": 2,
            "returned": 1,
            "truncated": True,
            "sorted_by": "pendingItemCount desc",
        }

    def test_folds_in_the_deferred_count_from_compositions(self):
        # WorkQueueSummary has no deferred field; list_queues adds it from the
        # workQueueCompositions aggregate (Invoices has 3 deferred in the mock).
        rows = {q["name"]: q for q in tier1()["list_queues"]()["items"]}
        assert rows["Invoices"]["deferred"] == 3
        assert rows["Onboarding"]["deferred"] == 0  # no entry => zero, not unknown

    def test_deferred_is_folded_into_the_full_ranked_set_not_just_the_page(self):
        # The deferred fold-in is domain enrichment, so it covers every ranked
        # queue — an embedder consuming the records gets deferred on all of them.
        # The MCP `limit` is representation-only: it caps the items shown without
        # narrowing which queues were enriched.
        class SpyClient(MockBPClient):
            asked_for = None

            def get_queue_compositions(self, queue_ids):
                SpyClient.asked_for = list(queue_ids)
                return super().get_queue_compositions(queue_ids)

        all_ids = [q["id"] for q in SpyClient().get_queues()]
        result = tier1(SpyClient())["list_queues"](limit=1)
        assert len(result["items"]) == 1  # Invoices (biggest backlog) — page capped
        assert sorted(SpyClient.asked_for) == sorted(all_ids)  # but all were enriched

    @pytest.mark.parametrize(
        "error",
        [requests.HTTPError("403 Forbidden"), requests.Timeout("read timed out")],
        ids=["denied", "timeout"],
    )
    def test_deferred_omitted_and_flagged_when_the_aggregate_read_fails(self, error):
        # A denied or dropped compositions read must not lose the listing; the
        # deferred field is absent and meta.deferred_unavailable degrades visibly.
        class NoCompositionsClient(MockBPClient):
            def get_queue_compositions(self, queue_ids):
                raise error

        result = tier1(NoCompositionsClient())["list_queues"]()
        assert result["items"]  # listing still stands
        assert all("deferred" not in q for q in result["items"])
        assert result["meta"]["deferred_unavailable"] is True

    def test_deferred_omitted_for_a_queue_the_aggregate_does_not_report(self):
        # A null count, or a queue missing from a short/paged response, is
        # UNKNOWN — the field is omitted for it, never a fabricated zero. Here
        # the aggregate reports only Invoices; Onboarding must not show deferred.
        class PartialClient(MockBPClient):
            def get_queue_compositions(self, queue_ids):
                return [{"id": queue_ids[0], "name": "Invoices", "deferred": 7}]

        rows = {q["name"]: q for q in tier1(PartialClient())["list_queues"]()["items"]}
        assert rows["Invoices"]["deferred"] == 7
        assert "deferred" not in rows["Onboarding"]

    def test_deferred_omitted_when_its_count_is_null(self):
        class NullDeferredClient(MockBPClient):
            def get_queue_compositions(self, queue_ids):
                return [{"id": qid, "deferred": None} for qid in queue_ids]

        rows = tier1(NullDeferredClient())["list_queues"]()["items"]
        assert all("deferred" not in q for q in rows)

    def test_deferred_omitted_when_the_body_is_not_a_list(self):
        # A gateway that reshapes the array into an object must not crash the
        # listing — no usable rows, so deferred is simply omitted (read succeeded).
        class ReshapedClient(MockBPClient):
            def get_queue_compositions(self, queue_ids):
                return {"items": []}  # not the bare array the endpoint returns

        result = tier1(ReshapedClient())["list_queues"]()
        assert result["items"]
        assert all("deferred" not in q for q in result["items"])
        assert "deferred_unavailable" not in result["meta"]  # the read itself worked

    def test_empty_estate_skips_the_composition_request(self):
        class SpyClient(MockBPClient):
            def get_queue_compositions(self, queue_ids):  # pragma: no cover
                raise AssertionError("must not request compositions for no queues")

        result = tier1(SpyClient(queues=[]))["list_queues"]()
        assert result["items"] == []


class TestGetQueue:
    def test_resolves_name_to_the_queue(self):
        queue = tier1()["get_queue"]("invoices")
        assert queue["name"] == "Invoices"
        assert queue["pendingItemCount"] == 12

    def test_accepts_the_raw_id(self):
        client = MockBPClient()
        qid = client.get_queues()[0]["id"]
        assert tier1(client)["get_queue"](qid)["id"] == qid

    def test_unknown_name_fails_with_suggestions(self):
        with pytest.raises(ValueError, match="Did you mean: Invoices"):
            tier1()["get_queue"]("Invocies")


class TestListQueueItems:
    def test_requires_a_valid_state(self):
        with pytest.raises(ValueError, match="state must be one of"):
            tier1()["list_queue_items"]("Invoices", "Broken", **WINDOW)

    def test_requires_the_window(self):
        with pytest.raises(ValueError, match="start_date is required"):
            tier1()["list_queue_items"]("Invoices", "Pending", None, None)

    def test_filters_and_envelopes(self):
        result = tier1()["list_queue_items"]("Invoices", "exceptioned", **WINDOW)
        assert [i["keyValue"] for i in result["items"]] == ["INV-1002"]
        assert result["meta"]["sorted_by"] == "lastUpdated desc"

    def test_exception_reason_is_scrubbed(self):
        result = tier1(scrubber=MarkerScrubber())["list_queue_items"](
            "Invoices", "Exceptioned", **WINDOW
        )
        assert result["items"][0]["exceptionReason"] == "[SCRUBBED]"

    def test_null_exception_reason_survives_as_null(self):
        result = tier1(scrubber=MarkerScrubber())["list_queue_items"](
            "Invoices", "Completed", **WINDOW
        )
        assert result["items"][0]["exceptionReason"] is None

    def test_most_recent_first(self):
        client = MockBPClient(
            queue_items=[
                {"queue": "q", "id": "old", "state": "Pending", "lastUpdated": "2026-03-01"},
                {"queue": "q", "id": "new", "state": "Pending", "lastUpdated": "2026-03-09"},
            ],
            queues=[{"id": "q", "name": "Q"}],
        )
        result = tier1(client)["list_queue_items"]("Q", "Pending", **WINDOW)
        assert [i["id"] for i in result["items"]] == ["new", "old"]

    def test_window_bounds_are_forwarded_to_the_client(self):
        # The Invoices fixtures hold a Completed item 8 days back and an
        # Exceptioned one 7 days back; each bound must actually reach the client
        # or the tool silently reads outside its stated window.
        items = tier1()["list_queue_items"]
        assert items("Invoices", "Exceptioned", _date(6), _date(0))["items"] == []
        assert items("Invoices", "Completed", _date(8), _date(8))["meta"]["total"] == 1

    def test_session_id_survives_the_scrub_spread_untouched(self):
        # sessionId is the item→session/resource correlation an agent needs
        # to derive exception classification — it must reach the model
        # unmodified through the {**item, exceptionReason: scrub} spread.
        result = tier1()["list_queue_items"]("Invoices", "Exceptioned", **WINDOW)
        assert result["items"][0]["sessionId"] == "e8a9d7c2-5f10-4b3e-bd64-000000000301"

    def test_status_text_filter_is_forwarded_to_the_client(self):
        client = MockBPClient(
            queues=[{"id": "q", "name": "Q"}],
            queue_items=[
                {
                    "queue": "q",
                    "id": "i1",
                    "state": "Pending",
                    "status": "awaiting review",
                    "lastUpdated": "2026-03-02",
                },
                {
                    "queue": "q",
                    "id": "i2",
                    "state": "Pending",
                    "status": "",
                    "lastUpdated": "2026-03-02",
                },
            ],
        )
        result = tier1(client)["list_queue_items"](
            "Q", "Pending", **WINDOW, status="awaiting review"
        )
        assert [i["id"] for i in result["items"]] == ["i1"]

    def test_within_sla_narrows_without_a_date_window(self):
        # No start_date/end_date passed at all — within_sla is scope enough.
        result = tier1()["list_queue_items"]("Invoices", "Exceptioned", within_sla=False)
        assert [i["keyValue"] for i in result["items"]] == ["INV-1002"]

    def test_sla_before_narrows_without_a_date_window(self):
        result = tier1()["list_queue_items"]("Invoices", "Exceptioned", sla_before=_date(-3650))
        assert [i["keyValue"] for i in result["items"]] == ["INV-1002"]

    def test_sort_by_loaded_date_asc(self):
        result = tier1()["list_queue_items"](
            "Invoices",
            "Completed",
            start_date=_date(3650),
            end_date=_date(0),
            sort_by="loadedDate asc",
        )
        assert result["meta"]["sorted_by"] == "loadedDate asc"


def _exception_item_id(client: MockBPClient) -> str:
    qid = next(q["id"] for q in client.get_queues() if q["name"] == "Invoices")
    return client.get_queue_items(qid, state="Exceptioned")[0]["id"]


class TestGetQueueItem:
    def test_returns_the_single_item_with_its_data(self):
        client = MockBPClient()
        item = tier1(client)["get_queue_item"](_exception_item_id(client))
        assert item["keyValue"] == "INV-1002"
        assert "rows" in item["data"]  # the only read carrying the payload
        assert "queue" not in item  # mock-internal plumbing never surfaces

    def test_rejects_a_non_uuid_item_id(self):
        with pytest.raises(ValueError, match="item_id must be a UUID"):
            tier1()["get_queue_item"]("INV-1002")

    def test_exception_reason_is_scrubbed(self):
        client = MockBPClient()
        item = tier1(client, MarkerScrubber())["get_queue_item"](_exception_item_id(client))
        assert item["exceptionReason"] == "[SCRUBBED]"

    def test_data_payload_is_scrubbed_type_aware(self):
        # The crux of v0.3.0: the only payload the read surface exposes is
        # scrubbed by Blue Prism value type — free text through the scrubber,
        # passwords redacted, binaries dropped, scalars kept, collections recursed.
        client = MockBPClient()
        item = tier1(client, MarkerScrubber())["get_queue_item"](_exception_item_id(client))
        row = item["data"]["rows"][0]
        assert row["Contact"]["value"] == "[SCRUBBED]"  # Text through the scrubber
        assert row["VaultPassword"]["value"] == "[PASSWORD]"  # secret redacted wholesale
        assert row["Scan"]["value"] == "[BINARY omitted]"  # base64 dropped
        assert row["Scan"]["additionalParameters"] == ["[SCRUBBED]"]  # filename scrubbed too
        assert row["Logo"]["value"] == "[IMAGE omitted]"  # image base64 dropped
        assert row["Amount"]["value"] == 1499.99  # scalar kept
        assert row["Approved"]["value"] is False  # scalar kept
        nested = row["LineItems"]["value"]["rows"][0]
        assert nested["Desc"]["value"] == "[SCRUBBED]"  # nested collection recursed
        assert nested["Net"]["value"] == 1249.99

    def test_data_scrub_fails_closed_on_unknown_and_miscased_types(self):
        # The scrub is a security boundary: a value type the server spells
        # differently (or one not in the spec, or a RadioButtons label array)
        # must NOT pass through verbatim — its string content is scrubbed and a
        # miscased Password is still redacted.
        class OddClient(MockBPClient):
            def get_queue_item(self, item_id):
                return {
                    "id": item_id,
                    "exceptionReason": None,
                    "data": {
                        "rows": [
                            {
                                "Legacy": {"valueType": "text", "value": "secret note"},
                                "Secret": {"valueType": "password", "value": "hunter2"},
                                "Options": {
                                    "valueType": "RadioButtons",
                                    "value": ["pick one", "or two"],
                                },
                                "Mystery": {"valueType": "Quantum", "value": "leak me"},
                                "Empty": {"valueType": "Text", "value": None},
                                "Count": {"valueType": "Tally", "value": 7},
                            }
                        ]
                    },
                }

        item = tier1(OddClient(), MarkerScrubber())["get_queue_item"](
            "f3b2a190-8c47-4e2d-9b55-000000000402"
        )
        row = item["data"]["rows"][0]
        assert row["Legacy"]["value"] == "[SCRUBBED]"  # miscased Text still scrubbed
        assert row["Secret"]["value"] == "[PASSWORD]"  # miscased Password still redacted
        assert row["Options"]["value"] == ["[SCRUBBED]", "[SCRUBBED]"]  # list scrubbed elementwise
        assert row["Mystery"]["value"] == "[SCRUBBED]"  # unknown type fails closed
        assert row["Empty"]["value"] is None  # None has no text to scrub
        assert row["Count"]["value"] == 7  # an unknown type's numeric value is left as-is

    def test_data_payload_pii_is_actually_removed_end_to_end(self):
        # With the real regex scrubber: no supplier phone number (top-level or
        # nested) and no password value ever reaches the model in the payload.
        client = MockBPClient()
        item = tier1(client, RegexScrubber())["get_queue_item"](_exception_item_id(client))
        blob = json.dumps(item["data"])
        assert "07700 900123" not in blob
        assert "07700 900456" not in blob
        assert "s3cret-Pa55word" not in blob

    def test_empty_data_collection_is_a_scrub_noop(self):
        # An item with no payload still answers a data field; scrubbing a
        # rows-less collection must not crash.
        client = MockBPClient()
        qid = next(q["id"] for q in client.get_queues() if q["name"] == "Invoices")
        completed = client.get_queue_items(qid, state="Completed")[0]["id"]
        assert tier1(client)["get_queue_item"](completed)["data"] == {"rows": []}

    def test_session_id_survives_the_scrub_spread_untouched(self):
        client = MockBPClient()
        item = tier1(client)["get_queue_item"](_exception_item_id(client))
        assert item["sessionId"] == "e8a9d7c2-5f10-4b3e-bd64-000000000301"

    def test_sla_date_time_is_the_single_item_capital_t_spelling(self):
        # The single-item WorkQueueItem shape spells the SLA deadline
        # `slaDateTime`; the list/attempt NoData shape spells it `slaDatetime`.
        client = MockBPClient()
        item = tier1(client)["get_queue_item"](_exception_item_id(client))
        assert "slaDateTime" in item
        assert "slaDatetime" not in item

    def test_non_collection_data_passes_through_untouched(self):
        # Defensive: a null/odd-shaped `data` (e.g. an item the server returns
        # with no collection) must survive rather than raise mid-scrub.
        class OddClient(MockBPClient):
            def get_queue_item(self, item_id):
                return {"id": item_id, "exceptionReason": None, "data": None}

        item = tier1(OddClient(), MarkerScrubber())["get_queue_item"](
            "f3b2a190-8c47-4e2d-9b55-000000000402"
        )
        assert item["data"] is None


class TestListItemAttempts:
    def test_lists_attempt_history_latest_first(self):
        client = MockBPClient()
        qid = next(q["id"] for q in client.get_queues() if q["name"] == "Invoices")
        result = tier1(client)["list_item_attempts"]("Invoices", _exception_item_id(client))
        assert [a["attemptNumber"] for a in result["items"]] == [2, 1]
        assert result["meta"]["sorted_by"].startswith("attemptNumber desc")
        assert qid  # queue resolved by name

    def test_resolves_queue_name_and_rejects_a_non_uuid_item(self):
        with pytest.raises(ValueError, match="item_id must be a UUID"):
            tier1()["list_item_attempts"]("Invoices", "INV-1002")

    def test_exception_reason_is_scrubbed(self):
        client = MockBPClient()
        result = tier1(client, MarkerScrubber())["list_item_attempts"](
            "Invoices", _exception_item_id(client)
        )
        # The first attempt carried an exception reason; the retry attempt's is null.
        reasons = {a["exceptionReason"] for a in result["items"]}
        assert reasons == {"[SCRUBBED]", None}

    def test_unknown_queue_name_fails_with_suggestions(self):
        with pytest.raises(ValueError, match="Did you mean: Invoices"):
            tier1()["list_item_attempts"]("Invocies", _exception_item_id(MockBPClient()))

    def test_session_id_is_per_attempt_not_just_per_item(self):
        # Each attempt row carries the sessionId of the session that worked
        # THAT attempt — the exceptioned attempt was worked (a session id),
        # the not-yet-retried attempt has none.
        client = MockBPClient()
        result = tier1(client)["list_item_attempts"]("Invoices", _exception_item_id(client))
        by_attempt = {a["attemptNumber"]: a["sessionId"] for a in result["items"]}
        assert by_attempt[1] == "e8a9d7c2-5f10-4b3e-bd64-000000000301"
        assert by_attempt[2] is None


class TestGetSession:
    def test_returns_the_single_session(self):
        client = MockBPClient()
        sid = client.get_sessions()[0]["sessionId"]
        assert tier1(client)["get_session"](sid)["sessionId"] == sid

    def test_rejects_a_non_uuid_session_id(self):
        with pytest.raises(ValueError, match="session_id must be a UUID"):
            tier1()["get_session"]("session-7")

    def test_exception_message_is_scrubbed(self):
        client = MockBPClient()
        failed = next(s for s in client.get_sessions() if s["status"] == "Terminated")
        session = tier1(client, MarkerScrubber())["get_session"](failed["sessionId"])
        assert session["exceptionMessage"] == "[SCRUBBED]"

    def test_null_exception_message_survives_as_null(self):
        client = MockBPClient()
        ok = next(s for s in client.get_sessions() if s["status"] == "Completed")
        session = tier1(client, MarkerScrubber())["get_session"](ok["sessionId"])
        assert session["exceptionMessage"] is None


class TestListSessions:
    def test_requires_the_window(self):
        with pytest.raises(ValueError, match="required"):
            tier1()["list_sessions"](None, None)

    def test_most_recent_first(self):
        result = tier1()["list_sessions"](**WINDOW)
        starts = [s["startTime"] for s in result["items"]]
        assert starts == sorted(starts, reverse=True)

    def test_window_bounds_are_forwarded_to_the_client(self):
        # Fixture sessions start 8, 7, and 4 days back; a window spanning days
        # 7..5 must exclude both outer ones — each bound has to actually reach
        # the client.
        result = tier1()["list_sessions"](_date(7), _date(5))
        assert [s["sessionNumber"] for s in result["items"]] == [2]

    def test_filters_by_process_name_case_insensitively(self):
        result = tier1()["list_sessions"](**WINDOW, process="invoice processing")
        assert result["meta"]["total"] == 2
        assert all(s["processName"] == "Invoice Processing" for s in result["items"])

    def test_filters_by_resource_name_case_insensitively(self):
        # BOT-02 holds two fixtures (a Terminated run and an in-flight Running
        # one); the filter keeps both and nothing from another worker.
        result = tier1()["list_sessions"](**WINDOW, resource="bot-02")
        assert result["items"], "expected BOT-02 sessions"
        assert all(s["resourceName"] == "BOT-02" for s in result["items"])

    def test_filters_by_status_with_canonical_casing(self):
        result = tier1()["list_sessions"](**WINDOW, status="terminated")
        assert [s["status"] for s in result["items"]] == ["Terminated"]

    def test_rejects_an_unknown_status(self):
        with pytest.raises(ValueError, match="status must be one of"):
            tier1()["list_sessions"](**WINDOW, status="Crashed")

    def test_exception_message_is_scrubbed(self):
        result = tier1(scrubber=MarkerScrubber())["list_sessions"](**WINDOW, status="Terminated")
        assert result["items"][0]["exceptionMessage"] == "[SCRUBBED]"

    def test_null_exception_message_survives_as_null(self):
        result = tier1(scrubber=MarkerScrubber())["list_sessions"](**WINDOW, status="Completed")
        assert all(s["exceptionMessage"] is None for s in result["items"])


class TestGetSessionLog:
    def _failed_session(self, client):
        return next(s for s in client.get_sessions() if s["status"] == "Terminated")

    def test_latest_stage_first(self):
        client = MockBPClient()
        result = tier1(client)["get_session_log"](self._failed_session(client)["sessionId"])
        numbers = [e["logNumber"] for e in result["items"]]
        assert numbers == sorted(numbers, reverse=True)
        assert "latest stage first" in result["meta"]["sorted_by"]

    def test_stage_results_are_scrubbed(self):
        client = MockBPClient()
        result = tier1(client, MarkerScrubber())["get_session_log"](
            self._failed_session(client)["sessionId"]
        )
        # Every stage carrying result text is scrubbed; empty results stay empty.
        with_text = [e for e in result["items"] if e["result"]]
        assert with_text and all(e["result"] == "[SCRUBBED]" for e in with_text)

    def test_errors_only_filters_to_exception_handling_stages(self):
        client = MockBPClient()
        result = tier1(client)["get_session_log"](
            self._failed_session(client)["sessionId"], errors_only=True
        )
        assert result["items"] and all(
            e["stageType"] in {"Exception", "Recover", "Resume"} for e in result["items"]
        )

    def test_window_bounds_stage_execution_time(self):
        client = MockBPClient()
        result = tier1(client)["get_session_log"](
            self._failed_session(client)["sessionId"],
            start_date=_ts(7, "10:00:00"),
            end_date=_ts(7, "10:01:00"),
        )
        assert result["items"] and all(e["stageType"] != "Exception" for e in result["items"])

    def test_reversed_window_fails_loudly(self):
        client = MockBPClient()
        with pytest.raises(ValueError, match="after end_date"):
            tier1(client)["get_session_log"](
                self._failed_session(client)["sessionId"],
                start_date="2026-03-02",
                end_date="2026-03-01",
            )

    def test_a_single_bound_is_accepted(self):
        # Either bound may be omitted (the window only narrows an already-scoped
        # read) — a lone bound must not trip the order check or crash on the
        # missing one.
        client = MockBPClient()
        sid = self._failed_session(client)["sessionId"]
        assert tier1(client)["get_session_log"](sid, start_date=_date(7))["items"]
        assert tier1(client)["get_session_log"](sid, end_date=_date(6))["items"]

    def test_unknown_session_yields_an_empty_envelope(self):
        result = tier1()["get_session_log"]("no-such-session")
        assert result["items"] == []
        assert result["meta"]["total"] == 0


class TestListResources:
    def test_most_urgent_first(self):
        result = tier1()["list_resources"]()
        statuses = [r["displayStatus"] for r in result["items"]]
        assert statuses[0] == "Offline"  # BOT-03 outranks Idle/Working

    def test_unknown_statuses_sort_last_not_crash(self):
        client = MockBPClient(
            resources=[
                {"name": "B-NEW", "displayStatus": "SomethingNew"},
                {"name": "B-DOWN", "displayStatus": "Offline"},
                {"name": "B-NONE"},  # no status at all
            ]
        )
        result = tier1(client)["list_resources"]()
        assert [r["name"] for r in result["items"]] == ["B-DOWN", "B-NONE", "B-NEW"]


class TestListSchedules:
    def test_active_first_then_name_retired_last(self):
        client = MockBPClient(
            schedules=[
                {"id": 1, "name": "Zulu", "isRetired": False},
                {"id": 2, "name": "Alpha", "isRetired": True},
                {"id": 3, "name": "Echo", "isRetired": False},
            ]
        )
        result = tier1(client)["list_schedules"]()
        assert [s["name"] for s in result["items"]] == ["Echo", "Zulu", "Alpha"]

    def test_last_run_folds_in_where_the_schedule_has_run(self):
        client = MockBPClient(
            schedules=[
                {"id": 1, "name": "Daily Invoice Run", "isRetired": False},
                {"id": 9, "name": "Never Run", "isRetired": False},
            ]
        )
        result = tier1(client)["list_schedules"]()
        by_name = {s["name"]: s for s in result["items"]}
        # Schedule 1 has runs → its latest outcome is folded in...
        assert by_name["Daily Invoice Run"]["last_run"] == {
            "status": "completed",
            "startTime": _ts(1, "06:00:00"),
            "endTime": _ts(1, "06:12:40"),
            "duration": "00:12:40",
        }
        # ...one that never ran carries no last_run (never a fabricated one).
        assert "last_run" not in by_name["Never Run"]
        assert "last_run_unavailable" not in result["meta"]

    def test_schedule_without_an_id_is_not_enriched(self):
        # A schedule row with no id can't be looked up — it is skipped, not
        # crashed on, and carries no last_run.
        client = MockBPClient(schedules=[{"name": "Orphan", "isRetired": False}])
        result = tier1(client)["list_schedules"]()
        assert "last_run" not in result["items"][0]
        assert "last_run_unavailable" not in result["meta"]

    def test_an_id_less_schedule_does_not_stop_later_enrichment(self):
        # An id-less row is skipped, not a hard stop — a schedule that sorts
        # after it still gets its last run folded in.
        client = MockBPClient(
            schedules=[
                {"name": "AAA No Id", "isRetired": False},
                {"id": 1, "name": "BBB Daily Invoice Run", "isRetired": False},
            ]
        )
        result = tier1(client)["list_schedules"]()
        by_name = {s["name"]: s for s in result["items"]}
        assert "last_run" not in by_name["AAA No Id"]
        assert by_name["BBB Daily Invoice Run"]["last_run"]["status"] == "completed"

    def test_last_run_read_failure_degrades_visibly(self):
        client = MockBPClient()

        def boom(_schedule_ids):
            raise requests.ConnectionError("schedule logs denied")

        client.get_latest_schedule_runs = boom
        result = tier1(client)["list_schedules"]()
        assert result["meta"]["last_run_unavailable"] is True
        assert all("last_run" not in s for s in result["items"])

    def test_last_run_fold_uses_the_sweep_not_per_schedule_reads(self):
        # The fold must go through the one-sweep client read (the N-calls
        # bottleneck fix); a listing that quietly regressed to per-schedule
        # reads would pass every fold assertion above, so pin the call shape.
        client = MockBPClient()
        seen: list = []
        original = client.get_latest_schedule_runs

        def spy(schedule_ids):
            seen.append(list(schedule_ids))
            return original(schedule_ids)

        client.get_latest_schedule_runs = spy
        tier1(client)["list_schedules"]()
        assert len(seen) == 1 and set(seen[0]) == {1, 2}


class TestGetSchedule:
    def test_returns_the_full_definition_by_name_case_insensitive(self):
        detail = tier1()["get_schedule"]("daily invoice run")
        assert detail["name"] == "Daily Invoice Run"
        # The single read's value over the list row: the timing definition.
        assert detail["intervalType"] == "Day"
        assert detail["dailyDetails"] == {"period": 1, "calendarId": 1}
        assert detail["timeZoneId"] == "GMT Standard Time"

    def test_resolves_by_id_too(self):
        assert tier1()["get_schedule"]("2")["name"] == "Weekly Reconciliation"

    def test_unknown_schedule_fails_loudly_with_candidates(self):
        with pytest.raises(ValueError, match="No schedule named"):
            tier1()["get_schedule"]("No Such Schedule")


class TestListScheduleTasks:
    def test_tasks_come_back_in_chain_order_with_sessions_folded(self):
        # Fixture schedule 2 lists its tasks out of chain order (22 before 21);
        # the walk from initialTaskId 21 must reorder them 21 → 22, and each
        # task carries its sessions (what it runs, where).
        result = tier1()["list_schedule_tasks"]("Weekly Reconciliation")
        assert [t["id"] for t in result["items"]] == [21, 22]
        assert result["items"][0]["sessions"] == [
            {"processName": "Invoice Processing", "resourceName": "BOT-02", "taskSessionId": 211}
        ]
        assert "chain order" in result["meta"]["sorted_by"]
        assert "sessions_unavailable" not in result["meta"]

    def test_failure_branch_ranks_after_the_success_path(self):
        # Every id here is chosen to CONTRADICT plain id order (initial task 9,
        # orphan 1), so only the real walk — success path first, failure branch
        # after, unreachable last — produces this sequence; an id-sorted
        # fallback would give [1, 2, 4, 9].
        client = MockBPClient(
            schedules=[{"id": 5, "name": "Branching", "isRetired": False, "initialTaskId": 9}],
            schedule_tasks={
                "5": [
                    {"id": 1, "name": "Orphan", "onSuccessTaskId": None, "onFailureTaskId": None},
                    {"id": 2, "name": "Next", "onSuccessTaskId": None, "onFailureTaskId": None},
                    {"id": 4, "name": "Alert", "onSuccessTaskId": None, "onFailureTaskId": None},
                    {"id": 9, "name": "Main", "onSuccessTaskId": 2, "onFailureTaskId": 4},
                ]
            },
            task_sessions={},
        )
        result = tier1(client)["list_schedule_tasks"]("Branching")
        assert [t["id"] for t in result["items"]] == [9, 2, 4, 1]

    def test_unreachable_tasks_sort_last_not_crash(self):
        # A task no link reaches (orphaned by a broken chain) still shows,
        # after the walkable chain — the orphan's id (0) sorts FIRST under a
        # plain id sort, so only "chain first, orphans after" passes.
        client = MockBPClient(
            schedules=[{"id": 5, "name": "Broken", "isRetired": False, "initialTaskId": 1}],
            schedule_tasks={
                "5": [
                    # Orphans listed in reverse of their id tie-break, so a
                    # stable no-op sort cannot pass by accident.
                    {"id": 3, "name": "Orphan B", "onSuccessTaskId": None, "onFailureTaskId": None},
                    {"id": 1, "name": "Main", "onSuccessTaskId": 99, "onFailureTaskId": None},
                    {"id": 0, "name": "Orphan A", "onSuccessTaskId": None, "onFailureTaskId": None},
                ]
            },
            task_sessions={},
        )
        result = tier1(client)["list_schedule_tasks"]("Broken")
        assert [t["id"] for t in result["items"]] == [1, 0, 3]

    def test_missing_initial_task_id_still_lists_every_task(self):
        # A schedule row without initialTaskId (or one no fixture matches)
        # can't anchor a walk — everything is "unreachable", nothing is lost.
        client = MockBPClient(
            schedules=[{"id": 5, "name": "Anchorless", "isRetired": False}],
            schedule_tasks={"5": [{"id": 2, "name": "B"}, {"id": 1, "name": "A"}]},
            task_sessions={},
        )
        result = tier1(client)["list_schedule_tasks"]("Anchorless")
        assert len(result["items"]) == 2

    def test_sessions_read_failure_degrades_visibly(self):
        client = MockBPClient()

        def boom(_task_id):
            raise requests.ConnectionError("task sessions denied")

        client.get_task_sessions = boom
        result = tier1(client)["list_schedule_tasks"]("Weekly Reconciliation")
        assert result["meta"]["sessions_unavailable"] is True
        assert result["items"] and all("sessions" not in t for t in result["items"])

    def test_unknown_schedule_fails_loudly(self):
        with pytest.raises(ValueError, match="No schedule named"):
            tier1()["list_schedule_tasks"]("No Such Schedule")


class TestListScheduleLogs:
    def test_estate_wide_sweep_newest_first(self):
        result = tier1()["list_schedule_logs"]()
        assert {r["scheduleId"] for r in result["items"]} == {1, 2}
        starts = [r["startTime"] for r in result["items"]]
        assert starts == sorted(starts, reverse=True)
        assert result["meta"]["sorted_by"] == "startTime desc"

    def test_scopes_to_one_schedule_by_name(self):
        result = tier1()["list_schedule_logs"](schedule="weekly reconciliation")
        assert result["items"] and all(r["scheduleId"] == 2 for r in result["items"])

    def test_status_filter_is_canonicalised(self):
        # "terminated" → the Capitalised query enum; rows match either way.
        result = tier1()["list_schedule_logs"](status="terminated")
        assert result["items"] and all(r["status"] == "terminated" for r in result["items"])

    def test_unknown_status_fails_loudly_listing_choices(self):
        with pytest.raises(ValueError, match="PartExceptioned"):
            tier1()["list_schedule_logs"](status="failed")

    def test_window_bounds_are_validated(self):
        with pytest.raises(ValueError, match="ISO"):
            tier1()["list_schedule_logs"](start_date="last tuesday")
        with pytest.raises(ValueError, match="swap the bounds"):
            tier1()["list_schedule_logs"](start_date=_date(0), end_date=_date(5))

    def test_window_narrows_the_runs(self):
        result = tier1()["list_schedule_logs"](start_date=_date(2), end_date=_date(0))
        assert result["items"]
        assert all(r["startTime"][:10] >= _date(2) for r in result["items"])

    def test_end_bound_excludes_newer_runs(self):
        # Both bounds must reach the client: an end date two days back excludes
        # the day-1 run the unbounded read returns.
        result = tier1()["list_schedule_logs"](start_date=_date(30), end_date=_date(2))
        assert result["items"]
        assert all(r["startTime"][:10] <= _date(2) for r in result["items"])

    def test_unknown_schedule_fails_loudly(self):
        with pytest.raises(ValueError, match="No schedule named"):
            tier1()["list_schedule_logs"](schedule="No Such Schedule")


class TestListProcesses:
    def test_alphabetical_by_process_name(self):
        result = tier1()["list_processes"]()
        names = [p["processName"] for p in result["items"]]
        assert names == sorted(names)


class TestListQueueConfigurations:
    def test_maps_queues_to_processes_busiest_first(self):
        client = MockBPClient(
            queue_configurations=[
                {"name": "Quiet", "activeQueueStats": {"activeSessions": 0}},
                {"name": "Busy", "activeQueueStats": {"activeSessions": 4}},
                {"name": "Some", "activeQueueStats": {"activeSessions": 2}},
            ]
        )
        result = tier1(client)["list_queue_configurations"]()
        assert [c["name"] for c in result["items"]] == ["Busy", "Some", "Quiet"]

    def test_carries_the_process_and_resource_group_ids(self):
        config = tier1()["list_queue_configurations"]()["items"][0]
        assert config["activeWorkQueueConfiguration"]["assignedProcessId"]
        assert config["activeWorkQueueConfiguration"]["assignedResourceGroupId"]

    def test_missing_stats_sort_as_zero_not_crash(self):
        client = MockBPClient(queue_configurations=[{"name": "NoStats"}])
        result = tier1(client)["list_queue_configurations"]()
        assert result["items"][0]["name"] == "NoStats"

    def test_degrades_visibly_on_an_older_estate_or_denied_read(self):
        # The endpoint is 7.4+ (the one tool above the 7.2 floor): a 404 on an
        # older estate, or a 403 on a denied read, degrades to an empty envelope
        # with a meta.unavailable note rather than failing the read surface.
        class OldEstate(MockBPClient):
            def get_queue_configurations(self):
                raise requests.HTTPError("404 Not Found")

        result = tier1(OldEstate())["list_queue_configurations"]()
        assert result["items"] == []
        assert result["meta"]["total"] == 0
        assert "7.4" in result["meta"]["unavailable"]


class TestListResourcePools:
    def test_largest_pool_first(self):
        client = MockBPClient(
            resource_pools=[
                {"name": "Small", "members": 1},
                {"name": "Big", "members": 9},
                {"name": "Mid", "members": 4},
            ]
        )
        result = tier1(client)["list_resource_pools"]()
        assert [p["name"] for p in result["items"]] == ["Big", "Mid", "Small"]

    def test_empty_estate_is_an_empty_envelope(self):
        result = tier1(MockBPClient(resource_pools=[]))["list_resource_pools"]()
        assert result["items"] == []
        assert result["meta"]["total"] == 0


class TestListEnvironmentVariables:
    def test_alphabetical_by_name(self):
        result = tier1()["list_environment_variables"]()
        names = [v["name"] for v in result["items"]]
        assert names == sorted(names)

    def test_value_is_scrubbed_type_aware(self):
        # Same fail-closed policy as the queue-item payload, keyed on dataType:
        # Password redacted, Text through the scrubber, scalars kept.
        rows = {
            v["name"]: v
            for v in tier1(scrubber=MarkerScrubber())["list_environment_variables"]()["items"]
        }
        assert rows["Ledger API Key"]["value"] == "[PASSWORD]"
        assert rows["Finance Mailbox"]["value"] == "[SCRUBBED]"
        assert rows["Retry Limit"]["value"] == 3  # Number scalar kept
        # Description is admin-authored free text → scrubbed at the same boundary.
        assert rows["Finance Mailbox"]["description"] == "[SCRUBBED]"

    def test_value_pii_is_actually_removed_end_to_end(self):
        result = tier1(scrubber=RegexScrubber())["list_environment_variables"]()
        blob = json.dumps(result["items"])
        assert "ap-team@contoso.example" not in blob
        assert "07700 900123" not in blob
        assert "s3cr3t-token-value" not in blob

    def test_unknown_and_miscased_types_fail_closed(self):
        client = MockBPClient(
            environment_variables=[
                {"name": "Legacy", "dataType": "text", "value": "leak"},
                {"name": "Secret", "dataType": "password", "value": "hunter2"},
                {"name": "Mystery", "dataType": "Quantum", "value": "leak too"},
                {"name": "Blob", "dataType": "Image", "value": "AAAA"},
                {"name": "Empty", "dataType": "Text", "value": None},
            ]
        )
        rows = {
            v["name"]: v
            for v in tier1(client, MarkerScrubber())["list_environment_variables"]()["items"]
        }
        assert rows["Legacy"]["value"] == "[SCRUBBED]"  # miscased Text still scrubbed
        assert rows["Secret"]["value"] == "[PASSWORD]"  # miscased Password still redacted
        assert rows["Mystery"]["value"] == "[SCRUBBED]"  # unknown type fails closed
        assert rows["Blob"]["value"] == "[IMAGE omitted]"
        assert rows["Empty"]["value"] is None


class TestListProcessGroups:
    def test_folders_first_then_processes_alphabetical(self):
        client = MockBPClient(
            process_groups=[
                {"name": "Zeta Process", "nodeType": "Item"},
                {"name": "Beta Folder", "nodeType": "Group"},
                {"name": "Alpha Process", "nodeType": "Item"},
                {"name": "Yankee Folder", "nodeType": "Group"},
            ]
        )
        result = tier1(client)["list_process_groups"]()
        assert [n["name"] for n in result["items"]] == [
            "Beta Folder",
            "Yankee Folder",
            "Alpha Process",
            "Zeta Process",
        ]


# --- Tier 2 -----------------------------------------------------------------------


class TestExceptionSummary:
    def test_groups_by_reason_with_counts(self):
        result = tier2()["exception_summary"]("Invoices", **WINDOW)
        assert result["meta"]["sorted_by"] == "count desc"
        [group] = result["items"]
        assert group["count"] == 1
        assert group["reason"] == "Invoice total did not match purchase order"
        assert group["resources"] == ["BOT-01"]

    def test_requires_the_window(self):
        with pytest.raises(ValueError, match="required"):
            tier2()["exception_summary"]("Invoices", None, None)

    def test_groups_after_scrubbing_folding_pii_variants(self):
        # Two reasons that differ only in personal data scrub to the same
        # text and must land in ONE bucket — counts reflect failure modes,
        # not distinct customers.
        client = MockBPClient(
            queues=[{"id": "q", "name": "Q"}],
            queue_items=[
                {
                    "queue": "q",
                    "id": "i1",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02T10:00:00Z",
                    "exceptionedDate": "2026-03-02T10:00:00Z",
                    "exceptionReason": "No record for John Smith",
                    "resource": "BOT-01",
                },
                {
                    "queue": "q",
                    "id": "i2",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-04T10:00:00Z",
                    "exceptionedDate": "2026-03-04T10:00:00Z",
                    "exceptionReason": "No record for Jane Jones",
                    "resource": "BOT-02",
                },
            ],
        )
        result = tier2(client, MarkerScrubber())["exception_summary"]("Q", **WINDOW)
        [group] = result["items"]
        assert group["count"] == 2
        assert group["first_seen"] == "2026-03-02T10:00:00Z"
        assert group["last_seen"] == "2026-03-04T10:00:00Z"
        assert group["resources"] == ["BOT-01", "BOT-02"]

    def test_missing_reason_gets_an_explicit_bucket(self):
        client = MockBPClient(
            queues=[{"id": "q", "name": "Q"}],
            queue_items=[
                {"queue": "q", "id": "i1", "state": "Exceptioned", "lastUpdated": "2026-03-02"},
            ],
        )
        result = tier2(client)["exception_summary"]("Q", **WINDOW)
        [group] = result["items"]
        assert group["reason"] == "(no reason recorded)"
        # No exceptionedDate on the row → the timestamps fall back to
        # lastUpdated rather than vanishing.
        assert group["first_seen"] == "2026-03-02"
        assert group["last_seen"] == "2026-03-02"

    def test_most_frequent_reason_first(self):
        items = [
            {
                "queue": "q",
                "id": f"i{n}",
                "state": "Exceptioned",
                "lastUpdated": "2026-03-02",
                "exceptionReason": reason,
            }
            for n, reason in enumerate(["Twice", "Twice", "Once"])
        ]
        client = MockBPClient(queues=[{"id": "q", "name": "Q"}], queue_items=items)
        result = tier2(client)["exception_summary"]("Q", **WINDOW)
        assert [(g["reason"], g["count"]) for g in result["items"]] == [
            ("Twice", 2),
            ("Once", 1),
        ]

    def test_window_bounds_are_forwarded_to_the_client(self):
        # The fixture exception sits on 03-02; a window on either side of it
        # must come back empty — each bound has to actually reach the client.
        summary = tier2()["exception_summary"]
        assert summary("Invoices", "2026-03-03", "2026-03-31")["items"] == []
        assert summary("Invoices", "2026-03-01", "2026-03-01")["items"] == []

    def test_single_queue_summary_has_no_queues_field(self):
        # The per-queue summary keeps its original row shape (no `queues`).
        result = tier2()["exception_summary"]("Invoices", **WINDOW)
        assert result["items"] and all("queues" not in g for g in result["items"])


class TestEstateExceptionSummary:
    def test_groups_one_reason_across_queues(self):
        client = MockBPClient(
            queues=[{"id": "qa", "name": "Alpha"}, {"id": "qb", "name": "Bravo"}],
            queue_items=[
                {
                    "queue": "qa",
                    "id": "i1",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": "Timeout",
                    "resource": "BOT-01",
                },
                {
                    "queue": "qb",
                    "id": "i2",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-03",
                    "exceptionReason": "Timeout",
                    "resource": "BOT-02",
                },
            ],
        )
        result = tier2(client)["estate_exception_summary"](**WINDOW)
        [group] = result["items"]
        assert group["reason"] == "Timeout"
        assert group["count"] == 2
        assert group["queues"] == ["Alpha", "Bravo"]
        assert group["resources"] == ["BOT-01", "BOT-02"]

    def test_ranked_most_frequent_first_across_the_estate(self):
        client = MockBPClient(
            queues=[{"id": "qa", "name": "Alpha"}, {"id": "qb", "name": "Bravo"}],
            queue_items=[
                {
                    "queue": "qa",
                    "id": "i1",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": "Rare",
                },
                {
                    "queue": "qa",
                    "id": "i2",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": "Common",
                },
                {
                    "queue": "qb",
                    "id": "i3",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": "Common",
                },
            ],
        )
        result = tier2(client)["estate_exception_summary"](**WINDOW)
        assert [(g["reason"], g["count"]) for g in result["items"]] == [
            ("Common", 2),
            ("Rare", 1),
        ]

    def test_reasons_grouped_after_scrubbing(self):
        client = MockBPClient(
            queues=[{"id": "qa", "name": "Alpha"}, {"id": "qb", "name": "Bravo"}],
            queue_items=[
                {
                    "queue": "qa",
                    "id": "i1",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": "anything",
                },
                {
                    "queue": "qb",
                    "id": "i2",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": "different",
                },
            ],
        )
        result = tier2(client, MarkerScrubber())["estate_exception_summary"](**WINDOW)
        # Both reasons scrub to the same marker → one bucket spanning both queues.
        [group] = result["items"]
        assert group["count"] == 2
        assert group["queues"] == ["Alpha", "Bravo"]

    def test_only_exceptioned_items_are_counted(self):
        # The scan filters each queue to its Exceptioned items — a Completed
        # item in the same window must not inflate any reason.
        client = MockBPClient(
            queues=[{"id": "qa", "name": "Alpha"}],
            queue_items=[
                {
                    "queue": "qa",
                    "id": "i1",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": "Timeout",
                },
                {
                    "queue": "qa",
                    "id": "i2",
                    "state": "Completed",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": None,
                },
            ],
        )
        result = tier2(client)["estate_exception_summary"](**WINDOW)
        [group] = result["items"]
        assert group["reason"] == "Timeout"
        assert group["count"] == 1

    def test_window_is_required(self):
        with pytest.raises(ValueError):
            tier2()["estate_exception_summary"](None, None)

    def test_queue_without_an_id_is_skipped(self):
        # A queue row carrying no id can't be drilled for items — it is skipped
        # rather than crashing the estate-wide scan.
        client = MockBPClient(
            queues=[{"name": "NoId"}, {"id": "qb", "name": "Bravo"}],
            queue_items=[
                {
                    "queue": "qb",
                    "id": "i1",
                    "state": "Exceptioned",
                    "lastUpdated": "2026-03-02",
                    "exceptionReason": "Timeout",
                },
            ],
        )
        result = tier2(client)["estate_exception_summary"](**WINDOW)
        [group] = result["items"]
        assert group["queues"] == ["Bravo"]


class TestThroughputSummary:
    def test_per_process_outcome_counts(self):
        result = tier2()["throughput_summary"](**WINDOW)
        rows = {r["process"]: r for r in result["items"]}
        invoices = rows["Invoice Processing"]
        assert invoices["total_sessions"] == 2
        assert invoices["completed"] == 2
        assert invoices["completion_rate_pct"] == 100.0
        onboarding = rows["Customer Onboarding"]
        assert onboarding["terminated"] == 1
        assert onboarding["completion_rate_pct"] == 0.0
        assert onboarding["terminated_process_errors"] == 1
        assert onboarding["terminated_internal_errors"] == 0

    def test_busiest_first(self):
        result = tier2()["throughput_summary"](**WINDOW)
        totals = [r["total_sessions"] for r in result["items"]]
        assert totals == sorted(totals, reverse=True)

    def test_scopes_to_one_process(self):
        result = tier2()["throughput_summary"](**WINDOW, process="customer onboarding")
        assert [r["process"] for r in result["items"]] == ["Customer Onboarding"]

    def test_window_bounds_are_forwarded_to_the_client(self):
        # Fixture sessions start 8, 7, and 4 days back; a window spanning days
        # 7..5 must keep only the middle one.
        result = tier2()["throughput_summary"](_date(7), _date(5))
        rows = {r["process"]: r["total_sessions"] for r in result["items"]}
        assert rows == {"Customer Onboarding": 1}

    def test_outcome_counts_over_a_mixed_bag(self):
        # One of each outcome: the completion rate divides by FINISHED runs
        # (completed + terminated + stopped), the still-running one lands in
        # `other`, and the termination cause is split out.
        client = MockBPClient(
            sessions=[
                {"processName": "P", "status": "Completed", "startTime": "2026-03-02T09:00:00Z"},
                {
                    "processName": "P",
                    "status": "Terminated",
                    "terminationReason": "InternalError",
                    "startTime": "2026-03-02T10:00:00Z",
                },
                {"processName": "P", "status": "Stopped", "startTime": "2026-03-02T11:00:00Z"},
                {"processName": "P", "status": "Running", "startTime": "2026-03-02T12:00:00Z"},
            ]
        )
        [row] = tier2(client)["throughput_summary"](**WINDOW)["items"]
        assert row["total_sessions"] == 4
        assert (row["completed"], row["terminated"], row["stopped"], row["other"]) == (1, 1, 1, 1)
        assert row["completion_rate_pct"] == 33.3  # 1 of 3 finished, one decimal
        assert row["terminated_internal_errors"] == 1
        assert row["terminated_process_errors"] == 0

    def test_requires_the_window(self):
        with pytest.raises(ValueError, match="required"):
            tier2()["throughput_summary"](None, None)

    def test_unfinished_runs_have_no_completion_rate(self):
        # All sessions still Running → nothing has finished; a rate of 0
        # would read as "everything failed", so it must be None.
        client = MockBPClient(
            sessions=[
                {"processName": "P", "status": "Running", "startTime": "2026-03-02T09:00:00Z"},
            ]
        )
        [row] = tier2(client)["throughput_summary"](**WINDOW)["items"]
        assert row["completion_rate_pct"] is None
        assert row["other"] == 1

    def test_a_session_without_a_process_name_is_grouped_as_unknown(self):
        client = MockBPClient(
            sessions=[{"status": "Completed", "startTime": "2026-03-02T09:00:00Z"}]
        )
        [row] = tier2(client)["throughput_summary"](**WINDOW)["items"]
        assert row["process"] == "(unknown)"

    def test_an_empty_window_yields_an_empty_envelope(self):
        result = tier2()["throughput_summary"]("2001-01-01", "2001-01-02")
        assert result["items"] == []
        assert result["meta"]["total"] == 0


class TestEstateHealth:
    def test_rolls_up_worker_status_and_licence(self):
        result = tier2()["estate_health"]()
        assert result["workers_total"] == 3
        assert result["workers_by_status"] == {"Idle": 1, "Working": 1, "Offline": 1}
        assert [w["name"] for w in result["workers_requiring_attention"]] == ["BOT-03"]
        assert result["attention_meta"]["total"] == 1
        assert result["license_usage"]["concurrentSessionsLimit"] == 10

    def test_attention_list_is_most_urgent_first_and_capped(self):
        client = MockBPClient(
            resources=[
                {"name": "B-WARN", "displayStatus": "Warning"},
                {"name": "B-MISS", "displayStatus": "Missing"},
                {"name": "B-OFF", "displayStatus": "Offline"},
                {"name": "B-OK", "displayStatus": "Idle"},
            ]
        )
        result = tier2(client)["estate_health"](limit=2)
        assert [w["name"] for w in result["workers_requiring_attention"]] == [
            "B-MISS",
            "B-OFF",
        ]
        assert result["attention_meta"]["total"] == 3
        assert result["attention_meta"]["truncated"] is True

    @pytest.mark.parametrize(
        "error",
        [
            requests.HTTPError("403 Forbidden"),
            requests.Timeout("read timed out"),
            requests.ConnectionError("connection refused"),
        ],
        ids=["denied", "timeout", "connection"],
    )
    def test_licence_read_failure_degrades_visibly_not_fatally(self, error):
        # A denied, timed-out, or dropped licence read keeps worker health;
        # the licence block says why it is missing. Transport errors are just
        # as likely as a 403 on this one extra request.
        class NoLicenceClient(MockBPClient):
            def get_current_limits_and_usage(self):
                raise error

        result = tier2(NoLicenceClient())["estate_health"]()
        assert result["workers_total"] == 3
        note = result["license_usage"]["unavailable"]
        assert note.startswith("licence read failed:")
        assert str(error) in note

    def test_a_worker_without_a_status_is_counted_as_unknown(self):
        client = MockBPClient(resources=[{"name": "B-1"}])
        result = tier2(client)["estate_health"]()
        assert result["workers_by_status"] == {"(unknown)": 1}

    def test_an_empty_estate_reports_zeroes_not_errors(self):
        result = tier2(MockBPClient(resources=[]))["estate_health"]()
        assert result["workers_total"] == 0
        assert result["workers_by_status"] == {}
        assert result["workers_requiring_attention"] == []
        assert result["attention_meta"]["truncated"] is False


class TestLicenseEntitlement:
    def test_reshapes_entitlement_into_readable_tiers(self):
        result = tier2()["license_entitlement"]()
        assert result["active_license_types"] == ["Enterprise"]
        assert result["enterprise"] == {
            "published_processes_limit": 0,
            "concurrent_sessions_limit": 10,
            "runtime_resources_limit": 5,
            "process_alert_machines_limit": 0,
        }
        assert result["desktop"]["concurrent_sessions_limit"] == 0

    def test_missing_tier_yields_nulls_not_an_error(self):
        client = MockBPClient(license_entitlement={"activeLicenseTypes": ["Desktop"]})
        result = tier2(client)["license_entitlement"]()
        assert result["active_license_types"] == ["Desktop"]
        assert result["enterprise"] == {
            "published_processes_limit": None,
            "concurrent_sessions_limit": None,
            "runtime_resources_limit": None,
            "process_alert_machines_limit": None,
        }

    @pytest.mark.parametrize(
        "error",
        [
            requests.HTTPError("403 Forbidden"),
            requests.Timeout("read timed out"),
            requests.ConnectionError("connection refused"),
        ],
        ids=["denied", "timeout", "connection"],
    )
    def test_read_failure_degrades_visibly_not_fatally(self, error):
        # The "System - License" permission may be withheld; a denied or dropped
        # read returns an unavailable note rather than erroring the tool.
        class NoEntitlementClient(MockBPClient):
            def get_license_entitlement(self):
                raise error

        result = tier2(NoEntitlementClient())["license_entitlement"]()
        assert result["unavailable"].startswith("licence entitlement read failed:")
        assert str(error) in result["unavailable"]


class TestResourceUtilization:
    def test_a_row_with_a_malformed_date_is_skipped_not_a_crash(self):
        client = MockBPClient(
            resource_utilization=[
                {
                    "resourceId": "r1",
                    "digitalWorkerName": "BOT-BAD",
                    "utilizationDate": "not-a-date",
                    "usages": [10] * 24,
                },
                {
                    "resourceId": "r2",
                    "digitalWorkerName": "BOT-MISSING",
                    "utilizationDate": None,
                    "usages": [10] * 24,
                },
                {
                    "resourceId": "r3",
                    "digitalWorkerName": "BOT-OK",
                    "utilizationDate": _date(0),
                    "usages": [10] * 24,
                },
            ]
        )
        result = tier2(client)["resource_utilization"](start_date=_date(0), end_date=_date(0))
        assert [w["worker"] for w in result["workers"]] == ["BOT-OK"]

    def test_aggregates_daily_and_windowed_per_worker(self):
        result = tier2()["resource_utilization"](start_date=_date(2), end_date=_date(0))
        workers = {w["worker"]: w for w in result["workers"]}
        assert set(workers) == {"BOT-01", "BOT-02"}

        bot01 = workers["BOT-01"]
        assert [d["date"] for d in bot01["daily"]] == [_date(2), _date(1), _date(0)]
        assert [d["worked_minutes"] for d in bot01["daily"]] == [495, 495, 220]
        assert bot01["daily"][0]["wall_clock_minutes"] == 1440
        assert bot01["daily"][0]["utilization_pct"] == 34.4
        assert bot01["window_worked_minutes"] == 1210
        assert bot01["window_wall_clock_minutes"] == 3 * 1440
        assert bot01["window_utilization_pct"] == 28.0

        # BOT-02 has no row on the most recent day — its window denominator
        # still counts the full 3-day window (an idle day is 0%, not excluded).
        bot02 = workers["BOT-02"]
        assert len(bot02["daily"]) == 2
        assert bot02["window_worked_minutes"] == 720
        assert bot02["window_wall_clock_minutes"] == 3 * 1440
        assert bot02["window_utilization_pct"] == 16.7

    def test_ranked_highest_windowed_utilization_first(self):
        result = tier2()["resource_utilization"](start_date=_date(2), end_date=_date(0))
        assert [w["worker"] for w in result["workers"]] == ["BOT-01", "BOT-02"]
        assert result["workers_meta"]["sorted_by"] == "window_utilization_pct desc"

    def test_estate_rollup_is_total_worked_over_total_wall_clock(self):
        result = tier2()["resource_utilization"](start_date=_date(2), end_date=_date(0))
        assert result["estate_worked_minutes"] == 1210 + 720
        assert result["estate_wall_clock_minutes"] == 2 * 3 * 1440
        assert result["estate_utilization_pct"] == 22.3

    def test_a_worker_absent_from_the_window_is_absent_from_the_result(self):
        # Only today is in the window; BOT-02 has no row for today at all.
        result = tier2()["resource_utilization"](start_date=_date(0), end_date=_date(0))
        assert [w["worker"] for w in result["workers"]] == ["BOT-01"]
        assert result["estate_wall_clock_minutes"] == 1440

    def test_an_estate_with_no_data_in_window_reports_none_not_a_crash(self):
        result = tier2()["resource_utilization"](start_date=_date(365), end_date=_date(365))
        assert result["workers"] == []
        assert result["estate_worked_minutes"] == 0
        assert result["estate_wall_clock_minutes"] == 0
        assert result["estate_utilization_pct"] is None

    def test_missing_window_fails_loudly(self):
        with pytest.raises(ValueError):
            tier2()["resource_utilization"](start_date=None, end_date=_date(0))

    def test_workers_list_is_capped_by_limit(self):
        result = tier2()["resource_utilization"](start_date=_date(2), end_date=_date(0), limit=1)
        assert len(result["workers"]) == 1
        assert result["workers_meta"]["truncated"] is True
        assert result["workers_meta"]["total"] == 2

    @pytest.mark.parametrize(
        "error",
        [
            requests.HTTPError("403 Forbidden"),
            requests.Timeout("read timed out"),
            requests.ConnectionError("connection refused"),
        ],
        ids=["denied", "timeout", "connection"],
    )
    def test_read_failure_degrades_visibly_not_fatally(self, error):
        class NoUtilizationClient(MockBPClient):
            def get_resource_utilization(self, start_date):
                raise error

        result = tier2(NoUtilizationClient())["resource_utilization"](
            start_date=_date(2), end_date=_date(0)
        )
        assert result["workers"] == []
        assert result["estate_worked_minutes"] is None
        assert result["estate_utilization_pct"] is None
        assert result["unavailable"].startswith("resource utilization read failed:")
        assert str(error) in result["unavailable"]


# --- Registration ------------------------------------------------------------------


class FakeApp:
    """Duck-typed FastMCP: captures what register_tools would expose."""

    def __init__(self):
        self.registered = []

    def tool(self):
        def decorator(fn):
            self.registered.append(fn)
            return fn

        return decorator


class TestRegisterTools:
    def test_registers_all_read_tools_by_name(self):
        app = FakeApp()
        names = register_tools(app, MockBPClient(), NullScrubber(), config=BPConfig())
        assert names == [
            "list_queues",
            "get_queue",
            "list_queue_items",
            "get_queue_item",
            "list_item_attempts",
            "list_sessions",
            "get_session",
            "get_session_log",
            "list_resources",
            "list_schedules",
            "get_schedule",
            "list_schedule_tasks",
            "list_schedule_logs",
            "list_processes",
            "list_queue_configurations",
            "list_resource_pools",
            "list_environment_variables",
            "list_process_groups",
            "exception_summary",
            "estate_exception_summary",
            "throughput_summary",
            "estate_health",
            "license_entitlement",
            "resource_utilization",
        ]
        assert [fn.__name__ for fn in app.registered] == names

    def test_every_tool_carries_a_description(self):
        # The docstring IS the tool description an LLM client selects on.
        app = FakeApp()
        register_tools(app, MockBPClient(), NullScrubber(), config=BPConfig())
        for fn in app.registered:
            assert fn.__doc__ and len(fn.__doc__.strip()) > 80, fn.__name__

    def test_default_limit_is_fifty(self):
        assert DEFAULT_LIMIT == 50


# --- validate_data_value (refactored out of validate_session_parameters) --------


class TestValidateDataValue:
    def test_canonicalises_value_type_case(self):
        result = validate_data_value("X", {"valueType": "text", "value": "hello"})
        assert result == {"valueType": "Text", "value": "hello"}

    def test_rejects_missing_value_type(self):
        with pytest.raises(ValueError, match="valueType"):
            validate_data_value("X", {"value": "hello"})

    def test_rejects_missing_value(self):
        with pytest.raises(ValueError, match="valueType"):
            validate_data_value("X", {"valueType": "Text"})

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError, match="X"):
            validate_data_value("X", "just-a-string")

    def test_validates_additional_parameters(self):
        result = validate_data_value(
            "Doc", {"valueType": "Collection", "value": [], "additionalParameters": ["A"]}
        )
        assert result["additionalParameters"] == ["A"]

    def test_rejects_non_string_additional_parameters(self):
        with pytest.raises(ValueError, match="additionalParameters"):
            validate_data_value(
                "Doc", {"valueType": "Text", "value": "", "additionalParameters": [1]}
            )

    def test_collection_value_validates_nested_rows(self):
        nested = {"rows": [{"Amount": {"valueType": "Number", "value": 42}}]}
        result = validate_data_value("Data", {"valueType": "Collection", "value": nested})
        assert result["valueType"] == "Collection"

    def test_collection_rejects_invalid_nested_value_type(self):
        nested = {"rows": [{"X": {"valueType": "Spreadsheet", "value": "y"}}]}
        with pytest.raises(ValueError, match="Spreadsheet"):
            validate_data_value("Data", {"valueType": "Collection", "value": nested})

    def test_collection_with_no_rows_key_passes(self):
        result = validate_data_value("Data", {"valueType": "Collection", "value": {"schema": "v1"}})
        assert result["valueType"] == "Collection"

    def test_collection_rows_must_be_a_list(self):
        with pytest.raises(ValueError, match="rows.*must be a list"):
            validate_data_value("D", {"valueType": "Collection", "value": {"rows": "bad"}})

    def test_collection_row_must_be_an_object(self):
        with pytest.raises(ValueError, match="rows\\[0\\].*must be an object"):
            validate_data_value("D", {"valueType": "Collection", "value": {"rows": [42]}})


# --- validate_queue_items -------------------------------------------------------


class TestValidateQueueItems:
    def test_accepts_a_minimal_item(self):
        result = validate_queue_items([{}])
        assert result == [{}]

    def test_rejects_empty_list(self):
        with pytest.raises(ValueError, match="non-empty list"):
            validate_queue_items([])

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="non-empty list"):
            validate_queue_items("not a list")

    def test_rejects_non_dict_item(self):
        with pytest.raises(ValueError, match="items\\[0\\] must be an object"):
            validate_queue_items(["string"])

    def test_rejects_unknown_key_naming_it(self):
        with pytest.raises(ValueError, match="defferedDate"):
            validate_queue_items([{"defferedDate": "2026-01-01"}])

    def test_unknown_key_error_names_index(self):
        with pytest.raises(ValueError, match="items\\[1\\]"):
            validate_queue_items([{}, {"badKey": 1}])

    def test_validates_deferred_date_as_iso(self):
        with pytest.raises(ValueError, match="deferredDate"):
            validate_queue_items([{"deferredDate": "next tuesday"}])

    def test_accepts_valid_deferred_date(self):
        result = validate_queue_items([{"deferredDate": "2026-04-01T09:00:00"}])
        assert result[0]["deferredDate"] == "2026-04-01T09:00:00"

    def test_priority_must_be_int(self):
        with pytest.raises(ValueError, match="priority"):
            validate_queue_items([{"priority": "high"}])

    def test_priority_rejects_bool(self):
        with pytest.raises(ValueError, match="priority"):
            validate_queue_items([{"priority": True}])

    def test_sla_accepts_none(self):
        result = validate_queue_items([{"sla": None}])
        assert result[0]["sla"] is None

    def test_sla_must_be_int_or_none(self):
        with pytest.raises(ValueError, match="sla"):
            validate_queue_items([{"sla": "60"}])

    def test_tags_must_be_string_array(self):
        with pytest.raises(ValueError, match="tags"):
            validate_queue_items([{"tags": [1, 2]}])

    def test_is_suggested_must_be_bool(self):
        with pytest.raises(ValueError, match="isSuggested"):
            validate_queue_items([{"isSuggested": "yes"}])

    def test_status_must_be_string(self):
        with pytest.raises(ValueError, match="status"):
            validate_queue_items([{"status": 123}])

    def test_process_name_must_be_string(self):
        with pytest.raises(ValueError, match="processName"):
            validate_queue_items([{"processName": 42}])

    def test_data_collection_validates_rows(self):
        with pytest.raises(ValueError, match="valueType"):
            validate_queue_items([{"data": {"rows": [{"X": {"valueType": "Bad", "value": ""}}]}}])

    def test_data_must_be_object(self):
        with pytest.raises(ValueError, match="data must be an object"):
            validate_queue_items([{"data": "not-an-object"}])

    def test_data_with_no_rows_key_is_accepted(self):
        result = validate_queue_items([{"data": {"schema": "v1"}}])
        assert result[0]["data"] == {"schema": "v1"}

    def test_data_rows_must_be_a_list(self):
        with pytest.raises(ValueError, match="rows must be a list"):
            validate_queue_items([{"data": {"rows": "not-a-list"}}])

    def test_data_row_must_be_an_object(self):
        with pytest.raises(ValueError, match="rows\\[0\\] must be an object"):
            validate_queue_items([{"data": {"rows": ["string"]}}])

    def test_full_item_round_trips(self):
        item = {
            "data": {"rows": [{"Ref": {"valueType": "Text", "value": "INV-1"}}]},
            "deferredDate": "2026-06-01T10:00:00",
            "priority": 2,
            "tags": ["urgent"],
            "status": "New",
            "sla": 30,
            "processName": "Invoice Processing",
            "isSuggested": False,
        }
        result = validate_queue_items([item])
        assert result[0] == item

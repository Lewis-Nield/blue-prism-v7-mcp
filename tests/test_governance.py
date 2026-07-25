"""Tests for the governance layer + Tier 3 tools (Phase 5).

The contract pinned here: capability resolution over the documented permission
names (CNF clauses, case-insensitive), the fail-loud audit log (no path, no
actions), dry-run-by-default action tools whose attempt line lands before the
write, and register_tools gating Tier 3 behind enable_actions + capabilities.
All tool tests run over MockBPClient, whose writes mutate the in-memory
fixtures — so "the dry run changed nothing" is a real read-back assertion.
"""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from blue_prism_v7_mcp.config import BPConfig
from blue_prism_v7_mcp.governance import (
    TOOL_PERMISSIONS,
    UNRETIRE_EXTRA_PERMISSION,
    AuditLog,
    audit_detail,
    bind_actor,
    build_audit_log,
    holds,
    resolve_capabilities,
    unsatisfied_clauses,
    validate_actor,
)
from blue_prism_v7_mcp.mock import _DEFAULT_PERMISSIONS, MockBPClient, _date
from blue_prism_v7_mcp.pii import NullScrubber
from blue_prism_v7_mcp.tools import build_tier3_tools, register_tools
from blue_prism_v7_mcp.tools.common import validate_uuid

ALL_ACTION_TOOLS = {
    "retry_queue_item",
    "defer_queue_item",
    "create_queue_items",
    "start_process",
    "stop_session",
    "set_schedule_enabled",
    "trigger_schedule",
    "stop_schedule",
}

QUEUE_ONLY = ["Full Access to Queue Management"]


def make_audit(tmp_path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


def read_audit(audit: AuditLog) -> list[dict]:
    return [json.loads(line) for line in audit.path.read_text().splitlines()]


def tier3(
    tmp_path, client=None, permissions=None, audit=None
) -> tuple[dict, AuditLog, MockBPClient]:
    client = client or MockBPClient()
    audit = audit or make_audit(tmp_path)
    perms = permissions if permissions is not None else list(_DEFAULT_PERMISSIONS)
    tools, _ = build_tier3_tools(client, audit=audit, permissions=perms)
    return {t.__name__: t for t in tools}, audit, client


def flaky_audit(tmp_path, fail_on: str) -> AuditLog:
    """An AuditLog whose write starts failing at a chosen lifecycle status —
    the disk filling up (or rotation snatching the file) mid-action."""

    class FlakyAudit(AuditLog):
        def record(self, tool, args, status, detail=None):
            if status == fail_on:
                raise OSError("disk full")
            super().record(tool, args, status, detail)

    return FlakyAudit(tmp_path / "audit.jsonl")


# --- Capability resolution ------------------------------------------------------


class TestResolveCapabilities:
    def test_full_permissions_allow_every_action_tool(self):
        assert resolve_capabilities(_DEFAULT_PERMISSIONS) == ALL_ACTION_TOOLS

    def test_queue_permission_alone_allows_only_the_queue_tools(self):
        assert resolve_capabilities(QUEUE_ONLY) == {
            "retry_queue_item",
            "defer_queue_item",
            "create_queue_items",
        }

    def test_no_permissions_allow_nothing(self):
        assert resolve_capabilities([]) == set()

    @pytest.mark.parametrize("process_perm", ["Create Process", "Edit Process", "Execute Process"])
    def test_start_process_accepts_any_process_permission_with_control_resource(self, process_perm):
        assert "start_process" in resolve_capabilities([process_perm, "Control Resource"])

    def test_start_process_needs_control_resource_too(self):
        # Both clauses must hold: a process permission alone is not enough.
        assert "start_process" not in resolve_capabilities(["Execute Process"])
        assert "start_process" not in resolve_capabilities(["Control Resource"])

    def test_set_schedule_enabled_needs_edit_and_retire(self):
        assert "set_schedule_enabled" not in resolve_capabilities(["Edit Schedule"])
        assert "set_schedule_enabled" in resolve_capabilities(["Edit Schedule", "Retire Schedule"])

    def test_trigger_schedule_needs_only_edit(self):
        assert "trigger_schedule" in resolve_capabilities(["Edit Schedule"])

    def test_matching_is_case_insensitive_and_whitespace_tolerant(self):
        # The spec carries these names only as prose, so the live strings may
        # differ in case; the resolver must not withhold a tool over casing.
        assert resolve_capabilities(["  full access to QUEUE management  "]) == {
            "retry_queue_item",
            "defer_queue_item",
            "create_queue_items",
        }

    def test_unknown_permissions_are_ignored(self):
        assert resolve_capabilities(["System Manager", "Audit Viewer"]) == set()


class TestUnsatisfiedClauses:
    def test_satisfied_tool_has_no_unsatisfied_clauses(self):
        assert unsatisfied_clauses("retry_queue_item", QUEUE_ONLY) == []

    def test_failed_clauses_render_their_alternatives(self):
        clauses = unsatisfied_clauses("start_process", [])
        assert clauses == [
            "Create Process | Edit Process | Execute Process",
            "Control Resource",
        ]

    def test_only_the_failed_clause_is_reported(self):
        assert unsatisfied_clauses("start_process", ["Execute Process"]) == ["Control Resource"]


class TestHolds:
    def test_case_insensitive(self):
        assert holds(["create SCHEDULE"], "Create Schedule")

    def test_missing(self):
        assert not holds(["Edit Schedule"], "Create Schedule")

    def test_unretire_extra_is_the_documented_create_permission(self):
        # Pinned: PUT /schedules/{id} documents Create Schedule for unretiring.
        assert UNRETIRE_EXTRA_PERMISSION == "Create Schedule"


# --- Audit log -------------------------------------------------------------------


class TestAuditLog:
    def test_records_one_json_line_per_event(self, tmp_path):
        audit = make_audit(tmp_path)
        audit.record("retry_queue_item", {"queue_id": "q-1"}, status="dry_run")
        audit.record("retry_queue_item", {"queue_id": "q-1"}, status="attempt")
        lines = read_audit(audit)
        assert [e["status"] for e in lines] == ["dry_run", "attempt"]
        assert lines[0]["tool"] == "retry_queue_item"
        assert lines[0]["args"] == {"queue_id": "q-1"}
        assert lines[0]["ts"].endswith("+00:00")  # UTC, never server-local

    def test_detail_appears_only_when_given(self, tmp_path):
        audit = make_audit(tmp_path)
        audit.record("t", {}, status="error", detail="boom")
        audit.record("t", {}, status="success")
        error_line, success_line = read_audit(audit)
        assert error_line["detail"] == "boom"
        assert "detail" not in success_line

    def test_creates_parent_directories(self, tmp_path):
        audit = AuditLog(tmp_path / "deep" / "nested" / "audit.jsonl")
        audit.record("t", {}, status="dry_run")
        assert len(read_audit(audit)) == 1

    def test_new_audit_file_is_owner_only(self, tmp_path):
        audit = AuditLog(tmp_path / "audit.jsonl")
        assert audit.path.stat().st_mode & 0o777 == 0o600

    def test_pre_existing_world_readable_file_is_tightened(self, tmp_path):
        # The branch `touch(mode=...)` alone cannot reach: touch applies its mode
        # only on creation, so every already-deployed 0644 log would stay 0644.
        existing = tmp_path / "audit.jsonl"
        existing.write_text('{"ts": "old"}\n', encoding="utf-8")
        existing.chmod(0o644)

        audit = AuditLog(existing)

        assert audit.path.stat().st_mode & 0o777 == 0o600
        assert audit.path.read_text(encoding="utf-8") == '{"ts": "old"}\n'  # append-only

    def test_unwritable_path_fails_at_construction_not_first_action(self, tmp_path):
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("")
        with pytest.raises(OSError):
            AuditLog(blocker / "audit.jsonl")

    def test_non_json_native_args_are_stringified(self, tmp_path):
        # Tool args are ids/names/dates today, but a future arg must never be
        # able to crash the audit write mid-action.
        audit = make_audit(tmp_path)
        audit.record("t", {"when": object()}, status="dry_run")
        assert "when" in read_audit(audit)[0]["args"]


class TestBuildAuditLog:
    def test_blank_path_fails_loud_naming_the_env_var(self, tmp_path):
        with pytest.raises(ValueError, match="BP_AUDIT_LOG_PATH"):
            build_audit_log(BPConfig(enable_actions=True))

    @pytest.mark.parametrize("path", ["", "   "])
    def test_whitespace_only_path_is_blank(self, path):
        with pytest.raises(ValueError, match="audit"):
            build_audit_log(BPConfig(enable_actions=True, audit_log_path=path))

    def test_valid_path_opens_the_log(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        audit = build_audit_log(BPConfig(enable_actions=True, audit_log_path=str(path)))
        assert audit.path == path
        assert path.exists()  # touched at startup, not on first action


# --- Tier 3 tool builder ----------------------------------------------------------


class TestBuildTier3Tools:
    def test_full_permissions_build_all_action_tools_in_order(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        assert list(tools) == [
            "retry_queue_item",
            "defer_queue_item",
            "create_queue_items",
            "start_process",
            "stop_session",
            "set_schedule_enabled",
            "trigger_schedule",
            "stop_schedule",
        ]

    def test_narrow_permissions_build_only_allowed_tools(self, tmp_path):
        tools, _, _ = tier3(tmp_path, permissions=QUEUE_ONLY)
        assert set(tools) == {"retry_queue_item", "defer_queue_item", "create_queue_items"}

    def test_every_action_tool_warns_about_dry_run_in_its_description(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        for name, fn in tools.items():
            assert "DRY RUN" in fn.__doc__, name
            assert "dry_run=false" in fn.__doc__, name

    def test_the_underdocumented_writes_carry_loud_caveats(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        assert "CAUTION" in tools["defer_queue_item"].__doc__
        assert "CAUTION" in tools["set_schedule_enabled"].__doc__
        assert "CAUTION" not in tools["retry_queue_item"].__doc__

    def test_permission_map_covers_exactly_the_built_tools(self):
        assert set(TOOL_PERMISSIONS) == ALL_ACTION_TOOLS

    def test_every_action_tool_defaults_to_dry_run(self, tmp_path):
        # Structural, not per-tool: a future action tool that forgets the
        # dry-run default fails here, not in a post-incident review.
        tools, _, _ = tier3(tmp_path)
        for name, fn in tools.items():
            assert inspect.signature(fn).parameters["dry_run"].default is True, name

    def test_the_withheld_map_names_each_missing_clause(self, tmp_path):
        # The factory derives the allowed/withheld split once; the withheld
        # half is what register_tools puts on the startup audit line.
        tools, withheld = build_tier3_tools(
            MockBPClient(), audit=make_audit(tmp_path), permissions=QUEUE_ONLY
        )
        assert {t.__name__ for t in tools} == {
            "retry_queue_item",
            "defer_queue_item",
            "create_queue_items",
        }
        assert withheld == {
            "set_schedule_enabled": ["Edit Schedule", "Retire Schedule"],
            "start_process": [
                "Create Process | Edit Process | Execute Process",
                "Control Resource",
            ],
            "stop_session": [
                "Create Process | Edit Process | Execute Process",
                "Control Resource",
            ],
            "trigger_schedule": ["Edit Schedule"],
            "stop_schedule": ["Edit Schedule"],
        }


# --- The dry-run / audit contract --------------------------------------------------


def _exceptioned_item(client: MockBPClient) -> dict:
    return next(i for i in client.get_queue_items(_queue_id(client)) if i["state"] == "Exceptioned")


def _queue_id(client: MockBPClient) -> str:
    return next(q["id"] for q in client.get_queues() if q["name"] == "Invoices")


class TestDryRunContract:
    def test_every_tool_names_its_action_and_audits_the_same_args(self, tmp_path):
        # The action name in the result is what the model reasons over, and
        # the audit line must carry the same tool name and the resolved args
        # the result previews — one contract across every action tool.
        tools, audit, client = tier3(tmp_path)
        item = _exceptioned_item(client)
        session_id = client.get_sessions()[0]["sessionId"]
        calls = [
            ("retry_queue_item", lambda: tools["retry_queue_item"]("Invoices", item["id"])),
            (
                "defer_queue_item",
                lambda: tools["defer_queue_item"]("Invoices", item["id"], 1, "2026-04-01T09:00:00"),
            ),
            (
                "create_queue_items",
                lambda: tools["create_queue_items"](
                    "Invoices",
                    [{"data": {"rows": [{"Ref": {"valueType": "Text", "value": "X-1"}}]}}],
                ),
            ),
            ("start_process", lambda: tools["start_process"]("Invoice Processing", "BOT-01")),
            ("stop_session", lambda: tools["stop_session"](session_id)),
            (
                "set_schedule_enabled",
                lambda: tools["set_schedule_enabled"]("Daily Invoice Run", False),
            ),
            ("trigger_schedule", lambda: tools["trigger_schedule"]("Daily Invoice Run")),
            ("stop_schedule", lambda: tools["stop_schedule"]("Daily Invoice Run")),
        ]
        results = [(name, call()) for name, call in calls]
        for name, result in results:
            assert result["action"] == name
        lines = read_audit(audit)
        assert [e["tool"] for e in lines] == [name for name, _ in calls]
        assert [e["args"] for e in lines] == [result["would"] for _, result in results]
        # The audited args ARE the audit schema downstream consumers parse —
        # pin the exact keys per tool, not just that something was recorded.
        queue_id = _queue_id(client)
        process = client.get_processes()[0]
        resource = client.get_resources()[0]
        assert [result["would"] for _, result in results] == [
            {"queue": "Invoices", "queue_id": queue_id, "item_id": item["id"]},
            {
                "queue": "Invoices",
                "queue_id": queue_id,
                "item_id": item["id"],
                "attempt_number": 1,
                "defer_until": "2026-04-01T09:00:00",
            },
            {
                "queue": "Invoices",
                "queue_id": queue_id,
                "item_count": 1,
                "items": [{"fields": ["Ref"], "data_types": {"Ref": "Text"}}],
            },
            {
                "process": "Invoice Processing",
                "process_id": process["processId"],
                "resource": "BOT-01",
                "resource_id": resource["id"],
            },
            {"session_id": session_id},
            {"schedule": "Daily Invoice Run", "schedule_id": "1", "enabled": False},
            {"schedule": "Daily Invoice Run", "schedule_id": "1", "start_time": None},
            {"schedule": "Daily Invoice Run", "schedule_id": "1"},
        ]


class TestRetryQueueItem:
    def test_dry_run_is_the_default_and_changes_nothing(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        item = _exceptioned_item(client)
        result = tools["retry_queue_item"]("Invoices", item["id"])
        assert result["dry_run"] is True
        assert result["would"]["queue_id"] == _queue_id(client)
        assert _exceptioned_item(client)["state"] == "Exceptioned"  # untouched
        assert [e["status"] for e in read_audit(audit)] == ["dry_run"]

    def test_live_run_retries_and_audits_attempt_before_success(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        item = _exceptioned_item(client)
        result = tools["retry_queue_item"]("invoices", item["id"], dry_run=False)
        assert result == {
            "action": "retry_queue_item",
            "dry_run": False,
            "result": {"attemptId": 2},
        }
        refreshed = next(
            i for i in client.get_queue_items(_queue_id(client)) if i["id"] == item["id"]
        )
        assert refreshed["state"] == "Pending"
        attempt, success = read_audit(audit)
        assert (attempt["status"], success["status"]) == ("attempt", "success")
        assert attempt["tool"] == success["tool"] == "retry_queue_item"
        # The audited args are the resolved call — name as passed, UUIDs under.
        assert attempt["args"] == {
            "queue": "invoices",
            "queue_id": _queue_id(client),
            "item_id": item["id"],
        }
        assert success["args"] == attempt["args"]

    def test_item_id_must_be_a_uuid_not_a_key_value(self, tmp_path):
        tools, audit, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="list_queue_items"):
            tools["retry_queue_item"]("Invoices", "INV-1002")
        assert read_audit(audit) == []  # validation failures never reach the audit

    def test_unknown_queue_fails_with_suggestions(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        item = _exceptioned_item(MockBPClient())
        with pytest.raises(ValueError, match="Invoices"):
            tools["retry_queue_item"]("Invoces", item["id"])

    def test_a_failing_write_is_audited_as_attempt_then_error(self, tmp_path):
        class FailingClient(MockBPClient):
            def retry_queue_item(self, queue_id, item_id):
                raise RuntimeError("estate said no")

        client = FailingClient()
        tools, audit, _ = tier3(tmp_path, client=client)
        item = _exceptioned_item(client)
        with pytest.raises(RuntimeError, match="estate said no"):
            tools["retry_queue_item"]("Invoices", item["id"], dry_run=False)
        attempt, error = read_audit(audit)
        assert attempt["status"] == "attempt"
        assert error["status"] == "error"
        # The detail is the exception class, never the message: estate error
        # text can echo response content, which is a scrub target.
        assert error["detail"] == "RuntimeError"
        assert "estate said no" not in audit.path.read_text()
        assert error["tool"] == "retry_queue_item"
        assert error["args"]["item_id"] == item["id"]


class TestAuditDetail:
    """Error lines carry the exception class (and HTTP status), never the
    message — exception text can echo response content, a scrub target."""

    def test_records_the_class_name_never_the_message(self):
        assert audit_detail(RuntimeError("ref AB123456C was rejected")) == "RuntimeError"

    def test_http_errors_add_the_status_code(self):
        exc = RuntimeError("409 Conflict for url: ...")
        exc.response = SimpleNamespace(status_code=409)
        assert audit_detail(exc) == "RuntimeError (HTTP 409)"

    def test_a_response_without_a_status_code_is_just_the_class(self):
        exc = RuntimeError("boom")
        exc.response = object()
        assert audit_detail(exc) == "RuntimeError"


class TestValidateActor:
    def test_none_passes_through(self):
        assert validate_actor(None) is None

    def test_strips_whitespace(self):
        assert validate_actor("  alice@example.com  ") == "alice@example.com"

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_after_stripping_fails_loud(self, value):
        with pytest.raises(ValueError, match="actor must not be empty"):
            validate_actor(value)

    def test_over_length_fails_loud(self):
        with pytest.raises(ValueError, match="256 characters"):
            validate_actor("x" * 257)

    def test_at_the_length_boundary_passes(self):
        assert validate_actor("x" * 256) == "x" * 256


def _passthrough(text):
    return text


def _upper(text):
    # Makes scrubbing observable: a passthrough would make "bind_actor
    # scrubs" indistinguishable from "bind_actor forgot to scrub".
    return text.upper() if text is not None else None


class TestBindActor:
    def test_sets_the_actor_on_every_line_recorded_inside(self, tmp_path):
        audit = make_audit(tmp_path)
        with bind_actor("alice", _passthrough):
            audit.record("t", {}, status="dry_run")
        assert read_audit(audit)[0]["actor"] == "alice"

    def test_no_actor_bound_means_no_actor_field(self, tmp_path):
        audit = make_audit(tmp_path)
        audit.record("t", {}, status="dry_run")
        assert "actor" not in read_audit(audit)[0]

    def test_resets_after_the_context_exits(self, tmp_path):
        audit = make_audit(tmp_path)
        with bind_actor("alice", _passthrough):
            pass
        audit.record("t", {}, status="dry_run")
        assert "actor" not in read_audit(audit)[0]

    def test_resets_even_when_the_body_raises(self, tmp_path):
        audit = make_audit(tmp_path)
        with pytest.raises(RuntimeError):
            with bind_actor("alice", _passthrough):
                raise RuntimeError("boom")
        audit.record("t", {}, status="dry_run")
        assert "actor" not in read_audit(audit)[0]

    def test_nested_binds_restore_the_outer_actor(self, tmp_path):
        audit = make_audit(tmp_path)
        with bind_actor("alice", _passthrough):
            with bind_actor("bob", _passthrough):
                audit.record("t", {}, status="dry_run")
            audit.record("t", {}, status="dry_run")
        actors = [e["actor"] for e in read_audit(audit)]
        assert actors == ["bob", "alice"]

    def test_actor_is_scrubbed_before_it_reaches_the_audit_line(self, tmp_path):
        audit = make_audit(tmp_path)
        with bind_actor("alice", _upper):
            audit.record("t", {}, status="dry_run")
        assert read_audit(audit)[0]["actor"] == "ALICE"

    def test_none_actor_never_lands_on_the_audit_line(self, tmp_path):
        # scrub_text is called with None (it's contractually None-safe, like
        # make_cached_scrub's wrapper), but nothing must bind onto the line.
        audit = make_audit(tmp_path)
        with bind_actor(None, _passthrough):
            audit.record("t", {}, status="dry_run")
        assert "actor" not in read_audit(audit)[0]

    def test_invalid_actor_is_rejected_before_binding(self, tmp_path):
        audit = make_audit(tmp_path)
        with pytest.raises(ValueError, match="actor must not be empty"):
            with bind_actor("   ", _passthrough):
                pass  # pragma: no cover - validate_actor raises before the body runs
        audit.record("t", {}, status="dry_run")
        assert "actor" not in read_audit(audit)[0]

    def test_composes_with_the_real_cached_scrub_helper(self, tmp_path):
        # The intended wiring: a host builds one cached scrub_text (the same
        # one every other tool-boundary field goes through) and passes it
        # into bind_actor, rather than a raw Scrubber — proves the two
        # actually compose, not just that bind_actor accepts a callable.
        from blue_prism_v7_mcp.tools.common import make_cached_scrub

        audit = make_audit(tmp_path)
        scrub_text = make_cached_scrub(NullScrubber())
        with bind_actor("alice", scrub_text):
            audit.record("t", {}, status="dry_run")
        assert read_audit(audit)[0]["actor"] == "alice"

    @pytest.mark.parametrize(
        "dispatch, propagates",
        [
            ("create_task", True),
            ("to_thread", True),
            ("run_in_executor", False),
        ],
    )
    def test_contextvar_propagation_across_asyncio_dispatch(self, tmp_path, dispatch, propagates):
        # An embedding host dispatches per-request work across asyncio's
        # concurrency primitives; the bound actor must follow wherever
        # contextvars actually propagate, and must NOT silently reappear
        # where they don't. asyncio.create_task and asyncio.to_thread (like
        # anyio's to_thread.run_sync, the path FastAPI's sync-route dispatch
        # uses) explicitly copy the calling context before handing off; a
        # bare loop.run_in_executor call submits directly, with no copy.
        audit = make_audit(tmp_path)

        def record():
            audit.record("t", {}, status="dry_run")

        async def record_task():
            record()

        async def run():
            with bind_actor("alice", _passthrough):
                if dispatch == "create_task":
                    await asyncio.create_task(record_task())
                elif dispatch == "to_thread":
                    await asyncio.to_thread(record)
                else:
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, record)

        asyncio.run(run())
        entry = read_audit(audit)[0]
        if propagates:
            assert entry["actor"] == "alice"
        else:
            assert "actor" not in entry


class TestPostWriteAuditFailures:
    """The attempt line is fail-closed (no audit, no write). After the write
    the calculus inverts: an audit failure can only misreport the estate, so
    it must never mask what actually happened."""

    def test_a_failing_attempt_line_blocks_the_write(self, tmp_path):
        tools, _, client = tier3(tmp_path, audit=flaky_audit(tmp_path, "attempt"))
        item = _exceptioned_item(client)
        with pytest.raises(OSError, match="disk full"):
            tools["retry_queue_item"]("Invoices", item["id"], dry_run=False)
        assert _exceptioned_item(client)["state"] == "Exceptioned"  # unsent

    def test_a_failing_success_line_does_not_mask_a_completed_action(self, tmp_path, caplog):
        tools, audit, client = tier3(tmp_path, audit=flaky_audit(tmp_path, "success"))
        item = _exceptioned_item(client)
        result = tools["retry_queue_item"]("Invoices", item["id"], dry_run=False)
        # The mutation happened and the result says so — flagged, not raised:
        # raising would invite a retry of an action that already succeeded.
        assert result["dry_run"] is False
        assert result["audit_status"] == "success_line_failed"
        assert "audit success line failed" in caplog.text
        (attempt,) = read_audit(audit)
        assert attempt["status"] == "attempt"

    def test_a_failing_error_line_does_not_mask_the_original_error(self, tmp_path, caplog):
        class FailingClient(MockBPClient):
            def retry_queue_item(self, queue_id, item_id):
                raise RuntimeError("estate said no")

        client = FailingClient()
        tools, audit, _ = tier3(tmp_path, client=client, audit=flaky_audit(tmp_path, "error"))
        item = _exceptioned_item(client)
        with pytest.raises(RuntimeError, match="estate said no"):
            tools["retry_queue_item"]("Invoices", item["id"], dry_run=False)
        assert "audit error line failed" in caplog.text
        (attempt,) = read_audit(audit)
        assert attempt["status"] == "attempt"


class TestDeferQueueItem:
    def test_defer_until_must_be_iso(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="defer_until"):
            tools["defer_queue_item"](
                "Invoices", _exceptioned_item(MockBPClient())["id"], 1, "next tuesday"
            )

    def test_defer_until_is_required(self, tmp_path):
        tools, _, client = tier3(tmp_path)
        with pytest.raises(ValueError, match="defer_until is required"):
            tools["defer_queue_item"]("Invoices", _exceptioned_item(client)["id"], 1, None)

    @pytest.mark.parametrize("bad", [0, -1, "3", 1.5, True, None])
    def test_attempt_number_must_be_a_positive_integer(self, bad, tmp_path):
        # Deferral is attempt-scoped; a malformed attempt number must fail
        # here naming the field, not surface as an opaque API 400 — and must
        # never look valid inside a dry-run "would" payload.
        tools, audit, client = tier3(tmp_path)
        with pytest.raises(ValueError, match="attempt_number"):
            tools["defer_queue_item"](
                "Invoices", _exceptioned_item(client)["id"], bad, "2026-04-01T09:00:00"
            )
        assert read_audit(audit) == []  # validation failures never reach the audit

    def test_dry_run_returns_the_full_call_unsent(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        item = _exceptioned_item(client)
        result = tools["defer_queue_item"]("Invoices", item["id"], 1, "2026-04-01T09:00:00")
        assert result["dry_run"] is True
        assert result["would"]["attempt_number"] == 1
        assert result["would"]["defer_until"] == "2026-04-01T09:00:00"
        assert _exceptioned_item(client)["state"] == "Exceptioned"

    def test_live_run_defers_the_attempt(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        item = _exceptioned_item(client)
        result = tools["defer_queue_item"](
            "Invoices", item["id"], 1, "2026-04-01T09:00:00", dry_run=False
        )
        assert result["result"] is None  # the API answers 204 — no body
        refreshed = next(
            i for i in client.get_queue_items(_queue_id(client)) if i["id"] == item["id"]
        )
        assert refreshed["state"] == "Deferred"
        assert refreshed["deferredDate"] == "2026-04-01T09:00:00"
        assert [e["status"] for e in read_audit(audit)] == ["attempt", "success"]


class TestStartProcess:
    def test_resolves_process_and_resource_names(self, tmp_path):
        tools, _, client = tier3(tmp_path)
        result = tools["start_process"]("invoice processing", "bot-01")
        assert result["dry_run"] is True
        assert result["would"]["process_id"] == client.get_processes()[0]["processId"]
        assert result["would"]["resource_id"] == client.get_resources()[0]["id"]

    def test_dry_run_creates_no_session(self, tmp_path):
        tools, _, client = tier3(tmp_path)
        before = len(client.get_sessions())
        tools["start_process"]("Invoice Processing", "BOT-01")
        assert len(client.get_sessions()) == before

    def test_live_run_starts_the_session(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        before = len(client.get_sessions())
        result = tools["start_process"]("Invoice Processing", "BOT-01", dry_run=False)
        assert result["result"]["status"] == "Running"
        assert result["result"]["sessionId"]
        sessions = client.get_sessions()
        assert len(sessions) == before + 1
        # The write received the RESOLVED ids, not the names the model spoke.
        assert sessions[-1]["processId"] == client.get_processes()[0]["processId"]
        assert sessions[-1]["resourceId"] == client.get_resources()[0]["id"]
        assert [e["status"] for e in read_audit(audit)] == ["attempt", "success"]

    def test_unknown_process_fails_with_suggestions(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="Invoice Processing"):
            tools["start_process"]("Invoice Processin", "BOT-01")

    def test_dry_run_echoes_parameter_types_only_never_values(self, tmp_path):
        tools, audit, _ = tier3(tmp_path)
        result = tools["start_process"](
            "Invoice Processing",
            "BOT-01",
            parameters={
                "Login": {"valueType": "Password", "value": "s3cr3t-pw"},
                "Amount": {"valueType": "number", "value": 123},
            },
        )
        # Names + canonicalised types surface; the values do not.
        assert result["would"]["parameter_types"] == {"Login": "Password", "Amount": "Number"}
        assert "s3cr3t-pw" not in json.dumps(result)
        assert "s3cr3t-pw" not in audit.path.read_text()
        assert "123" not in str(result["would"].get("parameter_types"))

    def test_live_run_forwards_validated_parameters_to_the_client(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        result = tools["start_process"](
            "Invoice Processing",
            "BOT-01",
            parameters={"InvoiceDate": {"valueType": "date", "value": "2026-03-01"}},
            dry_run=False,
        )
        session_id = result["result"]["sessionId"]
        # The PUT body the client received: canonical type, value intact.
        assert client._session_parameters[session_id] == {
            "InvoiceDate": {"valueType": "Date", "value": "2026-03-01"}
        }
        # Audit still records types only, even on the live attempt line.
        attempt = next(e for e in read_audit(audit) if e["status"] == "attempt")
        assert attempt["args"]["parameter_types"] == {"InvoiceDate": "Date"}
        assert "2026-03-01" not in audit.path.read_text()

    def test_unknown_value_type_fails_loudly(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="valueType"):
            tools["start_process"](
                "Invoice Processing",
                "BOT-01",
                parameters={"X": {"valueType": "Spreadsheet", "value": "x"}},
            )

    def test_malformed_parameter_spec_fails_loudly(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="valueType"):
            tools["start_process"](
                "Invoice Processing", "BOT-01", parameters={"X": "just-a-string"}
            )

    def test_spec_missing_value_fails_loudly(self, tmp_path):
        # Each half of the shape guard ("valueType" present AND "value" present)
        # is pinned: a spec with only valueType must be rejected, not fall
        # through to a KeyError when the value is read.
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="valueType"):
            tools["start_process"](
                "Invoice Processing", "BOT-01", parameters={"X": {"valueType": "Text"}}
            )

    def test_spec_missing_value_type_fails_loudly(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="valueType"):
            tools["start_process"](
                "Invoice Processing", "BOT-01", parameters={"X": {"value": "hello"}}
            )

    def test_empty_parameters_object_fails_loudly(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="non-empty object"):
            tools["start_process"]("Invoice Processing", "BOT-01", parameters={})

    def test_additional_parameters_are_validated_and_forwarded(self, tmp_path):
        tools, _, client = tier3(tmp_path)
        result = tools["start_process"](
            "Invoice Processing",
            "BOT-01",
            parameters={
                "Doc": {
                    "valueType": "Collection",
                    "value": [],
                    "additionalParameters": ["Sheet1", "Sheet2"],
                }
            },
            dry_run=False,
        )
        session_id = result["result"]["sessionId"]
        assert client._session_parameters[session_id]["Doc"]["additionalParameters"] == [
            "Sheet1",
            "Sheet2",
        ]

    def test_non_string_additional_parameters_fail_loudly(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="additionalParameters"):
            tools["start_process"](
                "Invoice Processing",
                "BOT-01",
                parameters={
                    "Doc": {"valueType": "Text", "value": "x", "additionalParameters": [1]}
                },
            )


class TestStopSession:
    def test_dry_run_is_the_default_and_changes_nothing(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        session = client.get_sessions()[0]
        before = session["status"]
        result = tools["stop_session"](session["sessionId"])
        assert result["dry_run"] is True
        assert result["would"] == {"session_id": session["sessionId"]}
        assert client.get_sessions()[0]["status"] == before  # untouched
        assert [e["status"] for e in read_audit(audit)] == ["dry_run"]

    def test_live_run_stops_and_audits_attempt_before_success(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        session_id = client.get_sessions()[0]["sessionId"]
        result = tools["stop_session"](session_id, dry_run=False)
        assert result == {
            "action": "stop_session",
            "dry_run": False,
            "result": {"sessionId": session_id, "status": "Stopped"},
        }
        assert client._find_session(session_id)["status"] == "Stopped"
        assert [e["status"] for e in read_audit(audit)] == ["attempt", "success"]

    def test_non_uuid_session_id_fails_loudly(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="session_id must be a UUID"):
            tools["stop_session"]("not-a-uuid")

    def test_a_freshly_started_session_can_be_stopped(self, tmp_path):
        # The documented workflow — start a process, then stop the session it
        # returns — must hold under mock run mode: start_process's sessionId is
        # a valid UUID, so stop_session's validate_uuid accepts it.
        tools, _, client = tier3(tmp_path)
        started = tools["start_process"]("Invoice Processing", "BOT-01", dry_run=False)
        session_id = started["result"]["sessionId"]
        validate_uuid(session_id, "session_id")  # raises if the mock minted a non-UUID
        stopped = tools["stop_session"](session_id, dry_run=False)
        assert stopped["result"] == {"sessionId": session_id, "status": "Stopped"}
        assert client._find_session(session_id)["status"] == "Stopped"

    def test_unknown_session_is_a_lenient_no_op_in_the_mock(self, tmp_path):
        # A valid UUID the mock doesn't hold: the stop request still answers
        # Stopped (the mock mirrors the live PATCH's fire-and-forget shape) and
        # leaves the existing sessions untouched.
        tools, _, client = tier3(tmp_path)
        unknown = "e8a9d7c2-5f10-4b3e-bd64-0000000009ff"
        result = tools["stop_session"](unknown, dry_run=False)
        assert result["result"] == {"sessionId": unknown, "status": "Stopped"}
        assert client._find_session(unknown) is None


class TestSetScheduleEnabled:
    def test_live_disable_retires_the_schedule(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        tools["set_schedule_enabled"]("Daily Invoice Run", False, dry_run=False)
        schedule = next(s for s in client.get_schedules() if s["name"] == "Daily Invoice Run")
        assert schedule["isRetired"] is True

    def test_dry_run_disable_changes_nothing(self, tmp_path):
        tools, _, client = tier3(tmp_path)
        tools["set_schedule_enabled"]("Daily Invoice Run", False)
        schedule = next(s for s in client.get_schedules() if s["name"] == "Daily Invoice Run")
        assert schedule["isRetired"] is False

    def test_live_enable_unretires_with_the_create_permission(self, tmp_path):
        tools, _, client = tier3(tmp_path)
        tools["set_schedule_enabled"]("Weekly Reconciliation", True, dry_run=False)
        schedule = next(s for s in client.get_schedules() if s["name"] == "Weekly Reconciliation")
        assert schedule["isRetired"] is False

    def test_enable_without_create_schedule_fails_loud_even_in_dry_run(self, tmp_path):
        retire_only = [p for p in _DEFAULT_PERMISSIONS if p != UNRETIRE_EXTRA_PERMISSION]
        tools, audit, _ = tier3(tmp_path, permissions=retire_only)
        with pytest.raises(ValueError, match="Create Schedule"):
            tools["set_schedule_enabled"]("Weekly Reconciliation", True)
        assert read_audit(audit) == []

    def test_accepts_the_integer_id_as_a_string(self, tmp_path):
        tools, _, client = tier3(tmp_path)
        result = tools["set_schedule_enabled"]("2", True)
        assert result["would"]["schedule_id"] == "2"


class TestTriggerSchedule:
    def test_start_time_is_validated_when_given(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="start_time"):
            tools["trigger_schedule"]("Daily Invoice Run", "soon")

    def test_dry_run_with_no_start_time(self, tmp_path):
        tools, audit, _ = tier3(tmp_path)
        result = tools["trigger_schedule"]("Daily Invoice Run")
        assert result["dry_run"] is True
        assert result["would"]["start_time"] is None
        assert [e["status"] for e in read_audit(audit)] == ["dry_run"]

    def test_live_run_triggers_forwarding_the_resolved_call(self, tmp_path):
        class RecordingClient(MockBPClient):
            def trigger_schedule(self, schedule_id, start_time=None):
                self.last_trigger = (schedule_id, start_time)
                return super().trigger_schedule(schedule_id, start_time)

        client = RecordingClient()
        tools, _, _ = tier3(tmp_path, client=client)
        result = tools["trigger_schedule"](
            "Daily Invoice Run", "2026-04-01T09:00:00", dry_run=False
        )
        assert result["result"]["status"] == "Triggered"
        assert client.last_trigger == ("1", "2026-04-01T09:00:00")


class TestStopSchedule:
    def test_dry_run_is_the_default_and_changes_nothing(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        result = tools["stop_schedule"]("Daily Invoice Run")
        assert result["dry_run"] is True
        assert result["would"] == {"schedule": "Daily Invoice Run", "schedule_id": "1"}
        assert "lastOutcome" not in client.get_schedules()[0]  # untouched
        assert [e["status"] for e in read_audit(audit)] == ["dry_run"]

    def test_live_run_stops_forwarding_the_resolved_id(self, tmp_path):
        class RecordingClient(MockBPClient):
            def stop_schedule(self, schedule_id):
                self.last_stop = schedule_id
                return super().stop_schedule(schedule_id)

        client = RecordingClient()
        tools, audit, _ = tier3(tmp_path, client=client)
        result = tools["stop_schedule"]("Daily Invoice Run", dry_run=False)
        assert result["dry_run"] is False
        assert client.last_stop == "1"
        assert [e["status"] for e in read_audit(audit)] == ["attempt", "success"]

    def test_only_needs_edit_schedule_like_its_trigger_sibling(self):
        assert "stop_schedule" in resolve_capabilities(["Edit Schedule"])
        assert "stop_schedule" not in resolve_capabilities(["Retire Schedule"])


# --- register_tools: the enable_actions gate ----------------------------------------


class FakeApp:
    def __init__(self):
        self.registered = []

    def tool(self):
        def decorator(fn):
            self.registered.append(fn)
            return fn

        return decorator


READ_TOOLS = [
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


class TestRegisterToolsGate:
    def test_default_config_registers_reads_only(self):
        names = register_tools(FakeApp(), MockBPClient(), NullScrubber(), config=BPConfig())
        assert names == READ_TOOLS

    def test_config_is_required_not_optional(self):
        # A server entrypoint that forgot the config must fail at startup —
        # not silently register a read-only surface with BP_ENABLE_ACTIONS
        # ignored.
        with pytest.raises(TypeError):
            register_tools(FakeApp(), MockBPClient(), NullScrubber())

    def test_actions_disabled_registers_reads_only(self, tmp_path):
        config = BPConfig(enable_actions=False, audit_log_path=str(tmp_path / "a.jsonl"))
        names = register_tools(FakeApp(), MockBPClient(), NullScrubber(), config=config)
        assert names == READ_TOOLS

    def test_actions_enabled_registers_the_allowed_action_tools(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        config = BPConfig(enable_actions=True, audit_log_path=str(path))
        names = register_tools(FakeApp(), MockBPClient(), NullScrubber(), config=config)
        assert names == READ_TOOLS + [
            "retry_queue_item",
            "defer_queue_item",
            "create_queue_items",
            "start_process",
            "stop_session",
            "set_schedule_enabled",
            "trigger_schedule",
            "stop_schedule",
        ]
        startup = [json.loads(line) for line in path.read_text().splitlines()]
        assert [e["status"] for e in startup] == ["startup"]
        assert startup[0]["tool"] == "register_tools"
        assert startup[0]["args"]["registered"] == sorted(ALL_ACTION_TOOLS)
        assert startup[0]["args"]["withheld"] == {}

    def test_registered_tools_are_wired_to_the_live_client(self, tmp_path):
        # Registration must hand the builders the real client, not just
        # produce well-named closures: a registered read and a registered
        # action dry-run both have to answer from the client's data.
        app = FakeApp()
        client = MockBPClient()
        config = BPConfig(enable_actions=True, audit_log_path=str(tmp_path / "a.jsonl"))
        register_tools(app, client, NullScrubber(), config=config)
        by_name = {fn.__name__: fn for fn in app.registered}
        queues = by_name["list_queues"]()
        assert any(q["name"] == "Invoices" for q in queues["items"])
        # Exercises tier1's scrub boundary, so a mis-wired scrubber crashes too.
        items = by_name["list_queue_items"]("Invoices", "Exceptioned", _date(60), _date(0))
        assert items["meta"]["total"] >= 1
        summary = by_name["exception_summary"]("Invoices", _date(60), _date(0))
        assert summary["meta"]["total"] >= 0
        dry = by_name["retry_queue_item"](
            "Invoices",
            client.get_queue_items(
                next(q["id"] for q in client.get_queues() if q["name"] == "Invoices")
            )[0]["id"],
        )
        assert dry["dry_run"] is True
        assert dry["would"]["queue_id"] == next(
            q["id"] for q in client.get_queues() if q["name"] == "Invoices"
        )

    def test_withheld_tools_are_audited_with_their_missing_clauses(self, tmp_path):
        path = tmp_path / "audit.jsonl"
        config = BPConfig(enable_actions=True, audit_log_path=str(path))
        client = MockBPClient(permissions=QUEUE_ONLY)
        names = register_tools(FakeApp(), client, NullScrubber(), config=config)
        assert set(names) & ALL_ACTION_TOOLS == {
            "retry_queue_item",
            "defer_queue_item",
            "create_queue_items",
        }
        startup = json.loads(path.read_text().splitlines()[0])
        assert startup["args"]["withheld"]["trigger_schedule"] == ["Edit Schedule"]
        assert startup["args"]["withheld"]["start_process"] == [
            "Create Process | Edit Process | Execute Process",
            "Control Resource",
        ]

    def test_actions_enabled_without_audit_path_refuses_to_start(self):
        config = BPConfig(enable_actions=True)
        with pytest.raises(ValueError, match="BP_AUDIT_LOG_PATH"):
            register_tools(FakeApp(), MockBPClient(), NullScrubber(), config=config)

    def test_a_failed_permissions_call_refuses_to_start(self, tmp_path):
        class NoPermissionsClient(MockBPClient):
            def get_user_permissions(self):
                raise RuntimeError("403 from the permissions endpoint")

        config = BPConfig(enable_actions=True, audit_log_path=str(tmp_path / "a.jsonl"))
        with pytest.raises(RuntimeError, match="403"):
            register_tools(FakeApp(), NoPermissionsClient(), NullScrubber(), config=config)

    def test_every_action_tool_carries_a_description(self, tmp_path):
        app = FakeApp()
        config = BPConfig(enable_actions=True, audit_log_path=str(tmp_path / "a.jsonl"))
        register_tools(app, MockBPClient(), NullScrubber(), config=config)
        for fn in app.registered:
            assert fn.__doc__ and len(fn.__doc__.strip()) > 80, fn.__name__


# --- create_queue_items tool ---------------------------------------------------


class TestCreateQueueItems:
    def test_dry_run_is_the_default_and_changes_nothing(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        before = len(client.get_queue_items(_queue_id(client)))
        result = tools["create_queue_items"]("Invoices", [{"priority": 2, "tags": ["test"]}])
        assert result["dry_run"] is True
        assert result["would"]["queue_id"] == _queue_id(client)
        assert result["would"]["item_count"] == 1
        assert len(client.get_queue_items(_queue_id(client))) == before
        assert [e["status"] for e in read_audit(audit)] == ["dry_run"]

    def test_live_run_creates_items_in_the_mock_queue(self, tmp_path):
        tools, audit, client = tier3(tmp_path)
        queue_id = _queue_id(client)
        before = len(client.get_queue_items(queue_id))
        result = tools["create_queue_items"](
            "Invoices",
            [{"priority": 1, "status": "New"}, {"tags": ["batch"]}],
            dry_run=False,
        )
        assert result["dry_run"] is False
        assert len(result["result"]["ids"]) == 2
        after = client.get_queue_items(queue_id)
        assert len(after) == before + 2
        new_items = after[before:]
        assert new_items[0]["state"] == "Pending"
        assert new_items[0]["priority"] == 1
        assert new_items[1]["tags"] == ["batch"]
        assert [e["status"] for e in read_audit(audit)] == ["attempt", "success"]

    def test_dry_run_echoes_data_field_names_and_types_never_values(self, tmp_path):
        tools, audit, _ = tier3(tmp_path)
        result = tools["create_queue_items"](
            "Invoices",
            [
                {
                    "data": {
                        "rows": [
                            {
                                "Secret": {"valueType": "Password", "value": "s3cr3t-pw"},
                                "Amount": {"valueType": "Number", "value": 9999},
                            }
                        ]
                    }
                }
            ],
        )
        item_shape = result["would"]["items"][0]
        assert set(item_shape["fields"]) == {"Secret", "Amount"}
        assert item_shape["data_types"] == {"Secret": "Password", "Amount": "Number"}
        assert "s3cr3t-pw" not in json.dumps(result)
        assert "s3cr3t-pw" not in audit.path.read_text()
        assert "9999" not in json.dumps(result["would"]["items"])

    def test_dry_run_data_type_casing_is_canonicalised_in_audit_shape(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        result = tools["create_queue_items"](
            "Invoices",
            [{"data": {"rows": [{"Ref": {"valueType": "text", "value": "INV-1"}}]}}],
        )
        item_shape = result["would"]["items"][0]
        assert item_shape["data_types"] == {"Ref": "Text"}

    def test_nested_collection_inner_values_never_reach_audit_shape(self, tmp_path):
        tools, audit, _ = tier3(tmp_path)
        result = tools["create_queue_items"](
            "Invoices",
            [
                {
                    "data": {
                        "rows": [
                            {
                                "Items": {
                                    "valueType": "Collection",
                                    "value": {
                                        "rows": [
                                            {
                                                "Secret": {
                                                    "valueType": "Password",
                                                    "value": "s3cr3t-nested",
                                                }
                                            }
                                        ]
                                    },
                                }
                            }
                        ]
                    }
                }
            ],
        )
        item_shape = result["would"]["items"][0]
        assert item_shape["data_types"] == {"Items": "Collection"}
        assert "s3cr3t-nested" not in json.dumps(result)
        assert "s3cr3t-nested" not in audit.path.read_text()

    def test_unknown_queue_fails_with_suggestions(self, tmp_path):
        tools, _, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="Invoices"):
            tools["create_queue_items"]("Invoces", [{}])

    def test_invalid_items_fail_before_the_audit(self, tmp_path):
        tools, audit, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="non-empty list"):
            tools["create_queue_items"]("Invoices", [])
        assert read_audit(audit) == []

    def test_unknown_item_key_fails_naming_it(self, tmp_path):
        tools, audit, _ = tier3(tmp_path)
        with pytest.raises(ValueError, match="badKey"):
            tools["create_queue_items"]("Invoices", [{"badKey": "x"}])
        assert read_audit(audit) == []

    def test_mock_queue_pending_count_increases(self, tmp_path):
        tools, _, client = tier3(tmp_path)
        before_pending = next(q for q in client.get_queues() if q["name"] == "Invoices")[
            "pendingItemCount"
        ]
        tools["create_queue_items"]("Invoices", [{"priority": 1}], dry_run=False)
        after_pending = next(q for q in client.get_queues() if q["name"] == "Invoices")[
            "pendingItemCount"
        ]
        assert after_pending == before_pending + 1

    def test_a_failing_write_is_audited_as_attempt_then_error(self, tmp_path):
        class FailingClient(MockBPClient):
            def create_queue_items(self, queue_id, items):
                raise RuntimeError("queue locked")

        client = FailingClient()
        tools, audit, _ = tier3(tmp_path, client=client)
        with pytest.raises(RuntimeError, match="queue locked"):
            tools["create_queue_items"]("Invoices", [{}], dry_run=False)
        attempt, error = read_audit(audit)
        assert attempt["status"] == "attempt"
        assert error["status"] == "error"
        assert error["detail"] == "RuntimeError"
        assert "queue locked" not in audit.path.read_text()


# --- validate_uuid ------------------------------------------------------------------


class TestValidateUuid:
    def test_uuid_passes_through_stripped(self):
        uuid = "f3b2a190-8c47-4e2d-9b55-000000000401"
        assert validate_uuid(f"  {uuid} ", "item_id") == uuid

    @pytest.mark.parametrize("value", ["INV-1002", "", None, 42])
    def test_rejects_non_uuids_naming_the_field(self, value):
        with pytest.raises(ValueError, match="item_id"):
            validate_uuid(value, "item_id")

    def test_hint_is_appended_when_given(self):
        with pytest.raises(ValueError, match="list_queue_items"):
            validate_uuid("nope", "item_id", hint="Use list_queue_items to find the item's id.")

    def test_no_hint_means_no_trailing_noise(self):
        with pytest.raises(ValueError) as exc:
            validate_uuid("nope", "item_id")
        assert str(exc.value) == "item_id must be a UUID; got 'nope'."

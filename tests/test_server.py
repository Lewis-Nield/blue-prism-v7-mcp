"""Tests for the server wiring (Phase 6).

The contract pinned here: the client factory (live vs mock, with the loud
all-at-once connection-settings check), build_server assembling a real FastMCP
app with the full registered surface, startup misconfigurations surfacing as a
clean SystemExit from main() (never a half-started transport), and logging
pinned to stderr — stdout belongs to JSON-RPC.
"""

import asyncio
import logging
import sys

import pytest
from mcp.server.fastmcp import FastMCP

from blue_prism_mcp.client import BPClient
from blue_prism_mcp.config import BPConfig
from blue_prism_mcp.mock import MockBPClient
from blue_prism_mcp.pii import ScrubberUnavailableError
from blue_prism_mcp.server import SERVER_NAME, build_client, build_server, main

CONNECTION_ENV_VARS = ("BP_API_BASE_URL", "BP_AUTH_URL", "BP_CLIENT_ID", "BP_CLIENT_SECRET")

LIVE_SETTINGS = dict(
    base_url="https://bp.example.com/api/v7",
    auth_url="https://auth.example.com",
    client_id="svc-mcp",
    client_secret="s3cret",
)

READ_TOOLS = {
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
}

ACTION_TOOLS = {
    "retry_queue_item",
    "defer_queue_item",
    "start_process",
    "stop_session",
    "set_schedule_enabled",
    "trigger_schedule",
    "stop_schedule",
}


def tool_names(app: FastMCP) -> set[str]:
    return {tool.name for tool in asyncio.run(app.list_tools())}


@pytest.fixture
def clean_logging():
    """Snapshot/restore root logging state — main() adds a root handler."""
    root = logging.getLogger()
    pkg = logging.getLogger("blue_prism_mcp")
    saved = (list(root.handlers), root.level, pkg.level)
    yield
    root.handlers[:], root.level, pkg.level = saved[0], saved[1], saved[2]
    root.setLevel(saved[1])
    pkg.setLevel(saved[2])


@pytest.fixture
def bare_env(monkeypatch):
    """Strip every BP_* variable so from_env sees only what the test sets."""
    import os

    for var in list(os.environ):
        if var.startswith("BP_"):
            monkeypatch.delenv(var)
    return monkeypatch


class TestBuildClient:
    def test_mock_source_builds_the_offline_client(self):
        client = build_client(BPConfig(data_source="mock"))
        assert isinstance(client, MockBPClient)

    def test_mock_source_needs_no_connection_settings(self):
        # The whole point of mock mode: zero estate access, zero credentials.
        client = build_client(BPConfig(data_source="mock"))
        assert client.get_resources()  # fixtures present, no HTTP

    def test_live_source_builds_the_real_client_without_network(self):
        # BPClient is lazy (first token fetch happens on first request), so
        # construction alone must not touch the network — no mocking needed.
        client = build_client(BPConfig(data_source="live", **LIVE_SETTINGS))
        assert isinstance(client, BPClient)

    def test_live_with_nothing_configured_names_every_missing_variable(self):
        with pytest.raises(ValueError) as exc:
            build_client(BPConfig(data_source="live"))
        message = str(exc.value)
        for var in CONNECTION_ENV_VARS:
            assert var in message
        assert "BP_DATA_SOURCE=mock" in message  # the no-estate escape is advertised

    @pytest.mark.parametrize(
        "attr,var",
        [
            ("base_url", "BP_API_BASE_URL"),
            ("auth_url", "BP_AUTH_URL"),
            ("client_id", "BP_CLIENT_ID"),
            ("client_secret", "BP_CLIENT_SECRET"),
        ],
    )
    def test_live_with_one_setting_missing_names_exactly_that_variable(self, attr, var):
        settings = {**LIVE_SETTINGS, attr: ""}
        with pytest.raises(ValueError) as exc:
            build_client(BPConfig(data_source="live", **settings))
        message = str(exc.value)
        assert var in message
        for other in set(CONNECTION_ENV_VARS) - {var}:
            assert other not in message

    def test_live_with_whitespace_only_credential_reads_as_missing(self):
        # Paste noise: BPConfig strips it at the boundary, so the startup
        # check names the variable instead of letting a truthy " " through to
        # die at the first token fetch.
        settings = {**LIVE_SETTINGS, "client_secret": "   "}
        with pytest.raises(ValueError, match="BP_CLIENT_SECRET"):
            build_client(BPConfig(data_source="live", **settings))

    @pytest.mark.parametrize("source", ["", "Mock", "database", "live "])
    def test_unknown_source_refuses_to_start(self, source):
        # from_env normalises case/whitespace; a value built any other way
        # must match exactly or fail loud, mirroring build_scrubber.
        with pytest.raises(ValueError, match="data_source"):
            build_client(BPConfig(data_source=source))


class TestBuildServer:
    def test_mock_app_carries_the_full_read_surface(self):
        app = build_server(BPConfig(data_source="mock"))
        assert isinstance(app, FastMCP)
        assert app.name == SERVER_NAME
        assert tool_names(app) == READ_TOOLS

    def test_handshake_reports_this_artifacts_version(self):
        # serverInfo.version must identify blue-prism-mcp, not the mcp
        # library FastMCP defaults to.
        import blue_prism_mcp

        app = build_server(BPConfig(data_source="mock"))
        assert app._mcp_server.version == blue_prism_mcp.__version__

    def test_version_wiring_fails_loud_when_the_private_seam_is_gone(self):
        # The mcp pins are ranges: a future-compatible release could rename or
        # drop FastMCP._mcp_server.version. Better a loud, actionable startup
        # error than a silently wrong version in every handshake.
        from blue_prism_mcp.server import _apply_server_version

        class NoSeam:  # FastMCP-shaped enough to call, missing the version seam
            pass

        with pytest.raises(RuntimeError, match="mcp"):
            _apply_server_version(NoSeam(), "9.9.9")

    def test_version_wiring_happy_path_sets_the_lowlevel_version(self):
        from blue_prism_mcp.server import _apply_server_version

        class Seam:
            version = None

        class App:
            _mcp_server = Seam()

        app = App()
        _apply_server_version(app, "9.9.9")
        assert app._mcp_server.version == "9.9.9"

    def test_actions_enabled_registers_tier3(self, tmp_path):
        config = BPConfig(
            data_source="mock",
            enable_actions=True,
            audit_log_path=str(tmp_path / "audit.jsonl"),
        )
        app = build_server(config)
        assert tool_names(app) == READ_TOOLS | ACTION_TOOLS

    def test_actions_without_audit_path_refuse_to_start_even_in_mock(self):
        # The governance contract does not relax for mock mode: an enabled
        # action surface always requires an audit destination.
        with pytest.raises(ValueError, match="BP_AUDIT_LOG_PATH"):
            build_server(BPConfig(data_source="mock", enable_actions=True))

    def test_unknown_pii_backend_refuses_to_start(self):
        with pytest.raises(ScrubberUnavailableError):
            build_server(BPConfig(data_source="mock", pii_backend="rot13"))

    def test_configured_scrubber_reaches_the_registered_tools(self):
        # End-to-end through the app: the scrubber build_server constructs
        # from config must be the one the registered tools actually scrub
        # with (a server wired with the wrong/no scrubber would still pass
        # every registration test). The custom pattern matches the customer
        # reference in the mock session fixture's exceptionMessage.
        config = BPConfig(
            data_source="mock",
            pii_backend="regex",
            pii_custom_patterns=(("CUSTOMER_REF", r"ref [\d ]+\d"),),
        )
        app = build_server(config)
        out = asyncio.run(
            app.call_tool("list_sessions", {"start_date": "2020-01-01", "end_date": "2030-01-01"})
        )
        blob = "".join(c.text for c in out if hasattr(c, "text"))
        assert "[CUSTOMER_REF]" in blob
        assert "4929 1234" not in blob  # the fixture's raw reference never leaves

    def test_startup_line_reports_the_surface_without_secrets(self, caplog):
        with caplog.at_level(logging.INFO, logger="blue_prism_mcp.server"):
            build_server(BPConfig(data_source="mock", client_secret="s3cret"))
        startup = caplog.records[-1].getMessage()
        assert "20 tools" in startup
        assert "data_source=mock" in startup
        assert "actions=disabled" in startup
        assert "s3cret" not in startup


class TestMain:
    def test_mock_env_builds_and_runs_the_stdio_app(self, bare_env, clean_logging, monkeypatch):
        bare_env.setenv("BP_DATA_SOURCE", "mock")
        ran = []
        monkeypatch.setattr(FastMCP, "run", lambda self, *a, **kw: ran.append(self.name))
        main()
        assert ran == [SERVER_NAME]

    def test_missing_connection_settings_exit_cleanly(self, bare_env, clean_logging, monkeypatch):
        # Default env is live mode with nothing set: an operator message via
        # SystemExit (exit code 1), not a traceback — and never a started
        # transport.
        monkeypatch.setattr(
            FastMCP, "run", lambda self, *a, **kw: pytest.fail("transport must not start")
        )
        with pytest.raises(SystemExit) as exc:
            main()
        message = str(exc.value)
        assert message.startswith(f"{SERVER_NAME}:")
        for var in CONNECTION_ENV_VARS:
            assert var in message

    def test_bad_pii_backend_exits_cleanly(self, bare_env, clean_logging, monkeypatch):
        bare_env.setenv("BP_DATA_SOURCE", "mock")
        bare_env.setenv("BP_PII_BACKEND", "rot13")
        monkeypatch.setattr(
            FastMCP, "run", lambda self, *a, **kw: pytest.fail("transport must not start")
        )
        with pytest.raises(SystemExit, match="rot13"):
            main()

    def test_incompatible_mcp_exits_cleanly(self, bare_env, clean_logging, monkeypatch):
        # A seam failure in build_server routes through main()'s startup-error
        # net: an operator sees a one-line message, not an AttributeError
        # traceback, and the transport never starts.
        from blue_prism_mcp import server

        bare_env.setenv("BP_DATA_SOURCE", "mock")

        def _raise(app, version):
            raise RuntimeError("serverInfo.version seam gone")

        monkeypatch.setattr(server, "_apply_server_version", _raise)
        monkeypatch.setattr(
            FastMCP, "run", lambda self, *a, **kw: pytest.fail("transport must not start")
        )
        with pytest.raises(SystemExit, match="serverInfo.version seam gone") as exc:
            server.main()
        assert str(exc.value).startswith(f"{SERVER_NAME}:")

    def test_repeated_configuration_does_not_stack_handlers(self, clean_logging):
        # main() may run more than once in-process; logging setup must be
        # idempotent or every line would duplicate per extra handler.
        from blue_prism_mcp.server import _configure_logging

        _configure_logging()
        after_first = len(logging.getLogger().handlers)
        _configure_logging()
        _configure_logging()
        assert len(logging.getLogger().handlers) == after_first

    def test_logging_lands_on_stderr_never_stdout(self, bare_env, clean_logging, monkeypatch):
        bare_env.setenv("BP_DATA_SOURCE", "mock")
        monkeypatch.setattr(FastMCP, "run", lambda self, *a, **kw: None)
        main()
        root = logging.getLogger()
        streams = [
            handler.stream
            for handler in root.handlers
            if isinstance(handler, logging.StreamHandler)
        ]
        assert sys.stderr in streams
        assert all(stream is not sys.stdout for stream in streams)
        # Our own startup lines stay visible; libraries are capped at WARNING.
        assert logging.getLogger("blue_prism_mcp").level == logging.INFO
        assert root.level == logging.WARNING

"""End-to-end validation (Phase 7).

Phase 6 wired the server and was checked by hand: a real stdio handshake,
byte-clean JSON-RPC on stdout, logs on stderr. This formalises that check so it
can never silently regress, and proves the surface against a genuine MCP client
rather than hand-rolled frames.

Two angles, both launching the actual artifact as a subprocess
(``python -m blue_prism_v7_mcp``, mock data source — no estate, no credentials):

  * the real ``mcp`` client driving initialize -> tools/list -> tools/call, and
  * a raw subprocess asserting that nothing but JSON-RPC ever reaches stdout.

The async client is driven through ``asyncio.run`` (anyio runs on the asyncio
backend), matching the rest of the suite — no new test dependency.
"""

import asyncio
import json
import os
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import LATEST_PROTOCOL_VERSION

from blue_prism_v7_mcp import __version__

# The full read surface the mock server must advertise — the same set
# build_server registers, asserted here through a real client handshake.
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
}

# Launch the artifact the way an operator would, but by module so PATH
# resolution of the console script never enters the picture.
_LAUNCH = [sys.executable, "-m", "blue_prism_v7_mcp"]

# A window bounding both end-to-end paths — the raw subprocess and the real
# client session — so a deadlock or protocol mismatch fails loudly instead of
# hanging the suite. Generous: a cold import (FastMCP + our package) on a loaded
# CI runner should never approach this.
_TIMEOUT = 30


def _mock_env(**overrides: str) -> dict[str, str]:
    """A clean environment that runs the server offline.

    Inherits the real environment (interpreter, PATH, venv) so the subprocess
    can import the package, but strips every ``BP_*`` variable first so the
    host's own deployment config can never leak into a test, then pins mock
    mode. Overrides layer on top.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("BP_")}
    env["BP_DATA_SOURCE"] = "mock"
    env.update(overrides)
    return env


async def _drive_client(params: StdioServerParameters):
    """Run a full client session against the spawned server and return results."""
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool(
                "list_sessions",
                {"start_date": "2020-01-01", "end_date": "2030-01-01"},
            )
            return init, tools, result


class TestRealClientHandshake:
    """The artifact spoken to by the real mcp client, end to end."""

    def test_handshake_surface_and_a_tool_call(self):
        # regex backend so the scrubber path is exercised over the wire too,
        # not just the null default.
        params = StdioServerParameters(
            command=_LAUNCH[0], args=_LAUNCH[1:], env=_mock_env(BP_PII_BACKEND="regex")
        )
        # Bound the client drive with the same window the raw subprocess test
        # uses: a regression, deadlock, or protocol mismatch must fail loudly,
        # never hang the suite. wait_for cancels the coroutine on timeout, so
        # stdio_client/ClientSession unwind and the spawned server is torn down.
        init, tools, result = asyncio.run(asyncio.wait_for(_drive_client(params), timeout=_TIMEOUT))

        # serverInfo identifies THIS artifact and THIS version (the guarded
        # _mcp_server.version seam, now observed through a real client).
        assert init.serverInfo.name == "blue-prism-v7-mcp"
        assert init.serverInfo.version == __version__

        # The mock server advertises exactly the read surface.
        assert {tool.name for tool in tools.tools} == READ_TOOLS

        # The tool call round-trips a well-formed envelope, not an error.
        assert result.isError is False
        text = "".join(block.text for block in result.content if hasattr(block, "text"))
        payload = json.loads(text)
        assert set(payload) == {"items", "meta"}
        assert set(payload["meta"]) == {"total", "returned", "truncated", "sorted_by"}


class TestStdoutHygiene:
    """stdout belongs to JSON-RPC; logging must never touch it."""

    def _handshake_frames(self) -> str:
        # Newline-delimited JSON-RPC: initialize, the required initialized
        # notification, then a tools/list to generate a second response.
        return (
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": LATEST_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "stdout-hygiene-test", "version": "0"},
                    },
                }
            )
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
            + "\n"
            + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            + "\n"
        )

    def test_stdout_is_only_json_rpc_and_logs_land_on_stderr(self):
        proc = subprocess.run(
            _LAUNCH,
            input=self._handshake_frames(),
            capture_output=True,
            text=True,
            env=_mock_env(),
            timeout=_TIMEOUT,
        )

        # Closing stdin (EOF after the frames) ends the stdio session, so a
        # clean run exits zero. A non-zero code means app.run() crashed after
        # the ready line — output the framing checks below would still accept —
        # so assert it explicitly, surfacing both streams for diagnosis.
        assert proc.returncode == 0, (
            f"server exited {proc.returncode}\nSTDERR:\n{proc.stderr}\nSTDOUT:\n{proc.stdout}"
        )

        # Every non-blank stdout line is a JSON-RPC message — no log line, no
        # banner, no stray print ever slipped onto the protocol channel.
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        assert lines, "server produced no JSON-RPC on stdout"
        for line in lines:
            message = json.loads(line)  # raises if anything non-JSON reached stdout
            assert message.get("jsonrpc") == "2.0"

        # And the startup log line went where it belongs: stderr, not stdout.
        assert "blue-prism-v7-mcp ready" in proc.stderr
        assert "blue-prism-v7-mcp ready" not in proc.stdout

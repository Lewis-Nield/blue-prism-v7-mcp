"""FastMCP stdio server + console entrypoint.

The wiring layer: build the config from the environment, construct the client
(live `BPClient` or, with ``BP_DATA_SOURCE=mock``, the in-memory
`MockBPClient`), build the configured scrubber, register the tool surface, and
run the stdio transport.

Every startup failure here is LOUD and happens before the transport starts:
missing connection settings, an unknown data source, an unloadable PII
backend, a missing/unwritable audit path, or a failed permissions call all
refuse to start rather than serving a silently degraded surface.

stdout hygiene: the stdio transport speaks JSON-RPC over stdout, so nothing
else may write there. Python logging defaults to stderr, but `main` pins an
explicit stderr handler on the root logger before anything heavy runs, so a
library calling `logging.basicConfig()` later cannot claim stdout first. The
Presidio/spaCy import burst is already silenced at source (see
pii.PresidioScrubber, which quiets those loggers before importing them).
"""

from __future__ import annotations

import logging
import sys

import requests
from mcp.server.fastmcp import FastMCP

from . import __version__
from .client import BPClient
from .config import BPConfig
from .mock import MockBPClient, demo_estate
from .pii import ScrubberUnavailableError, build_scrubber
from .tools import register_tools

_log = logging.getLogger("blue_prism_v7_mcp.server")

SERVER_NAME = "blue-prism-v7-mcp"

# Marks the stderr handler _configure_logging installs, so a re-entrant call
# recognises its own handler and stays idempotent instead of stacking more.
_OUR_HANDLER_MARK = "_blue_prism_v7_mcp_handler"

# Connection settings a LIVE deployment cannot run without, as (config
# attribute, environment variable) pairs — the error names the env vars
# because the environment is the deployment contract (BPConfig.from_env).
_REQUIRED_CONNECTION_SETTINGS = (
    ("base_url", "BP_API_BASE_URL"),
    ("auth_url", "BP_AUTH_URL"),
    ("client_id", "BP_CLIENT_ID"),
    ("client_secret", "BP_CLIENT_SECRET"),
)

# Expected startup misconfigurations, converted by main() into a clean
# one-line SystemExit instead of a traceback: config errors and the strict
# permissions-shape check (ValueError), an unknown/unloadable PII backend
# (ScrubberUnavailableError), an untouchable audit file (OSError), a failed
# GET /user/permissions (RequestException), and an incompatible mcp release
# that no longer exposes the serverInfo.version seam (RuntimeError, raised by
# _apply_server_version). Anything else is a bug and tracebacks normally.
_STARTUP_ERRORS = (
    ValueError,
    ScrubberUnavailableError,
    OSError,
    requests.RequestException,
    RuntimeError,
)


def build_client(config: BPConfig) -> BPClient | MockBPClient:
    """Construct the client the deployment asked for — or refuse to start.

    Live mode validates the connection settings here, at startup, because
    BPClient itself is lazy (the first token fetch happens on the first
    request): without this check a misconfigured server would hand the model
    a full tool surface where every call fails. All missing variables are
    reported in one error, not one per restart.
    """
    source = config.data_source
    if source == "mock":
        return MockBPClient()
    if source == "demo":
        return demo_estate()
    if source == "live":
        missing = [var for attr, var in _REQUIRED_CONNECTION_SETTINGS if not getattr(config, attr)]
        if missing:
            raise ValueError(
                "Missing required connection settings: " + ", ".join(missing) + ". "
                "Set them in the environment (see .env.example), or run without "
                "an estate via BP_DATA_SOURCE=mock."
            )
        return BPClient(config)
    raise ValueError(f"Unknown data_source {source!r} — expected 'live', 'mock', or 'demo'.")


def build_server(config: BPConfig) -> FastMCP:
    """Assemble the FastMCP app: scrubber, client, and the registered tools.

    Separated from main() so tests (and embedders) can build a fully wired
    app from an injected config without touching the environment or the
    transport.
    """
    scrubber = build_scrubber(config)
    client = build_client(config)
    app = FastMCP(SERVER_NAME)
    _apply_server_version(app, __version__)
    names = register_tools(app, client, scrubber, config)
    _log.info(
        "%s ready: %d tools (%s) | data_source=%s pii_backend=%s actions=%s",
        SERVER_NAME,
        len(names),
        ", ".join(names),
        config.data_source,
        config.pii_backend,
        "enabled" if config.enable_actions else "disabled",
    )
    return app


def _apply_server_version(app: FastMCP, version: str) -> None:
    """Stamp THIS artifact's version into the handshake's serverInfo.

    FastMCP (1.x) exposes no public way to set serverInfo.version — its
    constructor takes no version and builds its lowlevel Server without one,
    so the handshake would otherwise report the mcp library's own version. The
    value lives on that lowlevel Server, reachable only through the private
    `_mcp_server` handle.

    We cross that seam deliberately, but guard it: the relaxed `mcp` range
    means a future-compatible release could rename or restructure the handle.
    Rather than AttributeError-tracebacking (or silently reporting the wrong
    version in every handshake), fail loud at startup with a message the
    operator can act on.
    """
    server = getattr(app, "_mcp_server", None)
    if server is None or not hasattr(server, "version"):
        raise RuntimeError(
            "Cannot set serverInfo.version: this 'mcp' release does not expose "
            "FastMCP._mcp_server.version. Pin 'mcp' to a tested version "
            "(developed against 1.27) or update the version wiring in server.py."
        )
    server.version = version


def _configure_logging() -> None:
    """Pin logging to stderr; our own startup lines at INFO, libraries at WARNING.

    Runs before the FastMCP app exists: FastMCP's constructor calls
    logging.basicConfig with its own (stderr-bound, rich) handler, and
    basicConfig is a no-op once the root logger already has a handler — so
    claiming root first leaves exactly one predictable handler in place.

    Idempotent: main() may run more than once in a single process (tests,
    embedding, re-entry). Our handler is tagged so a second call reuses it
    instead of stacking another and duplicating every line.
    """
    root = logging.getLogger()
    if not any(getattr(h, _OUR_HANDLER_MARK, False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
        setattr(handler, _OUR_HANDLER_MARK, True)
        root.addHandler(handler)
    root.setLevel(logging.WARNING)
    logging.getLogger("blue_prism_v7_mcp").setLevel(logging.INFO)


def main() -> None:
    """Console entrypoint declared in pyproject [project.scripts]."""
    _configure_logging()
    try:
        app = build_server(BPConfig.from_env())
    except _STARTUP_ERRORS as exc:
        # Still fail-loud — exit code 1 with the full reason on stderr — but a
        # misconfiguration is an operator message, not a stack trace.
        raise SystemExit(f"{SERVER_NAME}: {exc}") from exc
    app.run()  # stdio transport: JSON-RPC over stdout from here on

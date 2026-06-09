"""BPClient — Blue Prism v7 REST client (THE extraction core).

Phase 1 ports `data/bp_api_provider.py` from the dashboard repo and performs the
Streamlit-ectomy:
    - delete `import streamlit`
    - `@st.cache_data(ttl=30)`     -> an instance-level TTL cache
    - auth token in session_state  -> instance state on this object
    - module-level config globals  -> the injected BPConfig

Phase 2 extends it beyond what the dashboard hit (resources/queues/schedules/
sessions) to the reusable surface: queue-ITEM listing, /processes, session
stage-log, and (Tier 3, gated) the write endpoints.

TODO(Phase 1): implement. Skeleton below records the intended public surface.
"""
from __future__ import annotations

from .config import BPConfig


class BPClient:
    """Stateful client for one Blue Prism v7 estate.

    Holds the injected config, the bearer token, and a per-instance TTL cache.
    Read methods mirror the v7 entities; write methods (Phase 2/5) are only
    surfaced as MCP tools when config.enable_actions is True.
    """

    def __init__(self, config: BPConfig) -> None:
        self._config = config
        # TODO(Phase 1): requests.Session, token state, TTL cache.

    # --- Tier 1 reads (Phase 1-2) -----------------------------------------
    # def get_resources(self) -> list[dict]: ...
    # def get_queues(self) -> list[dict]: ...
    # def get_queue_items(self, queue, status, start, end) -> list[dict]: ...
    # def get_sessions(self, start=None, end=None) -> list[dict]: ...
    # def get_session_log(self, session_id) -> list[dict]: ...   # Phase 2, PII vector
    # def get_schedules(self) -> list[dict]: ...
    # def get_processes(self) -> list[dict]: ...                 # Phase 2

    # --- Tier 3 writes (Phase 2/5, gated) ---------------------------------
    # def retry_queue_item(self, ...): ...
    # def start_process(self, ...): ...
    # def set_schedule_enabled(self, ...): ...

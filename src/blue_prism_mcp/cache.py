"""The read cache behind a small protocol, with a thread-safe default.

The MCP server runs one `BPClient` per stdio process, so the in-process
`TTLCache` is all it needs. But the embeddable core (DESIGN Phase 8) is meant to
be embedded in a long-lived, multi-threaded host that shares one client across
worker threads — so the cache sits behind a `Cache` protocol the host can
implement (e.g. a shared/Redis-backed store), and the default implementation is
itself thread-safe. No read behaviour changes: same per-key TTL, same "absent vs
cached falsy" distinction via the `MISS` sentinel.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol, runtime_checkable

# Cache sentinel — distinguishes "absent" from a cached falsy value (e.g. []).
# The client compares the result of `get` against this, so it is shared here
# rather than re-defined per implementation.
MISS = object()


@runtime_checkable
class Cache(Protocol):
    """The minimal contract `BPClient` needs from a read cache.

    `get` returns the cached value or the module-level ``MISS`` sentinel on a
    miss (so a cached ``[]`` or ``None`` is honoured). TTL/expiry is the
    implementation's concern, not the protocol's — a host backing this with a
    store that expires keys itself satisfies the same interface.
    """

    def get(self, key: Any) -> Any: ...

    def set(self, key: Any, value: Any) -> None: ...

    def clear(self) -> None: ...


class TTLCache:
    """A tiny time-to-live cache keyed by an arbitrary hashable, thread-safe.

    The default `Cache` for one estate: don't re-hit the live API on every read
    within a short window, bound to one client instance rather than a
    process-global store. A single lock guards the whole store so concurrent
    workers sharing a client never see a half-written or torn entry (the expiry
    delete in `get` must run under the lock too).
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._store: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Any) -> Any:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return MISS
            stored_at, value = entry
            if time.monotonic() - stored_at >= self._ttl:
                # >= so an entry expires exactly at the TTL boundary, and ttl=0
                # always misses regardless of clock resolution (no stale read can
                # slip through on a tied monotonic() reading).
                del self._store[key]
                return MISS
            return value

    def set(self, key: Any, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.monotonic(), value)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

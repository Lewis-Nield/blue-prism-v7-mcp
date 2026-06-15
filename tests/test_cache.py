"""Tests for the read cache (Phase 8): the Cache protocol + thread-safe TTLCache.

The TTL semantics are also exercised end-to-end through the client
(test_client.py); these pin the cache in isolation, including the concurrency
the embeddable core adds (a long-lived multi-threaded host sharing one client).
"""

from __future__ import annotations

import threading
import time

from blue_prism_mcp.cache import MISS, Cache, TTLCache


class TestTTLCache:
    """The default in-process cache: per-key TTL, absent vs cached-falsy."""

    def test_hit_returns_the_stored_value(self):
        cache = TTLCache(ttl=30)
        cache.set("k", [1, 2, 3])
        assert cache.get("k") == [1, 2, 3]

    def test_miss_returns_the_sentinel(self):
        assert TTLCache(ttl=30).get("absent") is MISS

    def test_a_cached_falsy_value_is_honoured_not_treated_as_a_miss(self):
        # The whole point of the MISS sentinel: a cached [] must not re-fetch.
        cache = TTLCache(ttl=30)
        cache.set("empty", [])
        assert cache.get("empty") == []
        assert cache.get("empty") is not MISS

    def test_entry_expires_at_the_ttl_boundary(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("blue_prism_mcp.cache.time.monotonic", lambda: clock[0])
        cache = TTLCache(ttl=30)
        cache.set("k", "v")
        clock[0] += 29.0
        assert cache.get("k") == "v"  # still fresh
        clock[0] += 1.0  # exactly at the boundary
        assert cache.get("k") is MISS  # >= ttl expires

    def test_ttl_zero_always_misses(self, monkeypatch):
        # A tied monotonic() reading must still expire (>= boundary), so ttl=0
        # is a working "never cache" setting regardless of clock resolution.
        monkeypatch.setattr("blue_prism_mcp.cache.time.monotonic", lambda: 1000.0)
        cache = TTLCache(ttl=0)
        cache.set("k", "v")
        assert cache.get("k") is MISS

    def test_clear_drops_every_entry(self):
        cache = TTLCache(ttl=30)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert cache.get("a") is MISS and cache.get("b") is MISS

    def test_satisfies_the_cache_protocol(self):
        assert isinstance(TTLCache(ttl=30), Cache)

    def test_concurrent_set_and_get_stay_consistent(self):
        # Many threads hammering the same cache must never raise or corrupt the
        # store; the lock guards the read-modify (expiry delete) in get too.
        cache = TTLCache(ttl=30)
        errors: list[Exception] = []
        barrier = threading.Barrier(8)

        def worker(n: int) -> None:
            try:
                barrier.wait()
                for i in range(2000):
                    key = f"k{(n + i) % 16}"
                    cache.set(key, i)
                    got = cache.get(key)
                    assert got is MISS or isinstance(got, int)
            except Exception as exc:  # pragma: no cover - only fires on a real bug
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors

    def test_concurrent_clear_during_reads_does_not_raise(self):
        # A clear() racing with get()/set() must not trip a "dict changed size"
        # or KeyError — the lock serialises the whole store.
        cache = TTLCache(ttl=30)
        stop = threading.Event()
        errors: list[Exception] = []

        def churn() -> None:
            try:
                while not stop.is_set():
                    cache.set("k", 1)
                    cache.get("k")
            except Exception as exc:  # pragma: no cover - only fires on a real bug
                errors.append(exc)

        def clearer() -> None:
            try:
                while not stop.is_set():
                    cache.clear()
            except Exception as exc:  # pragma: no cover - only fires on a real bug
                errors.append(exc)

        threads = [threading.Thread(target=churn) for _ in range(4)] + [
            threading.Thread(target=clearer)
        ]
        for t in threads:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join()
        assert not errors

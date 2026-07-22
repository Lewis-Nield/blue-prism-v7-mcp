"""Tests for transport governance (DESIGN Phase 12).

The deterministic core of the phase lives here: the token bucket's refill
arithmetic, the backoff calculation, the retry decision table, and the counter
contract a consuming host's health surface depends on. Everything with a clock
in it takes an injected one, so the assertions are exact rather than timing-
tolerant — the exceptions are the FIFO and concurrency tests, which are about
real threads and say so.
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from blue_prism_v7_mcp.transport import (
    RateLimiter,
    RequestCounters,
    RetryPolicy,
    TokenBucket,
    TransportBudgetExceeded,
    backoff_delay,
    normalise_path,
    retry_after_seconds,
)


class FakeClock:
    """A manually advanced monotonic clock."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# --- TokenBucket --------------------------------------------------------------


class TestTokenBucketArithmetic:
    """Refill and burst, pinned exactly under an injected clock."""

    def test_rejects_non_positive_rate(self):
        with pytest.raises(ValueError, match="rate must be positive"):
            TokenBucket(0, 5)
        with pytest.raises(ValueError, match="rate must be positive"):
            TokenBucket(-1.0, 5)

    def test_rejects_burst_below_one(self):
        with pytest.raises(ValueError, match="burst must be at least 1"):
            TokenBucket(1.0, 0)

    def test_starts_full_so_an_idle_client_has_its_headroom(self):
        clock = FakeClock()
        bucket = TokenBucket(1.0, 4, clock=clock)
        assert [bucket.acquire(0.0) for _ in range(4)] == [True] * 4
        assert bucket.acquire(0.0) is False

    def test_refills_at_the_configured_rate(self):
        clock = FakeClock()
        bucket = TokenBucket(2.0, 4, clock=clock)
        for _ in range(4):
            bucket.acquire(0.0)

        clock.now = 1.0  # one second at 2/s
        assert bucket.acquire(0.0) is True
        assert bucket.acquire(0.0) is True
        assert bucket.acquire(0.0) is False

    def test_refill_is_capped_at_burst(self):
        clock = FakeClock()
        bucket = TokenBucket(2.0, 4, clock=clock)
        for _ in range(4):
            bucket.acquire(0.0)

        clock.now = 1000.0  # long idle: 2000 tokens' worth of elapsed time
        assert sum(bucket.acquire(0.0) for _ in range(10)) == 4

    def test_partial_refill_does_not_grant_a_whole_token(self):
        clock = FakeClock()
        bucket = TokenBucket(1.0, 1, clock=clock)
        assert bucket.acquire(0.0) is True

        clock.now = 0.9
        assert bucket.acquire(0.0) is False
        clock.now = 1.0
        assert bucket.acquire(0.0) is True

    def test_a_clock_that_does_not_advance_never_removes_tokens(self):
        # Guards the elapsed<=0 branch: a frozen (or momentarily non-monotonic)
        # clock must not be able to drain the bucket.
        clock = FakeClock(now=5.0)
        bucket = TokenBucket(1.0, 3, clock=clock)
        assert bucket.acquire(0.0) is True
        clock.now = 4.0  # backwards
        assert bucket.acquire(0.0) is True
        assert bucket.acquire(0.0) is True

    def test_satisfies_the_rate_limiter_protocol(self):
        assert isinstance(TokenBucket(1.0, 1), RateLimiter)


class TestTokenBucketBlocking:
    """Real threads and real elapsed time — kept small but genuinely concurrent."""

    def test_blocks_until_a_token_accrues_then_grants(self):
        bucket = TokenBucket(rate=50.0, burst=1)  # a token every 20ms
        assert bucket.acquire(0.0) is True  # spend the initial one
        started = time.monotonic()
        assert bucket.acquire(1.0) is True
        assert time.monotonic() - started >= 0.015

    def test_returns_false_when_the_wait_budget_is_spent(self):
        bucket = TokenBucket(rate=1.0, burst=1)
        assert bucket.acquire(0.0) is True
        started = time.monotonic()
        assert bucket.acquire(0.05) is False
        # It waited rather than failing instantly, and did not wait the full
        # second it would have needed for the next token.
        assert 0.04 <= time.monotonic() - started < 0.9

    def test_grants_in_arrival_order(self):
        # FIFO is the point: without it a busy caller can starve one that has
        # been waiting, which under contention is the request most likely to be
        # a person waiting on a page.
        bucket = TokenBucket(rate=20.0, burst=1)  # a token every 50ms
        assert bucket.acquire(0.0) is True
        granted: list[int] = []
        lock = threading.Lock()

        def waiter(n: int) -> None:
            if bucket.acquire(5.0):
                with lock:
                    granted.append(n)

        threads = []
        for n in range(3):
            t = threading.Thread(target=waiter, args=(n,))
            t.start()
            threads.append(t)
            time.sleep(0.015)  # stagger so arrival order is unambiguous
        for t in threads:
            t.join()

        assert granted == [0, 1, 2]

    def test_a_timed_out_waiter_does_not_wedge_the_queue(self):
        # The head of the queue giving up must leave it and wake the next, or
        # everyone behind waits forever.
        bucket = TokenBucket(rate=25.0, burst=1)  # a token every 40ms
        assert bucket.acquire(0.0) is True
        results: dict[str, bool] = {}

        def quitter() -> None:
            results["quitter"] = bucket.acquire(0.01)  # gives up first

        def patient() -> None:
            results["patient"] = bucket.acquire(5.0)

        first = threading.Thread(target=quitter)
        first.start()
        time.sleep(0.005)
        second = threading.Thread(target=patient)
        second.start()
        first.join()
        second.join()

        assert results == {"quitter": False, "patient": True}

    def test_concurrent_acquire_never_over_grants(self):
        # 8 threads racing for a bucket that can only ever yield 4 tokens in the
        # window: the arithmetic must hold under contention, not just serially.
        bucket = TokenBucket(rate=0.001, burst=4)  # refill is effectively nil
        granted: list[bool] = []
        lock = threading.Lock()
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            got = bucket.acquire(0.0)
            with lock:
                granted.append(got)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sum(granted) == 4


# --- Backoff ------------------------------------------------------------------


class TestBackoffDelay:
    def test_window_doubles_per_attempt(self):
        # jitter()==0 pins the fixed half: base/2, base, 2*base, ...
        delays = [backoff_delay(n, base=1.0, jitter=lambda: 0.0) for n in range(4)]
        assert delays == [0.5, 1.0, 2.0, 4.0]

    def test_jitter_adds_at_most_the_other_half(self):
        assert backoff_delay(0, base=1.0, jitter=lambda: 1.0) == pytest.approx(1.0)
        assert backoff_delay(2, base=1.0, jitter=lambda: 0.5) == pytest.approx(3.0)

    def test_never_returns_zero_so_a_retry_is_always_a_real_pause(self):
        # Full jitter could retry instantly; against a struggling estate that is
        # the wrong instinct, so half the window is fixed.
        assert backoff_delay(0, base=0.5, jitter=lambda: 0.0) > 0


class TestRetryAfterSeconds:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("5", 5.0),
            (" 30 ", 30.0),
            (2, 2.0),
            ("0", 0.0),
            ("-3", 0.0),  # a past instant means "now"
            ("1.5", 1.5),
        ],
    )
    def test_numeric_forms(self, raw, expected):
        assert retry_after_seconds(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            None,
            "",
            "soon",
            "inf",
            "nan",
            object(),
        ],
    )
    def test_unusable_forms_fall_through(self, raw):
        assert retry_after_seconds(raw) is None


class TestRetryAfterHttpDate:
    """The absolute form: our clock subtracted from the estate's instant."""

    NOW = datetime(2026, 10, 21, 7, 28, 0, tzinfo=timezone.utc)

    def test_a_future_date_is_the_gap_from_now(self):
        assert retry_after_seconds("Wed, 21 Oct 2026 07:28:30 GMT", now=self.NOW) == 30.0

    def test_a_past_date_means_now_not_a_negative_sleep(self):
        assert retry_after_seconds("Wed, 21 Oct 2026 07:00:00 GMT", now=self.NOW) == 0.0

    def test_a_naive_offset_is_read_as_utc(self):
        # RFC 7231 mandates GMT; "-0000" parses naive. Reading it as anything
        # else would invent a timezone the header cannot express.
        assert retry_after_seconds("Wed, 21 Oct 2026 07:28:30 -0000", now=self.NOW) == 30.0

    def test_an_explicit_offset_is_respected(self):
        # 08:28:30 +0100 is 07:28:30 GMT — the same instant, 30s out.
        assert retry_after_seconds("Wed, 21 Oct 2026 08:28:30 +0100", now=self.NOW) == 30.0

    def test_a_skewed_estate_clock_yields_an_absurd_but_finite_gap(self):
        # This is the whole hazard: an unsynced host or a gateway writing local
        # time as GMT. The value is computed honestly here and CAPPED by
        # RetryPolicy — see TestRetryPolicy.
        assert retry_after_seconds("Wed, 21 Oct 2026 11:28:00 GMT", now=self.NOW) == 4 * 3600.0

    def test_a_real_clock_is_used_when_none_is_injected(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=120)
        stamp = format_datetime(future, usegmt=True)
        assert retry_after_seconds(stamp) == pytest.approx(120, abs=2)

    @pytest.mark.parametrize("raw", ["Wed, 99 Xxx 2026 07:28:00 GMT", "21 Oct", "GMT"])
    def test_a_malformed_date_is_not_a_date(self, raw):
        assert retry_after_seconds(raw, now=self.NOW) is None


# --- RetryPolicy --------------------------------------------------------------


class TestRetryPolicy:
    def _policy(self, max_retries=2, **kw):
        kw.setdefault("jitter", lambda: 0.0)
        kw.setdefault("base_delay", 1.0)
        kw.setdefault("max_delay", 1000.0)  # out of the way unless a test sets it
        return RetryPolicy(max_retries, **kw)

    def test_the_default_cap_is_a_minute(self):
        # Long enough to ride out a real blip, short enough that a caller (and
        # any browser request behind it) never looks hung.
        assert RetryPolicy(1, jitter=lambda: 0.0).delay_for(429, 0, "600") == 60.0

    def test_honours_retry_after_on_429(self):
        assert self._policy().delay_for(429, 0, "7") == 7.0

    def test_429_without_a_usable_retry_after_falls_back_to_backoff(self):
        assert self._policy().delay_for(429, 0, None) == 0.5
        assert self._policy().delay_for(429, 0, "later") == 0.5

    def test_honours_an_absolute_retry_after_date(self):
        stamp = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
        assert self._policy().delay_for(429, 0, stamp) == pytest.approx(20, abs=2)

    def test_an_hour_long_retry_after_is_capped_not_obeyed(self):
        # The estate gets to ask us to wait; it does not get to decide how long
        # we hang. Same ceiling whichever form it asks in.
        policy = self._policy(max_delay=60.0)
        assert policy.delay_for(429, 0, "3600") == 60.0

        skewed = format_datetime(datetime.now(timezone.utc) + timedelta(hours=4), usegmt=True)
        assert policy.delay_for(429, 0, skewed) == 60.0

    def test_a_runaway_backoff_window_is_capped_too(self):
        # base 1.0 doubling over 10 attempts would be minutes; the cap is what
        # makes max_retries safe to raise without recomputing the worst case.
        policy = self._policy(max_retries=20, max_delay=5.0)
        assert policy.delay_for(503, 10) == 5.0

    def test_a_delay_under_the_cap_is_untouched(self):
        assert self._policy(max_delay=60.0).delay_for(429, 0, "7") == 7.0
        assert self._policy(max_delay=60.0).delay_for(503, 0) == 0.5

    @pytest.mark.parametrize("status", [502, 503, 504])
    def test_transient_gateway_statuses_back_off(self, status):
        assert self._policy().delay_for(status, 0) == 0.5
        assert self._policy().delay_for(status, 1) == 1.0

    @pytest.mark.parametrize("status", [200, 201, 204, 400, 401, 403, 404, 409, 500, 501])
    def test_everything_else_is_not_retried(self, status):
        # Notably 500: as likely a bad request as a blip, and 401, which has its
        # own orthogonal re-auth path in the client.
        assert self._policy().delay_for(status, 0) is None

    def test_retry_after_is_ignored_for_non_429_statuses(self):
        # A Retry-After on a 503 is advisory; the bounded backoff is what we use.
        assert self._policy().delay_for(503, 0, "600") == 0.5

    def test_budget_is_spent_after_max_retries(self):
        policy = self._policy(max_retries=2)
        assert policy.delay_for(503, 1) is not None
        assert policy.delay_for(503, 2) is None
        assert policy.delay_for(429, 2, "1") is None

    def test_zero_max_retries_never_retries(self):
        assert self._policy(max_retries=0).delay_for(503, 0) is None

    def test_negative_max_retries_clamps_to_zero(self):
        assert self._policy(max_retries=-5).max_retries == 0

    def test_the_default_base_delay_is_half_a_second(self):
        # Pinned because it is the delay a caller gets without saying anything:
        # long enough to be a real pause, short enough not to look like a hang.
        assert RetryPolicy(1, jitter=lambda: 0.0).delay_for(503, 0) == 0.25

    def test_wait_delegates_to_the_injected_sleep(self):
        slept: list[float] = []
        self._policy(sleep=slept.append).wait(1.25)
        assert slept == [1.25]


# --- RequestCounters ----------------------------------------------------------


class TestRequestCounters:
    def test_fresh_counters_report_a_complete_zeroed_shape(self):
        # The shape is a published contract: a consumer rendering a health
        # surface must not have to default every key.
        assert RequestCounters().stats() == {
            "requests": 0,
            "token_fetches": 0,
            "retries": 0,
            "bytes_received": 0,
            "errors": {
                "rate_limited": 0,
                "client_error": 0,
                "server_error": 0,
                "timeout": 0,
                "connection_error": 0,
                "limiter_exhausted": 0,
            },
            "by_path": {},
        }

    def test_records_requests_bytes_and_paths(self):
        counters = RequestCounters()
        counters.record_request("/workqueues", status=200, bytes_received=120)
        counters.record_request("/workqueues", status=200, bytes_received=80)
        counters.record_request("/sessions", status=200)
        stats = counters.stats()

        assert stats["requests"] == 3
        assert stats["bytes_received"] == 200
        assert stats["by_path"] == {"/workqueues": 2, "/sessions": 1}
        assert sum(stats["errors"].values()) == 0

    @pytest.mark.parametrize(
        "status,bucket",
        [
            (429, "rate_limited"),
            (400, "client_error"),
            (404, "client_error"),
            (499, "client_error"),
            (500, "server_error"),
            (503, "server_error"),
        ],
    )
    def test_error_statuses_land_in_the_right_bucket(self, status, bucket):
        counters = RequestCounters()
        counters.record_request("/sessions", status=status)
        assert counters.stats()["errors"][bucket] == 1

    @pytest.mark.parametrize("status", [200, 201, 204, 302, 399])
    def test_non_error_statuses_bucket_nothing(self, status):
        counters = RequestCounters()
        counters.record_request("/sessions", status=status)
        assert sum(counters.stats()["errors"].values()) == 0

    def test_a_response_less_failure_is_recorded_by_kind(self):
        counters = RequestCounters()
        counters.record_request("/sessions", error="timeout")
        counters.record_request("/sessions", error="connection_error")
        stats = counters.stats()

        assert stats["requests"] == 2  # they were sent; they just did not land
        assert stats["errors"]["timeout"] == 1
        assert stats["errors"]["connection_error"] == 1

    def test_token_fetches_are_counted_apart_from_the_api_budget(self):
        # The auth server is a different host: token traffic must not inflate
        # the estate's request budget, but must not vanish from the report.
        counters = RequestCounters()
        counters.record_token_fetch()
        counters.record_token_fetch()
        stats = counters.stats()

        assert stats["token_fetches"] == 2
        assert stats["requests"] == 0
        assert stats["by_path"] == {}

    def test_retries_are_tallied_separately(self):
        counters = RequestCounters()
        counters.record_retry()
        assert counters.stats()["retries"] == 1

    def test_budget_exhaustion_is_an_error_but_not_a_request(self):
        # Nothing was sent, so "requests" and "by_path" stay in step with the
        # load the estate actually saw.
        counters = RequestCounters()
        counters.record_budget_exhausted()
        counters.record_budget_exhausted()
        counters.record_budget_exhausted()
        stats = counters.stats()

        # Accumulates: a host watching this bucket climb is watching its budget
        # get spent, so a flat "1" would hide the whole signal.
        assert stats["errors"]["limiter_exhausted"] == 3
        assert stats["requests"] == 0
        assert stats["by_path"] == {}

    def test_stats_returns_a_copy_a_caller_cannot_write_through(self):
        counters = RequestCounters()
        counters.record_request("/sessions", status=200)
        stats = counters.stats()
        stats["requests"] = 999
        stats["errors"]["server_error"] = 999
        stats["by_path"]["/sessions"] = 999

        assert counters.stats()["requests"] == 1
        assert counters.stats()["errors"]["server_error"] == 0
        assert counters.stats()["by_path"] == {"/sessions": 1}

    def test_concurrent_recording_loses_nothing(self):
        counters = RequestCounters()
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            for _ in range(500):
                counters.record_request("/sessions", status=500, bytes_received=1)
                counters.record_token_fetch()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        stats = counters.stats()
        assert stats["requests"] == 4000
        assert stats["token_fetches"] == 4000
        assert stats["bytes_received"] == 4000
        assert stats["errors"]["server_error"] == 4000
        assert stats["by_path"] == {"/sessions": 4000}


# --- Path templating ----------------------------------------------------------


class TestNormalisePath:
    @pytest.mark.parametrize(
        "path,expected",
        [
            # UUID ids — every entity but schedules.
            (
                "/sessions/3f2504e0-4f89-11d3-9a0c-0305e82c3301/logs",
                "/sessions/{id}/logs",
            ),
            (
                "/workqueues/3F2504E0-4F89-11D3-9A0C-0305E82C3301/items",
                "/workqueues/{id}/items",
            ),
            # Integer ids — schedules, and attempt numbers.
            ("/schedules/7/tasks", "/schedules/{id}/tasks"),
            ("/scheduleLogs/1042", "/scheduleLogs/{id}"),
            (
                "/workqueues/3f2504e0-4f89-11d3-9a0c-0305e82c3301/items/"
                "3f2504e0-4f89-11d3-9a0c-0305e82c3302/attempts/3",
                "/workqueues/{id}/items/{id}/attempts/{id}",
            ),
            # Literal segments are never mistaken for ids.
            ("/workqueues", "/workqueues"),
            ("/workqueues/configurations", "/workqueues/configurations"),
            ("/resources/pools", "/resources/pools"),
            ("/schedules/tasks/9/sessions", "/schedules/tasks/{id}/sessions"),
            # A near-miss UUID (too short) stays literal rather than collapsing.
            (
                "/sessions/3f2504e0-4f89-11d3-9a0c-0305e82c33",
                "/sessions/3f2504e0-4f89-11d3-9a0c-0305e82c33",
            ),
        ],
    )
    def test_id_segments_collapse(self, path, expected):
        assert normalise_path(path) == expected


def test_budget_exceeded_is_not_an_http_error():
    # Deliberately not a requests.RequestException: nothing was sent, so a
    # handler treating it as an HTTP failure would be misreading it.
    import requests

    assert issubclass(TransportBudgetExceeded, RuntimeError)
    assert not issubclass(TransportBudgetExceeded, requests.RequestException)

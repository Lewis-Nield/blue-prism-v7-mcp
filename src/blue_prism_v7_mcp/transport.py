"""Transport governance — the mechanism that bounds this client's load on v7.

The Blue Prism application server hosting the v7 REST API is the same host
serving interactive Control Room clients, the scheduler, and the runtime
resources' own connections. A tool that degrades the estate it monitors is worse
than a tool that is simply down, so the client needs a ceiling it can be held to
— and, just as importantly, a way to *report* the load it actually emitted.

This module is all mechanism and no policy: the engine ships the levers, the
deployment supplies the numbers (see BPConfig, where every knob defaults to
"off" so an unconfigured server behaves exactly as it did before this existed).

    RateLimiter / TokenBucket   — bound the request RATE
    RetryPolicy                 — bounded, jittered retry of transient failures
    RequestCounters             — what was actually emitted, for a health surface
    TransportBudgetExceeded     — raised when the budget is spent; never silent

Deliberately NOT here: the in-flight concurrency semaphore and the connection
pool sizing, which are plain `threading`/`requests` primitives owned by BPClient
because they have no policy of their own to express.

Note the name: `governance.py` in this package already means the Tier 3
capability gate and audit log. That is *authorisation* governance; this is
*transport* governance, and the two are unrelated.
"""

from __future__ import annotations

import random
import re
import threading
import time
from collections import deque
from typing import Any, Callable, Protocol, runtime_checkable

# The v7 API's ids are UUIDs (schedules being the one integer exception), so a
# raw request path is unbounded in cardinality — "/sessions/<uuid>/logs" would
# make a per-path tally useless as a load signal. Path segments that look like
# an id collapse to a template before they are counted.
_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Error buckets reported by RequestCounters.stats(). Fixed and always present
# (zeros included) so a consumer's health surface can render a stable shape
# without defaulting every key — this dict is a published contract.
_ERROR_KINDS = (
    "rate_limited",  # 429 — the estate itself pushing back
    "client_error",  # other 4xx
    "server_error",  # 5xx
    "timeout",
    "connection_error",
    "limiter_exhausted",  # our own budget, spent before a request was sent
)


class TransportBudgetExceeded(RuntimeError):
    """The configured wait for a request slot elapsed before one was free.

    Deliberately NOT a ``requests.RequestException``: nothing was sent, so an
    error handler that treats this as an HTTP failure would be misreading it.
    The contract (DESIGN Phase 12) is block, then fail visibly — never drop a
    request, never queue unboundedly — so a caller degrades through the same
    path it already uses for an unreachable estate.
    """


@runtime_checkable
class RateLimiter(Protocol):
    """The minimal contract BPClient needs from a rate limiter.

    `acquire` blocks for at most `timeout` seconds and returns True if a request
    slot was granted, False if the wait elapsed first. Mirrors the `Cache`
    protocol precedent: the default is good enough for one process, but a host
    running several can substitute a shared implementation without forking the
    client.
    """

    def acquire(self, timeout: float) -> bool: ...


class TokenBucket:
    """A thread-safe, FIFO token bucket — one shared budget per client.

    Tokens accrue at `rate` per second up to a ceiling of `burst`, and each
    request spends one. That shape is chosen because it separates the two things
    a steady poller and a browsing human do to an estate: `rate` is the
    sustained ceiling, `burst` is how much idle headroom a sudden fan-out may
    cash in at once.

    FIFO matters. A plain lock would let a busy caller starve one that has been
    waiting, which under contention is precisely the request most likely to be a
    user staring at a page. Waiters therefore take a ticket and only the head of
    the queue may spend a token; a waiter that times out leaves the queue and
    wakes the next.

    `clock` is injectable so a test can assert refill and burst arithmetic
    exactly. Note the blocking wait still uses a real condition variable — an
    injected clock is for non-blocking probes (`acquire(0.0)`), not for
    fast-forwarding a thread that is genuinely parked.
    """

    def __init__(
        self,
        rate: float,
        burst: int,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"TokenBucket rate must be positive, got {rate!r}")
        if burst < 1:
            raise ValueError(f"TokenBucket burst must be at least 1, got {burst!r}")
        self._rate = float(rate)
        self._burst = float(burst)
        self._clock = clock
        # Start full: a client that has been idle since construction has, by
        # definition, spent none of its budget.
        self._tokens = float(burst)
        self._updated = clock()
        self._cv = threading.Condition()
        self._queue: deque[int] = deque()
        self._next_ticket = 0

    def _refill(self) -> None:
        """Accrue tokens for the elapsed time. Caller holds the condition."""
        now = self._clock()
        elapsed = now - self._updated
        if elapsed <= 0:
            # A non-monotonic or frozen injected clock must never *remove*
            # tokens, and must not move the mark backwards.
            return
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._updated = now

    def acquire(self, timeout: float) -> bool:
        """Spend one token, waiting up to `timeout` seconds. True if granted."""
        with self._cv:
            ticket = self._next_ticket
            self._next_ticket += 1
            self._queue.append(ticket)
            deadline = self._clock() + max(timeout, 0.0)
            try:
                while True:
                    needed: float | None = None
                    if self._queue[0] == ticket:
                        self._refill()
                        if self._tokens >= 1.0:
                            self._tokens -= 1.0
                            self._queue.popleft()
                            # Wake the new head so it can spend or re-park.
                            self._cv.notify_all()
                            return True
                        # Head of the queue: park exactly long enough for the
                        # next whole token rather than polling for it.
                        needed = (1.0 - self._tokens) / self._rate
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        return False
                    self._cv.wait(remaining if needed is None else min(needed, remaining))
            finally:
                # Success already dequeued; this is the timeout/exception path,
                # where leaving the ticket behind would wedge every waiter
                # queued after it.
                try:
                    self._queue.remove(ticket)
                except ValueError:
                    pass
                else:
                    self._cv.notify_all()


def backoff_delay(attempt: int, *, base: float, jitter: Callable[[], float]) -> float:
    """Equal-jitter exponential backoff for retry `attempt` (0-based), seconds.

    The window doubles per attempt; half of it is fixed and half is random.
    Full jitter (a uniform draw over the whole window) spreads a synchronised
    herd better but can retry almost immediately, which against an estate that
    is already struggling is the wrong instinct — the fixed half guarantees the
    backoff is real. `jitter` returns a float in [0, 1) and is injectable so the
    calculation is deterministic under test.
    """
    window = base * (2**attempt)
    return window / 2 + jitter() * (window / 2)


def retry_after_seconds(value: Any) -> float | None:
    """Parse a `Retry-After` header value as seconds, or None if unusable.

    RFC 7231 allows either a delay in seconds or an HTTP-date. Only the numeric
    form is honoured: the date form needs the server's clock to agree with ours,
    and a skewed estate clock could park a caller for hours. An unparseable (or
    date-form) value falls through to the calculated backoff, which is bounded
    by construction.
    """
    if value is None:
        return None
    try:
        seconds = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return max(seconds, 0.0)


class RetryPolicy:
    """Bounded retry of the failures that are worth retrying, and only those.

    Retried: 429 (honouring `Retry-After` when it is a plain number) and the
    transient gateway statuses 502/503/504. Not retried: anything else — a 500
    is as likely to be a bad request as a blip, and a 404 will still be a 404.

    The 401 re-auth in `BPClient._request` is orthogonal and is NOT this: it is
    a single in-band token refresh, it is not counted here, and it happens
    inside each attempt this policy makes.

    `sleep` and `jitter` are injectable so the whole policy is testable without
    real elapsed time.
    """

    #: Statuses retried with calculated backoff (429 is handled separately so it
    #: can honour the server's own instruction first).
    TRANSIENT_STATUSES = frozenset({502, 503, 504})

    def __init__(
        self,
        max_retries: int,
        *,
        base_delay: float = 0.5,
        jitter: Callable[[], float] = random.random,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_retries = max(int(max_retries), 0)
        self._base_delay = base_delay
        self._jitter = jitter
        self._sleep = sleep

    def delay_for(self, status: int, attempt: int, retry_after: Any = None) -> float | None:
        """Seconds to wait before retry `attempt` (0-based), or None to give up.

        None means "this is not retriable" — either the status does not warrant
        it or the retry budget is spent. The caller then surfaces the response
        as-is, which for an error status means `raise_for_status`.
        """
        if attempt >= self.max_retries:
            return None
        if status == 429:
            stated = retry_after_seconds(retry_after)
            if stated is not None:
                return stated
        elif status not in self.TRANSIENT_STATUSES:
            return None
        return backoff_delay(attempt, base=self._base_delay, jitter=self._jitter)

    def wait(self, delay: float) -> None:
        self._sleep(delay)


class RequestCounters:
    """Cumulative tally of what this client emitted, since construction.

    This exists so a deployment's load budget can be *measured* rather than
    asserted — a host surfaces `stats()` on its health endpoint and the claim
    "we are a minority of API load" becomes checkable. The returned dict is a
    published contract: keys are stable, every error bucket is always present
    (zeros included), and the value is a fresh copy so a caller cannot mutate
    the counters by holding onto it.

        {
          "requests":       int,   # requests SENT to the API host
          "token_fetches":  int,   # POSTs to the auth server (a different host)
          "retries":        int,   # retried sends, also counted in "requests"
          "bytes_received": int,   # response bodies, where materialised
          "errors":         {kind: int, ...},   # keys per _ERROR_KINDS
          "by_path":        {path_template: int, ...},
        }

    `token_fetches` is deliberately separate (DESIGN Phase 12): the
    Authentication Server is a different service from the API host, so token
    traffic must not inflate the API budget — but it must not vanish from the
    report either. It counts POSTs issued, whatever their outcome; an auth
    failure surfaces as a raised exception, not a bucket here.

    `limiter_exhausted` is the one error bucket that does NOT correspond to a
    sent request, so it is absent from `requests` and `by_path`: those two stay
    in step with each other, and with the load the estate actually saw.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._requests = 0
        self._token_fetches = 0
        self._retries = 0
        self._bytes = 0
        self._errors = dict.fromkeys(_ERROR_KINDS, 0)
        self._by_path: dict[str, int] = {}

    def record_request(
        self,
        path_template: str,
        *,
        status: int | None = None,
        error: str | None = None,
        bytes_received: int = 0,
    ) -> None:
        """Record one sent request, classifying its outcome.

        Pass `error` for a failure with no HTTP response (a timeout or a refused
        connection); otherwise pass `status` and the bucket is derived from it.
        """
        kind = error if error is not None else _classify(status)
        with self._lock:
            self._requests += 1
            self._bytes += bytes_received
            self._by_path[path_template] = self._by_path.get(path_template, 0) + 1
            if kind is not None:
                self._errors[kind] += 1

    def record_token_fetch(self) -> None:
        with self._lock:
            self._token_fetches += 1

    def record_retry(self) -> None:
        with self._lock:
            self._retries += 1

    def record_budget_exhausted(self) -> None:
        """A request that was never sent because the transport budget ran out."""
        with self._lock:
            self._errors["limiter_exhausted"] += 1

    def stats(self) -> dict[str, Any]:
        """A snapshot of the counters — see the class docstring for the shape."""
        with self._lock:
            return {
                "requests": self._requests,
                "token_fetches": self._token_fetches,
                "retries": self._retries,
                "bytes_received": self._bytes,
                "errors": dict(self._errors),
                "by_path": dict(self._by_path),
            }


def _classify(status: int | None) -> str | None:
    """Map an HTTP status to an error bucket, or None if it isn't an error."""
    if status is None or status < 400:
        return None
    if status == 429:
        return "rate_limited"
    if status < 500:
        return "client_error"
    return "server_error"


def normalise_path(path: str) -> str:
    """Collapse id-shaped path segments to `{id}` for per-path tallies.

    `/workqueues/<uuid>/items` and `/schedules/7/tasks` become
    `/workqueues/{id}/items` and `/schedules/{id}/tasks`, so the tally counts
    endpoints rather than entities. Only UUIDs and bare integers are collapsed —
    the API's two id shapes — so a literal segment like
    `/workqueues/configurations` is never mistaken for one.
    """
    return "/".join("{id}" if _is_id_segment(seg) else seg for seg in path.split("/"))


def _is_id_segment(segment: str) -> bool:
    return bool(segment) and (segment.isdigit() or _UUID_RE.match(segment) is not None)

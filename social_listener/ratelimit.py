"""A token bucket that steers by Reddit's own rate-limit headers.

The specification (§3.3) is emphatic about this: Reddit's archived developer
wiki still says 60 requests/minute while current policy is 100 QPM averaged
over a 10-minute window. Any hard-coded number will eventually be wrong, so the
authority here is the live response headers:

    X-Ratelimit-Used        requests consumed this period
    X-Ratelimit-Remaining   requests left this period
    X-Ratelimit-Reset       seconds until the period resets

The configured QPM is a cold-start fallback, used only until the first response
teaches us the real budget. A policy change then degrades throughput instead of
getting the client banned.

This implementation is in-process and thread-safe, which is correct for a single
worker. §14 calls for a Redis-backed bucket once more than one worker runs --
the interface here is deliberately the one a Redis version would expose, so the
swap touches this file only.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from .config import settings


@dataclass
class BudgetState:
    """What the last response told us about our remaining budget."""

    used: float | None = None
    remaining: float | None = None
    reset_seconds: float | None = None
    observed_at: float | None = None

    @property
    def is_known(self) -> bool:
        return self.remaining is not None and self.observed_at is not None


class RateLimiter:
    def __init__(
        self,
        qpm: int | None = None,
        headroom: float | None = None,
        *,
        clock=time.monotonic,
        sleeper=time.sleep,
    ) -> None:
        self.fallback_qpm = qpm if qpm is not None else settings.rate_limit_qpm
        self.headroom = headroom if headroom is not None else settings.rate_limit_headroom
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._state = BudgetState()
        self._last_request_at: float | None = None
        self.calls_made = 0
        self.waits = 0
        self.total_wait_seconds = 0.0

    @property
    def effective_qpm(self) -> float:
        """Requests per minute we are willing to spend, after headroom."""
        return self.fallback_qpm * (1.0 - self.headroom)

    @property
    def state(self) -> BudgetState:
        return self._state

    def _min_interval(self) -> float:
        """Seconds to leave between requests.

        When the headers have told us the real budget, pace to spend what
        remains evenly across the rest of the window rather than burning it and
        stalling. Otherwise fall back to the configured QPM.
        """
        state = self._state
        if state.is_known and state.reset_seconds and state.remaining is not None:
            elapsed = self._clock() - (state.observed_at or self._clock())
            window_left = max(state.reset_seconds - elapsed, 0.0)
            spendable = state.remaining * (1.0 - self.headroom)
            if spendable <= 0:
                # Budget exhausted: wait out the window.
                return max(window_left, 1.0)
            if window_left > 0:
                return window_left / spendable
        return 60.0 / max(self.effective_qpm, 1.0)

    def acquire(self) -> float:
        """Block until a request may be made. Returns seconds waited."""
        with self._lock:
            now = self._clock()
            waited = 0.0
            if self._last_request_at is not None:
                gap = now - self._last_request_at
                needed = self._min_interval() - gap
                if needed > 0:
                    self._sleep(needed)
                    waited = needed
                    self.waits += 1
                    self.total_wait_seconds += needed
                    now = self._clock()
            self._last_request_at = now
            self.calls_made += 1
            return waited

    def observe(self, headers) -> None:
        """Update the budget from a response's rate-limit headers."""
        used = _header_float(headers, "x-ratelimit-used")
        remaining = _header_float(headers, "x-ratelimit-remaining")
        reset = _header_float(headers, "x-ratelimit-reset")
        if remaining is None and used is None and reset is None:
            return
        with self._lock:
            self._state = BudgetState(
                used=used,
                remaining=remaining,
                reset_seconds=reset,
                observed_at=self._clock(),
            )

    def backoff_seconds(self, attempt: int) -> float:
        """Delay before retry `attempt` (1-based) after a 429.

        Honours the reset hint when we have one; exponential from 2s otherwise,
        as §3.4 specifies.
        """
        state = self._state
        if state.reset_seconds:
            return max(float(state.reset_seconds), 2.0)
        return min(2.0 ** attempt, 64.0)


def _header_float(headers, name: str) -> float | None:
    if headers is None:
        return None
    try:
        raw = headers.get(name)
    except AttributeError:
        return None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None

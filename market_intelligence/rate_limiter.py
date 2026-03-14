"""Token bucket rate limiter for exchange API calls."""
from __future__ import annotations

import asyncio
import os
import time
from typing import Dict


class TokenBucketRateLimiter:
    """Async-safe token bucket rate limiter.

    Tokens refill at a fixed rate. If no tokens are available,
    ``acquire()`` awaits until one becomes available rather than
    dropping the request.
    """

    def __init__(self, rate: float, burst: int | None = None):
        """
        Args:
            rate: Maximum sustained requests per second.
            burst: Maximum burst size (defaults to ``int(rate)``).
        """
        self._rate = max(0.1, rate)
        self._burst = burst or max(1, int(rate))
        self._tokens = float(self._burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        async with self._lock:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            # Calculate wait time for next token.
            deficit = 1.0 - self._tokens
            wait = deficit / self._rate
        await asyncio.sleep(wait)
        async with self._lock:
            self._refill()
            self._tokens = max(0.0, self._tokens - 1.0)


class ExchangeRateLimiters:
    """Per-exchange rate limiters loaded from env vars."""

    def __init__(self) -> None:
        self._limiters: Dict[str, TokenBucketRateLimiter] = {}

    def get(self, exchange: str) -> TokenBucketRateLimiter:
        if exchange not in self._limiters:
            env_key = f"MI_RATE_LIMIT_{exchange.upper()}"
            rate = float(os.getenv(env_key, "10"))
            self._limiters[exchange] = TokenBucketRateLimiter(rate)
        return self._limiters[exchange]


_global_limiters: ExchangeRateLimiters | None = None


def get_exchange_rate_limiters() -> ExchangeRateLimiters:
    global _global_limiters
    if _global_limiters is None:
        _global_limiters = ExchangeRateLimiters()
    return _global_limiters

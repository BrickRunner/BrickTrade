"""
Per-exchange async rate limiter with 429 handling.

Uses a token-bucket algorithm:
- Each exchange gets a configurable request rate (requests/sec).
- Before every request, the caller awaits `acquire()` which sleeps
  if the bucket is empty.
- On HTTP 429, `record_429()` triggers exponential backoff that
  temporarily pauses ALL requests to that exchange.

Thread/task-safe via asyncio.Lock per bucket.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict

logger = logging.getLogger("trading_system")

# Sensible defaults per exchange (requests per second).
# Official documented limits (conservative to leave headroom):
#   OKX:     20 req/s (public), 10 req/s (private)
#   Bybit:   20 req/s (public), 10 req/s (private)
#   Binance: 10 req/s (order), 20 req/s (public)
#   HTX:     10 req/s (private)
DEFAULT_RATES: Dict[str, float] = {
    "okx": 15.0,
    "bybit": 15.0,
    "binance": 15.0,
    "htx": 8.0,
}

# Max backoff after repeated 429s
MAX_BACKOFF_SECONDS = 60.0


@dataclass
class _Bucket:
    """Token bucket for a single exchange."""
    rate: float                          # tokens (requests) per second
    tokens: float = 0.0
    max_tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # 429 backoff state
    backoff_until: float = 0.0           # monotonic time
    consecutive_429: int = 0
    base_backoff: float = 1.0            # initial backoff (seconds)

    def __post_init__(self):
        if self.max_tokens == 0.0:
            self.max_tokens = max(self.rate * 2, 5.0)  # burst capacity
        self.tokens = self.max_tokens

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
        self.last_refill = now


class ExchangeRateLimiter:
    """Async rate limiter that manages per-exchange request budgets."""

    def __init__(self, custom_rates: Dict[str, float] | None = None):
        self._rates = dict(DEFAULT_RATES)
        if custom_rates:
            self._rates.update(custom_rates)
        self._buckets: Dict[str, _Bucket] = {}

    def _get_bucket(self, exchange: str) -> _Bucket:
        key = exchange.lower()
        if key not in self._buckets:
            rate = self._rates.get(key, 10.0)
            self._buckets[key] = _Bucket(rate=rate)
        return self._buckets[key]

    async def acquire(self, exchange: str) -> None:
        """Wait until a request token is available for *exchange*.

        If the exchange is in 429 backoff, waits for backoff to expire first.
        FIX CRITICAL #11: Sleep OUTSIDE the lock to avoid serializing all requests
        to a single exchange while one task is waiting for backoff or refill.
        """
        bucket = self._get_bucket(exchange)

        # Step 1: Check backoff and refill under lock (fast)
        wait_backoff = 0.0
        wait_refill = 0.0
        async with bucket.lock:
            now = time.monotonic()
            if bucket.backoff_until > now:
                wait_backoff = bucket.backoff_until - now
            else:
                bucket._refill()
                if bucket.tokens < 1.0:
                    wait_refill = (1.0 - bucket.tokens) / bucket.rate
                else:
                    bucket.tokens -= 1.0

        # Step 2: Sleep OUTSIDE lock
        if wait_backoff > 0:
            logger.warning(
                "rate_limiter: %s in 429 backoff, waiting %.1fs",
                exchange, wait_backoff,
            )
            await asyncio.sleep(wait_backoff)
            # After backoff, need to re-acquire and refill
            async with bucket.lock:
                bucket._refill()
                if bucket.tokens < 1.0:
                    wait_refill = (1.0 - bucket.tokens) / bucket.rate
                else:
                    bucket.tokens -= 1.0
                    return
            if wait_refill > 0:
                await asyncio.sleep(wait_refill)
            return

        if wait_refill > 0:
            await asyncio.sleep(wait_refill)
            async with bucket.lock:
                bucket._refill()
                if bucket.tokens < 1.0:
                    # Rare race: tokens got consumed by another task
                    wait_refill = (1.0 - bucket.tokens) / bucket.rate
                else:
                    bucket.tokens -= 1.0
                    return
            if wait_refill > 0:
                await asyncio.sleep(wait_refill)

    def record_429(self, exchange: str) -> float:
        """Record a 429 response. Returns the backoff duration in seconds.

        Exponential backoff: 1s, 2s, 4s, 8s ... up to MAX_BACKOFF_SECONDS.
        """
        bucket = self._get_bucket(exchange)
        bucket.consecutive_429 += 1
        backoff = min(
            bucket.base_backoff * (2 ** (bucket.consecutive_429 - 1)),
            MAX_BACKOFF_SECONDS,
        )
        bucket.backoff_until = time.monotonic() + backoff
        # Drain tokens to prevent burst after backoff
        bucket.tokens = 0.0
        logger.warning(
            "rate_limiter: 429 from %s (#%d), backoff %.1fs",
            exchange, bucket.consecutive_429, backoff,
        )
        return backoff

    def record_success(self, exchange: str) -> None:
        """Reset 429 counter on successful response."""
        bucket = self._get_bucket(exchange)
        if bucket.consecutive_429 > 0:
            bucket.consecutive_429 = 0

    def status(self) -> Dict[str, Dict]:
        """Return status of all tracked exchanges."""
        result = {}
        for name, bucket in self._buckets.items():
            now = time.monotonic()
            result[name] = {
                "rate": bucket.rate,
                "tokens": round(bucket.tokens, 1),
                "in_backoff": bucket.backoff_until > now,
                "backoff_remaining": round(max(0, bucket.backoff_until - now), 1),
                "consecutive_429": bucket.consecutive_429,
            }
        return result


# Global singleton — shared across all exchange clients
_global_limiter: ExchangeRateLimiter | None = None


def get_rate_limiter() -> ExchangeRateLimiter:
    """Get or create the global rate limiter instance."""
    global _global_limiter
    if _global_limiter is None:
        _global_limiter = ExchangeRateLimiter()
    return _global_limiter


def init_rate_limiter(custom_rates: Dict[str, float] | None = None) -> ExchangeRateLimiter:
    """Initialize the global rate limiter with custom rates."""
    global _global_limiter
    _global_limiter = ExchangeRateLimiter(custom_rates)
    return _global_limiter

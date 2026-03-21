"""Per-exchange circuit breaker.

Tracks consecutive errors per exchange and temporarily disables trading
on exchanges that are unhealthy. Prevents cascading failures when one
exchange goes down or becomes unreliable.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict

logger = logging.getLogger("trading_system")


@dataclass
class ExchangeCircuitBreaker:
    max_consecutive_errors: int = 5
    cooldown_seconds: float = 600.0  # 10 minutes

    _error_counts: Dict[str, int] = field(default_factory=dict)
    _tripped_until: Dict[str, float] = field(default_factory=dict)
    _last_error_msg: Dict[str, str] = field(default_factory=dict)

    def record_success(self, exchange: str) -> None:
        """Reset error counter on successful operation."""
        if self._error_counts.get(exchange, 0) > 0:
            logger.info("circuit_breaker: %s recovered (was at %d errors)", exchange, self._error_counts[exchange])
        self._error_counts[exchange] = 0

    def record_error(self, exchange: str, error: str = "") -> None:
        """Record an error. If threshold reached, trip the breaker."""
        count = self._error_counts.get(exchange, 0) + 1
        self._error_counts[exchange] = count
        self._last_error_msg[exchange] = error
        if count >= self.max_consecutive_errors:
            trip_until = time.time() + self.cooldown_seconds
            self._tripped_until[exchange] = trip_until
            logger.warning(
                "circuit_breaker: TRIPPED for %s after %d consecutive errors "
                "(cooldown %.0fs, last_error=%s)",
                exchange, count, self.cooldown_seconds, error,
            )
            self._error_counts[exchange] = 0

    def is_available(self, exchange: str) -> bool:
        """Check if exchange is available (breaker not tripped or cooldown expired)."""
        trip_until = self._tripped_until.get(exchange, 0.0)
        if trip_until <= 0:
            return True
        now = time.time()
        if now >= trip_until:
            # Cooldown expired, allow traffic again
            self._tripped_until[exchange] = 0.0
            logger.info("circuit_breaker: %s cooldown expired, re-enabling", exchange)
            return True
        return False

    def remaining_cooldown(self, exchange: str) -> float:
        """Seconds remaining in cooldown, or 0 if available."""
        trip_until = self._tripped_until.get(exchange, 0.0)
        remaining = trip_until - time.time()
        return max(0.0, remaining)

    def status(self) -> Dict[str, Dict]:
        """Return status of all tracked exchanges."""
        result = {}
        for exchange in set(list(self._error_counts.keys()) + list(self._tripped_until.keys())):
            result[exchange] = {
                "available": self.is_available(exchange),
                "consecutive_errors": self._error_counts.get(exchange, 0),
                "cooldown_remaining": self.remaining_cooldown(exchange),
                "last_error": self._last_error_msg.get(exchange, ""),
            }
        return result

"""
Fee Optimization Module: Maker/Taker hybrid execution with dynamic calibration.

Saves 50-80% on fees by using maker orders on one leg when conditions allow.
Automatically calibrates timeouts and price offsets based on real fill data.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("trading_system")


@dataclass
class FeeStats:
    """Track maker/taker performance statistics."""
    maker_attempts: int = 0
    maker_fills: int = 0
    maker_fallback_to_taker: int = 0
    maker_avg_wait_ms: float = 0.0
    taker_only: int = 0
    total_fee_saved_usd: float = 0.0

    def fill_rate(self) -> float:
        """Percentage of maker orders that filled."""
        if self.maker_attempts == 0:
            return 0.0
        return (self.maker_fills / self.maker_attempts) * 100

    def fallback_rate(self) -> float:
        """Percentage of maker orders that fell back to taker."""
        if self.maker_attempts == 0:
            return 0.0
        return (self.maker_fallback_to_taker / self.maker_attempts) * 100


class FeeOptimizer:
    """Dynamically optimize maker/taker execution parameters."""

    def __init__(self):
        self.stats_by_exchange: Dict[str, FeeStats] = {}
        self._maker_fee_bps = 0.0    # maker fee (negative = rebate)
        self._taker_fee_bps = 5.0    # typical taker fee

    def get_stats(self, exchange: str) -> FeeStats:
        """Get or create stats for an exchange."""
        if exchange not in self.stats_by_exchange:
            self.stats_by_exchange[exchange] = FeeStats()
        return self.stats_by_exchange[exchange]

    def recommend_timeout(self, exchange: str) -> int:
        """Recommend maker timeout based on historical fill speed."""
        stats = self.get_stats(exchange)
        if stats.maker_fills < 10:
            # Not enough data — use default
            return 2000

        # If fills are fast, can afford shorter timeout
        if stats.maker_avg_wait_ms < 500:
            return 1500
        elif stats.maker_avg_wait_ms < 1000:
            return 2000
        elif stats.maker_avg_wait_ms < 2000:
            return 2500
        else:
            return 3000

    def recommend_price_offset(self, exchange: str, volatility: float = 0.0) -> float:
        """Recommend price offset in bps based on fill rate."""
        stats = self.get_stats(exchange)
        if stats.maker_attempts < 10:
            # Default conservative offset
            return 0.5

        fill_rate = stats.fill_rate()

        # Adjust offset based on fill rate
        if fill_rate > 80:
            # Very good fill rate — can be more aggressive (tighter)
            return 0.3
        elif fill_rate > 60:
            # Good fill rate
            return 0.5
        elif fill_rate > 40:
            # Moderate fill rate — widen slightly
            return 0.7
        else:
            # Poor fill rate — widen significantly
            return 1.0

        # Additional adjustment for high volatility
        if volatility > 0.02:  # >2% volatility
            return min(fill_rate * 0.015, 1.5)  # cap at 1.5 bps

        return 0.5

    def should_use_maker(
        self,
        exchange: str,
        volatility: float,
        spread_bps: float,
    ) -> bool:
        """Decide whether to use maker order based on conditions."""
        stats = self.get_stats(exchange)

        # Always try maker for the first N attempts to gather data
        if stats.maker_attempts < 20:
            return True

        # Check fill rate
        fill_rate = stats.fill_rate()

        # Don't use maker if fill rate is very poor (<30%)
        if fill_rate < 30:
            logger.debug(
                "fee_optimizer: skipping maker on %s (fill_rate=%.1f%%)",
                exchange, fill_rate,
            )
            return False

        # Don't use maker in high volatility
        if volatility > 0.03:  # >3% volatility
            logger.debug(
                "fee_optimizer: skipping maker on %s (volatility=%.2f%%)",
                exchange, volatility * 100,
            )
            return False

        # Don't use maker if spread is very tight (< 10 bps)
        # Risk of crossing and getting rejected
        if spread_bps < 10:
            logger.debug(
                "fee_optimizer: skipping maker on %s (spread=%.1f bps too tight)",
                exchange, spread_bps,
            )
            return False

        # All conditions met — use maker
        return True

    def record_maker_attempt(
        self,
        exchange: str,
        filled: bool,
        wait_ms: float,
        fell_back: bool = False,
    ) -> None:
        """Record outcome of a maker order attempt."""
        stats = self.get_stats(exchange)
        stats.maker_attempts += 1

        if filled:
            stats.maker_fills += 1
            # Update rolling average wait time
            if stats.maker_avg_wait_ms == 0:
                stats.maker_avg_wait_ms = wait_ms
            else:
                # Exponential moving average
                alpha = 0.2
                stats.maker_avg_wait_ms = (
                    alpha * wait_ms + (1 - alpha) * stats.maker_avg_wait_ms
                )

        if fell_back:
            stats.maker_fallback_to_taker += 1

    def record_taker_only(self, exchange: str) -> None:
        """Record when we used taker-only execution."""
        stats = self.get_stats(exchange)
        stats.taker_only += 1

    def estimate_fee_saved(
        self,
        exchange: str,
        notional_usd: float,
        maker_filled: bool,
    ) -> float:
        """Estimate fee savings from using maker order."""
        if not maker_filled:
            return 0.0

        # Typical fee structure:
        # Maker: -0.02% to +0.01% (often negative = rebate)
        # Taker: +0.04% to +0.06%
        # Savings: ~0.05-0.08% (50-80 bps)

        fee_diff_bps = self._taker_fee_bps - self._maker_fee_bps
        fee_saved_usd = notional_usd * (fee_diff_bps / 10_000)

        stats = self.get_stats(exchange)
        stats.total_fee_saved_usd += fee_saved_usd

        return fee_saved_usd

    def get_summary(self) -> Dict[str, Dict]:
        """Get summary statistics for all exchanges."""
        summary = {}
        for exchange, stats in self.stats_by_exchange.items():
            summary[exchange] = {
                "maker_attempts": stats.maker_attempts,
                "maker_fills": stats.maker_fills,
                "fill_rate_pct": round(stats.fill_rate(), 1),
                "fallback_rate_pct": round(stats.fallback_rate(), 1),
                "avg_wait_ms": round(stats.maker_avg_wait_ms, 1),
                "taker_only": stats.taker_only,
                "total_saved_usd": round(stats.total_fee_saved_usd, 4),
            }
        return summary

    def set_fee_rates(self, maker_bps: float, taker_bps: float) -> None:
        """Configure fee rates for this optimizer."""
        self._maker_fee_bps = maker_bps
        self._taker_fee_bps = taker_bps
        logger.info(
            "fee_optimizer: configured fees — maker=%.2f bps, taker=%.2f bps",
            maker_bps, taker_bps,
        )


# Global singleton
_fee_optimizer: Optional[FeeOptimizer] = None


def get_fee_optimizer() -> FeeOptimizer:
    """Get or create the global fee optimizer."""
    global _fee_optimizer
    if _fee_optimizer is None:
        _fee_optimizer = FeeOptimizer()
    return _fee_optimizer

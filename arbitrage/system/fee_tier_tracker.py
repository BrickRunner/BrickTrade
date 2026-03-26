"""
Fee Tier Optimization Module:

Tracks current VIP/fee tier on each exchange and adapts strategy accordingly.

Different exchanges have tiered fee structures based on:
- 30-day trading volume
- Holdings of exchange tokens (BNB, OKB, HT)
- VIP level

This module:
1. Tracks current fee tier
2. Calculates break-even spreads for current fees
3. Recommends whether to pursue volume for tier upgrades
4. Adjusts strategy parameters based on fee structure
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, Optional

logger = logging.getLogger("trading_system")


@dataclass
class FeeTier:
    """Fee tier information for an exchange."""
    exchange: str
    tier_level: int  # 0 = retail, 1-10 = VIP levels
    maker_fee_bps: float  # Maker fee in basis points (negative = rebate)
    taker_fee_bps: float  # Taker fee in basis points
    volume_30d_usd: float  # 30-day trading volume
    next_tier_volume: Optional[float] = None  # Volume needed for next tier
    next_tier_maker_bps: Optional[float] = None
    next_tier_taker_bps: Optional[float] = None
    last_updated: datetime = field(default_factory=datetime.now)


# Typical fee structures (as of 2026)
FEE_TIERS: Dict[str, list[Dict]] = {
    "okx": [
        {"level": 0, "volume": 0, "maker": 2.0, "taker": 5.0},
        {"level": 1, "volume": 500_000, "maker": 1.5, "taker": 4.0},
        {"level": 2, "volume": 2_000_000, "maker": 1.0, "taker": 3.5},
        {"level": 3, "volume": 10_000_000, "maker": 0.5, "taker": 3.0},
        {"level": 4, "volume": 50_000_000, "maker": 0.0, "taker": 2.5},
        {"level": 5, "volume": 100_000_000, "maker": -0.5, "taker": 2.0},  # Rebate!
    ],
    "bybit": [
        {"level": 0, "volume": 0, "maker": 1.0, "taker": 6.0},
        {"level": 1, "volume": 500_000, "maker": 0.6, "taker": 5.0},
        {"level": 2, "volume": 5_000_000, "maker": 0.4, "taker": 4.0},
        {"level": 3, "volume": 25_000_000, "maker": 0.2, "taker": 3.5},
        {"level": 4, "volume": 100_000_000, "maker": 0.0, "taker": 3.0},
    ],
    "htx": [
        {"level": 0, "volume": 0, "maker": 2.0, "taker": 5.0},
        {"level": 1, "volume": 1_000_000, "maker": 1.6, "taker": 4.0},
        {"level": 2, "volume": 5_000_000, "maker": 1.2, "taker": 3.5},
        {"level": 3, "volume": 20_000_000, "maker": 0.8, "taker": 3.0},
    ],
}


class FeeTierTracker:
    """Track and optimize fee tier strategy."""

    def __init__(self):
        self.current_tiers: Dict[str, FeeTier] = {}
        self._volume_tracker: Dict[str, float] = {}  # Track cumulative volume

    async def update_tier(
        self,
        exchange: str,
        volume_30d_usd: float,
    ) -> FeeTier:
        """Update fee tier based on 30-day volume."""
        tiers = FEE_TIERS.get(exchange, [])
        if not tiers:
            # Default fees if exchange not configured
            tier = FeeTier(
                exchange=exchange,
                tier_level=0,
                maker_fee_bps=2.0,
                taker_fee_bps=5.0,
                volume_30d_usd=volume_30d_usd,
            )
            self.current_tiers[exchange] = tier
            return tier

        # Find current tier based on volume
        current_tier_data = tiers[0]
        for t in tiers:
            if volume_30d_usd >= t["volume"]:
                current_tier_data = t
            else:
                break

        # Find next tier
        next_tier_data = None
        for t in tiers:
            if t["volume"] > volume_30d_usd:
                next_tier_data = t
                break

        tier = FeeTier(
            exchange=exchange,
            tier_level=current_tier_data["level"],
            maker_fee_bps=current_tier_data["maker"],
            taker_fee_bps=current_tier_data["taker"],
            volume_30d_usd=volume_30d_usd,
            next_tier_volume=next_tier_data["volume"] if next_tier_data else None,
            next_tier_maker_bps=next_tier_data["maker"] if next_tier_data else None,
            next_tier_taker_bps=next_tier_data["taker"] if next_tier_data else None,
        )

        self.current_tiers[exchange] = tier

        if next_tier_data:
            remaining = next_tier_data["volume"] - volume_30d_usd
            logger.info(
                "[FEE_TIER] %s: Level %d (maker=%.2f bps, taker=%.2f bps), "
                "volume=%.0f, next tier in $%.0f",
                exchange, tier.tier_level, tier.maker_fee_bps, tier.taker_fee_bps,
                volume_30d_usd, remaining,
            )
        else:
            logger.info(
                "[FEE_TIER] %s: Level %d (MAX) (maker=%.2f bps, taker=%.2f bps)",
                exchange, tier.tier_level, tier.maker_fee_bps, tier.taker_fee_bps,
            )

        return tier

    def calculate_breakeven_spread(
        self,
        long_exchange: str,
        short_exchange: str,
        use_maker_on_long: bool = False,
        use_maker_on_short: bool = False,
    ) -> float:
        """Calculate minimum spread needed to break even after fees."""
        long_tier = self.current_tiers.get(long_exchange)
        short_tier = self.current_tiers.get(short_exchange)

        if not long_tier or not short_tier:
            # Default: assume taker fees both sides
            return 10.0  # 10 bps (5 bps each side)

        # Entry fees
        long_entry_fee = long_tier.maker_fee_bps if use_maker_on_long else long_tier.taker_fee_bps
        short_entry_fee = short_tier.maker_fee_bps if use_maker_on_short else short_tier.taker_fee_bps

        # Exit fees (always taker for quick exit)
        long_exit_fee = long_tier.taker_fee_bps
        short_exit_fee = short_tier.taker_fee_bps

        # Total fee cost (round-trip)
        total_fee_bps = long_entry_fee + short_entry_fee + long_exit_fee + short_exit_fee

        # Add small buffer for slippage
        breakeven_with_buffer = total_fee_bps + 2.0  # 2 bps slippage buffer

        logger.debug(
            "[BREAKEVEN] %s<->%s: entry=(%.2f+%.2f) exit=(%.2f+%.2f) → "
            "total=%.2f bps (w/buffer=%.2f)",
            long_exchange, short_exchange,
            long_entry_fee, short_entry_fee,
            long_exit_fee, short_exit_fee,
            total_fee_bps, breakeven_with_buffer,
        )

        return breakeven_with_buffer

    def should_pursue_volume(
        self,
        exchange: str,
        estimated_trades_per_day: int,
        avg_trade_size_usd: float,
    ) -> tuple[bool, str]:
        """
        Decide if we should pursue higher volume to reach next fee tier.

        Returns: (should_pursue, reason)
        """
        tier = self.current_tiers.get(exchange)
        if not tier or tier.next_tier_volume is None:
            return False, "no_next_tier"

        # How much volume needed?
        remaining_volume = tier.next_tier_volume - tier.volume_30d_usd
        if remaining_volume <= 0:
            return False, "already_at_next_tier"

        # Estimate daily volume we'd generate
        daily_volume = estimated_trades_per_day * avg_trade_size_usd * 2  # * 2 for both legs

        # How many days to reach next tier?
        days_to_next_tier = remaining_volume / daily_volume if daily_volume > 0 else 999

        # Calculate fee savings at next tier
        current_cost_bps = tier.taker_fee_bps * 2  # Round-trip
        next_cost_bps = (tier.next_tier_taker_bps or 0) * 2
        savings_bps = current_cost_bps - next_cost_bps

        if savings_bps <= 0:
            return False, "no_savings_at_next_tier"

        # Annual savings if we reach next tier
        annual_trades = estimated_trades_per_day * 365
        annual_volume = annual_trades * avg_trade_size_usd * 2
        annual_savings_usd = annual_volume * (savings_bps / 10_000)

        # Is it worth pushing for?
        # Criteria: reach within 30 days AND save >$100/year
        if days_to_next_tier <= 30 and annual_savings_usd >= 100:
            return True, (
                f"reach_in_{int(days_to_next_tier)}d_save_${int(annual_savings_usd)}/yr"
            )

        return False, f"not_worthwhile (days={int(days_to_next_tier)}, save=${int(annual_savings_usd)})"

    def adjust_min_spread_for_fees(
        self,
        base_min_spread_bps: float,
        long_exchange: str,
        short_exchange: str,
    ) -> float:
        """
        Adjust minimum spread threshold based on current fee tier.

        Lower fees → can accept tighter spreads.
        Higher fees → need wider spreads.
        """
        breakeven = self.calculate_breakeven_spread(long_exchange, short_exchange)

        # Require at least 3 bps profit over breakeven
        min_profitable_spread = breakeven + 3.0

        # Use the higher of base threshold or fee-adjusted threshold
        adjusted = max(base_min_spread_bps, min_profitable_spread)

        if adjusted != base_min_spread_bps:
            logger.debug(
                "[FEE_ADJUST] %s<->%s: base_min=%.2f bps, breakeven=%.2f bps → adjusted=%.2f bps",
                long_exchange, short_exchange, base_min_spread_bps, breakeven, adjusted,
            )

        return adjusted

    def recommend_maker_usage(
        self,
        exchange: str,
    ) -> bool:
        """
        Recommend whether to use maker orders on this exchange.

        If maker fee < 0 (rebate), definitely use maker.
        If maker fee < 1 bps, strongly recommend maker.
        Otherwise, use maker opportunistically.
        """
        tier = self.current_tiers.get(exchange)
        if not tier:
            return False  # Unknown tier — default to taker

        if tier.maker_fee_bps < 0:
            return True  # Rebate — definitely use maker

        if tier.maker_fee_bps < 1.0:
            return True  # Very cheap maker — use it

        # Otherwise, use maker opportunistically (handled by FeeOptimizer)
        return False

    def get_summary(self) -> Dict[str, Dict]:
        """Get summary of all tracked fee tiers."""
        summary = {}
        for exchange, tier in self.current_tiers.items():
            summary[exchange] = {
                "tier_level": tier.tier_level,
                "maker_fee_bps": tier.maker_fee_bps,
                "taker_fee_bps": tier.taker_fee_bps,
                "volume_30d_usd": tier.volume_30d_usd,
                "next_tier_volume": tier.next_tier_volume,
                "next_tier_maker_bps": tier.next_tier_maker_bps,
                "next_tier_taker_bps": tier.next_tier_taker_bps,
            }
        return summary

    def record_trade_volume(
        self,
        exchange: str,
        notional_usd: float,
    ) -> None:
        """Record trading volume for tier tracking."""
        self._volume_tracker[exchange] = self._volume_tracker.get(exchange, 0.0) + notional_usd

    async def fetch_volume_from_exchange(
        self,
        exchange: str,
        api_client,  # Exchange API client
    ) -> float:
        """Fetch actual 30-day volume from exchange API."""
        try:
            # This would call exchange-specific API
            # Example: api_client.get_account_info() → parse volume
            # For now, return tracked volume
            return self._volume_tracker.get(exchange, 0.0)
        except Exception as exc:
            logger.warning("fee_tier: failed to fetch volume for %s: %s", exchange, exc)
            return 0.0


# Global singleton
_fee_tier_tracker: Optional[FeeTierTracker] = None


def get_fee_tier_tracker() -> FeeTierTracker:
    """Get or create the global fee tier tracker."""
    global _fee_tier_tracker
    if _fee_tier_tracker is None:
        _fee_tier_tracker = FeeTierTracker()
    return _fee_tier_tracker

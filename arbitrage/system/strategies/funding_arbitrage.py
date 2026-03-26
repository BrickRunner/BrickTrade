"""
Funding Rate Arbitrage Strategy:

Hold cross-exchange positions for 8 hours to collect funding payments.
Different risk profile from spot-futures arb: longer hold, lower frequency, stable returns.

Key characteristics:
- Entry: when funding rate differential is attractive (>0.05%)
- Hold: ~8 hours (one funding period)
- Exit: right after funding is collected
- Risk: convergence risk (prices diverge during hold period)
- Return: funding differential - fees - slippage
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional

from arbitrage.system.models import StrategyId, TradeIntent

logger = logging.getLogger("trading_system")


class FundingPosition(Enum):
    """Position type for funding arbitrage."""
    LONG_LOW_SHORT_HIGH = "long_low_short_high"  # Long on low funding, short on high funding
    NEUTRAL = "neutral"


@dataclass
class FundingOpportunity:
    """Opportunity to capture funding rate differential."""
    symbol: str
    long_exchange: str      # Exchange with lower (or negative) funding rate
    short_exchange: str     # Exchange with higher funding rate
    funding_long: float     # Funding rate on long exchange (%)
    funding_short: float    # Funding rate on short exchange (%)
    funding_diff: float     # funding_short - funding_long (%)
    next_funding_time: datetime  # When next funding payment occurs
    hours_until_funding: float   # Time until next payment
    estimated_profit_bps: float  # Expected profit in bps after fees
    spread_bps: float       # Current price spread (for entry cost)
    metadata: Dict = field(default_factory=dict)


@dataclass
class FundingConfig:
    """Configuration for funding arbitrage strategy."""
    enabled: bool = True
    min_funding_diff_pct: float = 0.05  # Minimum funding differential to enter (0.05% = 5 bps)
    max_hold_hours: float = 8.5  # Maximum hold time (slightly over 8h)
    min_hold_hours: float = 7.0  # Minimum hold time before exit
    entry_window_hours: float = 1.0  # Only enter if funding is within N hours
    max_spread_cost_bps: float = 15.0  # Max spread we're willing to pay on entry
    target_profit_bps: float = 10.0  # Target profit after all costs
    max_convergence_risk_bps: float = 30.0  # Max adverse price move tolerance
    rebalance_threshold_bps: float = 50.0  # Rebalance if spread moves this much


class FundingArbitrageStrategy:
    """Strategy for capturing funding rate differentials."""

    def __init__(self, config: FundingConfig):
        self.config = config
        self._active_opportunities: Dict[str, FundingOpportunity] = {}
        self._last_scan_time = datetime.now()

    async def scan_opportunities(
        self,
        symbols: List[str],
        funding_data: Dict[str, Dict[str, float]],  # {exchange: {symbol: rate}}
        spread_data: Dict[str, float],  # {symbol: spread_bps}
        next_funding_times: Dict[str, datetime],  # {exchange: next_funding_time}
    ) -> List[FundingOpportunity]:
        """Scan market for funding arbitrage opportunities."""
        opportunities: List[FundingOpportunity] = []
        now = datetime.now()

        for symbol in symbols:
            # Get funding rates from all exchanges
            rates: Dict[str, float] = {}
            for exchange, symbol_rates in funding_data.items():
                if symbol in symbol_rates:
                    rates[exchange] = symbol_rates[symbol]

            if len(rates) < 2:
                continue  # Need at least 2 exchanges

            # Find pair with maximum funding differential
            exchanges = list(rates.keys())
            best_diff = 0.0
            best_pair: Optional[tuple] = None

            for i, ex1 in enumerate(exchanges):
                for ex2 in exchanges[i + 1:]:
                    diff = abs(rates[ex1] - rates[ex2])
                    if diff > best_diff:
                        best_diff = diff
                        # Long on lower funding rate, short on higher
                        if rates[ex1] < rates[ex2]:
                            best_pair = (ex1, ex2, rates[ex1], rates[ex2])
                        else:
                            best_pair = (ex2, ex1, rates[ex2], rates[ex1])

            if not best_pair or best_diff < self.config.min_funding_diff_pct:
                continue

            long_ex, short_ex, funding_long, funding_short = best_pair

            # Check next funding time
            next_funding = next_funding_times.get(long_ex) or next_funding_times.get(short_ex)
            if not next_funding:
                continue

            hours_until = (next_funding - now).total_seconds() / 3600

            # Only enter if within entry window
            if hours_until > self.config.entry_window_hours or hours_until < 0.1:
                continue

            # Calculate estimated profit
            spread_bps = spread_data.get(symbol, 0.0)
            if spread_bps > self.config.max_spread_cost_bps:
                continue  # Spread too wide

            # Profit = funding differential - entry spread - exit spread - fees
            funding_diff_pct = funding_short - funding_long
            funding_diff_bps = funding_diff_pct * 100  # Convert % to bps
            est_profit_bps = funding_diff_bps - spread_bps * 2 - 10  # 10 bps for fees

            if est_profit_bps < self.config.target_profit_bps:
                continue  # Not profitable enough

            opportunity = FundingOpportunity(
                symbol=symbol,
                long_exchange=long_ex,
                short_exchange=short_ex,
                funding_long=funding_long,
                funding_short=funding_short,
                funding_diff=funding_diff_pct,
                next_funding_time=next_funding,
                hours_until_funding=hours_until,
                estimated_profit_bps=est_profit_bps,
                spread_bps=spread_bps,
            )

            opportunities.append(opportunity)
            logger.info(
                "[FUNDING_OPP] %s: %s(%.4f%%) <-> %s(%.4f%%), diff=%.2f bps, "
                "profit_est=%.2f bps, funding_in=%.1fh",
                symbol, long_ex, funding_long, short_ex, funding_short,
                funding_diff_bps, est_profit_bps, hours_until,
            )

        # Sort by estimated profit
        opportunities.sort(key=lambda o: o.estimated_profit_bps, reverse=True)
        self._last_scan_time = now
        return opportunities

    def create_intent(
        self,
        opp: FundingOpportunity,
        notional_usd: float,
        current_prices: Dict[str, float],  # {exchange: price}
    ) -> TradeIntent:
        """Create a TradeIntent for a funding arbitrage opportunity."""
        long_price = current_prices.get(opp.long_exchange, 0.0)
        short_price = current_prices.get(opp.short_exchange, 0.0)
        mid_price = (long_price + short_price) / 2 if long_price and short_price else 0.0

        # Stop-loss: exit if spread moves against us by max_convergence_risk_bps
        stop_loss_bps = self.config.max_convergence_risk_bps

        return TradeIntent(
            strategy_id=StrategyId.FUNDING_ARBITRAGE,
            symbol=opp.symbol,
            long_exchange=opp.long_exchange,
            short_exchange=opp.short_exchange,
            stop_loss_bps=stop_loss_bps,
            metadata={
                "funding_long": opp.funding_long,
                "funding_short": opp.funding_short,
                "funding_diff_pct": opp.funding_diff,
                "estimated_profit_bps": opp.estimated_profit_bps,
                "entry_spread_bps": opp.spread_bps,
                "next_funding_time": opp.next_funding_time.isoformat(),
                "hours_until_funding": opp.hours_until_funding,
                "entry_mid": mid_price,
                "long_price": long_price,
                "short_price": short_price,
                "strategy_type": "funding_arbitrage",
                "hold_until": (opp.next_funding_time + timedelta(minutes=10)).isoformat(),
            },
        )

    def should_exit_early(
        self,
        position: Dict,  # OpenPosition serialized
        current_time: datetime,
        current_spread_bps: float,
    ) -> tuple[bool, str]:
        """Check if position should be closed early (before funding collection)."""
        entry_time_str = position.get("metadata", {}).get("opened_at")
        if not entry_time_str:
            return False, ""

        try:
            entry_time = datetime.fromisoformat(entry_time_str)
        except (ValueError, TypeError):
            return False, ""

        hold_hours = (current_time - entry_time).total_seconds() / 3600

        # Don't exit before minimum hold time
        if hold_hours < self.config.min_hold_hours:
            return False, ""

        # Exit if spread moved against us significantly
        entry_spread = position.get("metadata", {}).get("entry_spread_bps", 0.0)
        spread_change = current_spread_bps - entry_spread

        if spread_change > self.config.max_convergence_risk_bps:
            return True, f"convergence_risk (spread moved {spread_change:.1f} bps)"

        # Exit if we've held too long
        if hold_hours > self.config.max_hold_hours:
            return True, "max_hold_time_exceeded"

        return False, ""

    def should_exit_for_funding(
        self,
        position: Dict,
        current_time: datetime,
    ) -> bool:
        """Check if it's time to exit after collecting funding."""
        next_funding_str = position.get("metadata", {}).get("next_funding_time")
        if not next_funding_str:
            return False

        try:
            next_funding = datetime.fromisoformat(next_funding_str)
        except (ValueError, TypeError):
            return False

        # Exit 5-10 minutes after funding collection
        exit_window_start = next_funding + timedelta(minutes=5)
        exit_window_end = next_funding + timedelta(minutes=30)

        return exit_window_start <= current_time <= exit_window_end

    def estimate_realized_profit(
        self,
        position: Dict,
        exit_spread_bps: float,
        funding_collected: float,  # Actual funding received (if known)
    ) -> float:
        """Estimate realized profit for a closed position."""
        entry_spread = position.get("metadata", {}).get("entry_spread_bps", 0.0)
        estimated_funding_bps = position.get("metadata", {}).get("funding_diff_pct", 0.0) * 100

        # If we have actual funding collected, use that
        funding_bps = funding_collected * 100 if funding_collected else estimated_funding_bps

        # Profit = funding - entry_spread - exit_spread - fees
        profit_bps = funding_bps - entry_spread - exit_spread_bps - 10  # 10 bps for fees

        return profit_bps

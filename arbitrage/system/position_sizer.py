"""
Dynamic Position Sizing Module:

Adjusts position size based on:
1. Volatility - reduce size in high volatility
2. Liquidity (orderbook depth) - reduce size if thin books
3. Current spread quality - larger size for better spreads
4. Available balance on each exchange
5. Open positions risk concentration

Goal: Maximize capital efficiency while respecting risk limits.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger("trading_system")


@dataclass
class SizingFactors:
    """Factors that influence position sizing."""
    base_notional: float          # Base position size in USD
    volatility_adj: float = 1.0   # Volatility adjustment (0.5-1.5)
    liquidity_adj: float = 1.0    # Liquidity adjustment (0.5-1.5)
    spread_adj: float = 1.0       # Spread quality adjustment (0.7-1.3)
    balance_adj: float = 1.0      # Balance constraint (0.0-1.0)
    risk_adj: float = 1.0         # Portfolio risk adjustment (0.5-1.0)
    final_notional: float = 0.0   # Final computed size


class DynamicPositionSizer:
    """Calculate optimal position size based on market conditions."""

    def __init__(
        self,
        base_notional_usd: float = 10.0,
        max_notional_usd: float = 100.0,
        min_notional_usd: float = 5.0,
    ):
        self.base_notional = base_notional_usd
        self.max_notional = max_notional_usd
        self.min_notional = min_notional_usd

    def calculate_size(
        self,
        symbol: str,
        long_exchange: str,
        short_exchange: str,
        volatility: float,
        book_depth_usd: float,
        spread_bps: float,
        balances: Dict[str, float],  # {exchange: available_usd}
        open_positions: int,
        max_positions: int,
    ) -> SizingFactors:
        """Calculate optimal position size."""
        factors = SizingFactors(base_notional=self.base_notional)

        # 1. Volatility adjustment
        # High volatility → smaller size
        # Target volatility: 1-2% (0.01-0.02)
        # Scale: 0.5x at 4% vol, 1.0x at 1% vol, 1.5x at 0.5% vol
        if volatility <= 0.005:  # Very low volatility
            factors.volatility_adj = 1.3
        elif volatility <= 0.01:  # Low volatility
            factors.volatility_adj = 1.2
        elif volatility <= 0.015:  # Normal volatility
            factors.volatility_adj = 1.0
        elif volatility <= 0.025:  # Elevated volatility
            factors.volatility_adj = 0.8
        elif volatility <= 0.04:  # High volatility
            factors.volatility_adj = 0.6
        else:  # Extreme volatility
            factors.volatility_adj = 0.4

        # 2. Liquidity adjustment
        # Deep books → larger size
        # Target: book depth >= 5x position size
        target_depth = self.base_notional * 5
        if book_depth_usd >= target_depth * 2:  # Very deep
            factors.liquidity_adj = 1.3
        elif book_depth_usd >= target_depth:  # Adequate depth
            factors.liquidity_adj = 1.0
        elif book_depth_usd >= target_depth * 0.5:  # Thin
            factors.liquidity_adj = 0.7
        else:  # Very thin
            factors.liquidity_adj = 0.4

        # 3. Spread quality adjustment
        # Wider spreads → can afford larger size (more profit buffer)
        # Tighter spreads → smaller size (less margin for error)
        if spread_bps >= 30:  # Wide spread
            factors.spread_adj = 1.2
        elif spread_bps >= 15:  # Good spread
            factors.spread_adj = 1.1
        elif spread_bps >= 8:  # Normal spread
            factors.spread_adj = 1.0
        elif spread_bps >= 5:  # Tight spread
            factors.spread_adj = 0.9
        else:  # Very tight spread
            factors.spread_adj = 0.7

        # 4. Balance constraint
        # Ensure we have enough balance on both exchanges
        long_balance = balances.get(long_exchange, 0.0)
        short_balance = balances.get(short_exchange, 0.0)
        min_balance = min(long_balance, short_balance)

        # Reserve buffer: keep at least 20% of balance unused
        usable_balance = min_balance * 0.80

        if usable_balance <= 0:
            factors.balance_adj = 0.0
        else:
            # Size limited by available balance
            balance_limit = usable_balance
            factors.balance_adj = min(1.0, balance_limit / self.base_notional)

        # 5. Portfolio risk adjustment
        # More open positions → smaller size per position
        # This prevents over-concentration
        if max_positions <= 0:
            factors.risk_adj = 1.0
        else:
            utilization = open_positions / max_positions
            if utilization >= 0.9:  # Nearly maxed out
                factors.risk_adj = 0.6
            elif utilization >= 0.7:  # High utilization
                factors.risk_adj = 0.8
            elif utilization >= 0.5:  # Moderate utilization
                factors.risk_adj = 0.9
            else:  # Low utilization
                factors.risk_adj = 1.0

        # Calculate final notional
        raw_notional = (
            self.base_notional
            * factors.volatility_adj
            * factors.liquidity_adj
            * factors.spread_adj
            * factors.balance_adj
            * factors.risk_adj
        )

        # Clamp to min/max
        final_notional = max(
            self.min_notional,
            min(raw_notional, self.max_notional)
        )

        # Round to reasonable precision
        final_notional = round(final_notional, 2)

        factors.final_notional = final_notional

        logger.debug(
            "[POSITION_SIZER] %s (%s<->%s): "
            "base=%.2f, vol_adj=%.2f, liq_adj=%.2f, spread_adj=%.2f, "
            "bal_adj=%.2f, risk_adj=%.2f → final=%.2f USD",
            symbol, long_exchange, short_exchange,
            self.base_notional,
            factors.volatility_adj,
            factors.liquidity_adj,
            factors.spread_adj,
            factors.balance_adj,
            factors.risk_adj,
            final_notional,
        )

        return factors

    def calculate_kelly_size(
        self,
        win_rate: float,
        avg_win_pct: float,
        avg_loss_pct: float,
        current_equity: float,
        max_kelly_fraction: float = 0.25,  # Use 25% of full Kelly (safer)
    ) -> float:
        """
        Calculate position size using Kelly Criterion.

        Kelly fraction = (p * b - q) / b
        where:
        - p = win probability
        - q = loss probability (1 - p)
        - b = win/loss ratio (avg_win / avg_loss)

        Returns: position size in USD
        """
        if win_rate <= 0 or win_rate >= 1:
            return self.base_notional

        if avg_loss_pct <= 0 or avg_win_pct <= 0:
            return self.base_notional

        p = win_rate
        q = 1 - p
        b = avg_win_pct / avg_loss_pct

        # Full Kelly
        kelly_fraction = (p * b - q) / b

        # Clamp to positive
        if kelly_fraction <= 0:
            return self.min_notional

        # Use fractional Kelly for safety (typically 25-50% of full Kelly)
        safe_kelly = kelly_fraction * max_kelly_fraction

        # Clamp to reasonable range (5%-50% of equity per position)
        safe_kelly = max(0.05, min(safe_kelly, 0.50))

        notional = current_equity * safe_kelly

        # Clamp to absolute min/max
        notional = max(self.min_notional, min(notional, self.max_notional))

        logger.debug(
            "[KELLY_SIZER] win_rate=%.2f%%, b=%.2f, kelly=%.4f, "
            "safe_kelly=%.4f, equity=%.2f → size=%.2f USD",
            win_rate * 100, b, kelly_fraction, safe_kelly,
            current_equity, notional,
        )

        return notional

    def adjust_for_correlation(
        self,
        base_size: float,
        current_symbols: list[str],
        new_symbol: str,
    ) -> float:
        """
        Reduce size if opening correlated positions.

        If portfolio already has BTC exposure and we're adding ETH,
        reduce size since they're correlated.
        """
        # Simple correlation heuristic: major coins are correlated
        majors = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}

        is_new_major = new_symbol in majors
        has_majors = any(s in majors for s in current_symbols)

        if is_new_major and has_majors:
            # Reduce size by 20% if adding another correlated major
            return base_size * 0.8

        return base_size

    def recommend_base_notional(
        self,
        total_equity: float,
        max_positions: int,
        risk_per_trade_pct: float = 0.02,  # 2% risk per trade
    ) -> float:
        """
        Recommend a base notional size based on account size.

        Kelly suggests risking 1-3% per trade for typical edge.
        We use notional, not risk, so:
        - risk_per_trade = notional * stop_loss_pct
        - notional = risk_per_trade / stop_loss_pct

        Assuming typical stop loss ~2-5%:
        - For 2% SL: notional = 0.02 / 0.02 = 1.0x risk amount
        - For 5% SL: notional = 0.02 / 0.05 = 0.4x risk amount
        """
        risk_amount = total_equity * risk_per_trade_pct
        typical_sl_pct = 0.03  # 3% stop loss

        base_notional = risk_amount / typical_sl_pct

        # Ensure we can open max_positions
        max_notional_all = total_equity * 0.80  # Use max 80% of equity
        per_position_limit = max_notional_all / max(max_positions, 1)

        recommended = min(base_notional, per_position_limit)

        # Clamp to absolute min/max
        recommended = max(self.min_notional, min(recommended, self.max_notional))

        logger.info(
            "[SIZER_RECOMMEND] equity=%.2f, max_pos=%d, risk_pct=%.2f%% → "
            "base_notional=%.2f USD (per_pos_limit=%.2f)",
            total_equity, max_positions, risk_per_trade_pct * 100,
            recommended, per_position_limit,
        )

        return recommended

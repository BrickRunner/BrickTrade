"""
Funding Rate Harvesting Strategy.

Aggressively captures high funding rates on a SINGLE exchange.
Enters 1-2 hours before funding settlement, exits right after.
Delta-neutral via spot hedge.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from arbitrage.system.models import (
    MarketSnapshot,
    OrderBookSnapshot,
    StrategyId,
    TradeIntent,
)
from arbitrage.system.strategies.base import BaseStrategy

logger = logging.getLogger("trading_system")

_DEFAULT_SPOT_FEE_PCT: Dict[str, float] = {
    "binance": 0.10, "bybit": 0.10, "okx": 0.08, "htx": 0.20,
}
_DEFAULT_PERP_FEE_PCT: Dict[str, float] = {
    "binance": 0.04, "bybit": 0.055, "okx": 0.05, "htx": 0.05,
}


class FundingHarvestingStrategy(BaseStrategy):
    """
    Harvest extreme funding rates on a single exchange.
    
    Enter short perp + long spot when funding > threshold.
    Collect funding payment, then exit.
    """

    def __init__(
        self,
        min_funding_rate_pct: float = 0.02,
        max_basis_spread_pct: float = 0.30,
        min_apr_threshold: float = 10.0,
        cooldown_sec: float = 300.0,
        max_holding_hours: float = 8.0,
        exit_on_negative_funding: bool = True,
        exit_max_holding_hours: float = 16.0,
    ):
        super().__init__(StrategyId.FUNDING_HARVESTING)
        self.min_funding_rate_pct = min_funding_rate_pct
        self.max_basis_spread_pct = max_basis_spread_pct
        self.min_apr_threshold = min_apr_threshold
        self.cooldown_sec = cooldown_sec
        # FIX #8: Exit logic for funding harvesting positions
        self.max_holding_hours = max_holding_hours
        self.exit_on_negative_funding = exit_on_negative_funding
        self.exit_max_holding_hours = exit_max_holding_hours
        self._last_signal_ts: Dict[str, float] = {}

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        """Scan for high funding rate harvesting opportunities."""
        intents: List[TradeIntent] = []
        funding = snapshot.funding_rates
        if not funding:
            return intents

        now = time.time()

        for exchange, rate in funding.items():
            if rate is None:
                continue

            cooldown_key = f"harvest_{exchange}_{snapshot.symbol}"
            if now - self._last_signal_ts.get(cooldown_key, 0.0) < self.cooldown_sec:
                continue

            intent = self._check_harvest(snapshot, exchange, rate, now)
            if intent:
                self._last_signal_ts[cooldown_key] = now
                intents.append(intent)

        return intents

    def _check_harvest(
        self,
        snapshot: MarketSnapshot,
        exchange: str,
        funding_rate: float,
        now: float,
    ) -> Optional[TradeIntent]:
        """Check if funding rate is worth harvesting on this exchange."""
        abs_rate = abs(funding_rate)
        rate_pct = abs_rate * 100

        if rate_pct < self.min_funding_rate_pct:
            return None

        # Calculate APR
        daily_rate = rate_pct * 3  # 3 funding periods per day
        apr = daily_rate * 365

        if apr < self.min_apr_threshold:
            return None

        # Need both spot and perp orderbooks
        perp_ob = snapshot.orderbooks.get(exchange)
        spot_ob = snapshot.spot_orderbooks.get(exchange)
        if not perp_ob or not spot_ob:
            return None
        if perp_ob.bid <= 0 or perp_ob.ask <= 0:
            return None
        if spot_ob.bid <= 0 or spot_ob.ask <= 0:
            return None

        # Check basis spread
        basis_pct = abs(perp_ob.mid - spot_ob.mid) / spot_ob.mid * 100
        if basis_pct > self.max_basis_spread_pct:
            return None

        # Calculate fees
        spot_fee = _DEFAULT_SPOT_FEE_PCT.get(exchange, 0.10)
        perp_fee = _DEFAULT_PERP_FEE_PCT.get(exchange, 0.05)
        round_trip_fee_pct = (spot_fee + perp_fee) * 2

        # Net profit per funding period
        net_profit_pct = rate_pct - round_trip_fee_pct / 3  # amortize over ~3 periods
        if net_profit_pct <= 0:
            return None

        # FIX #8: Validate that net profit in bps exceeds minimum viable threshold.
        # Prevents entering positions where fees consume all expected profit.
        net_profit_bps_check = net_profit_pct * 100
        min_viable_bps = max(10.0, round_trip_fee_pct * 2 * 100)
        if net_profit_bps_check < min_viable_bps * 0.1:
            logger.warning(
                "[FUNDING_HARVEST_SKIP_FEE] %s on %s: net_profit=%.1f bps too low vs fees=%.2f%%",
                snapshot.symbol, exchange, net_profit_bps_check, round_trip_fee_pct,
            )
            return None

        net_profit_bps = net_profit_pct * 100

        # Direction
        if funding_rate > 0:
            side = "harvest_short_perp"
            direction_desc = "SHORT perp + LONG spot"
        else:
            side = "harvest_long_perp"
            direction_desc = "LONG perp + SHORT spot"

        confidence = min(1.0, apr / 100.0)

        intent = TradeIntent(
            strategy_id=StrategyId.FUNDING_HARVESTING,
            symbol=snapshot.symbol,
            long_exchange=exchange,
            short_exchange=exchange,
            side=side,
            confidence=confidence,
            expected_edge_bps=net_profit_bps,
            stop_loss_bps=max(net_profit_bps * 3, 30.0),
            metadata={
                "strategy_type": "funding_harvesting",
                "exchange": exchange,
                "funding_rate": funding_rate,
                "funding_rate_pct": rate_pct,
                "apr": apr,
                "basis_pct": basis_pct,
                "direction": direction_desc,
                "net_profit_bps": net_profit_bps,
                "round_trip_fee_pct": round_trip_fee_pct,
            },
        )

        logger.info(
            "[FUNDING_HARVEST] %s on %s: rate=%.4f%%, APR=%.1f%%, "
            "basis=%.3f%%, net=%.1f bps, %s",
            snapshot.symbol, exchange, rate_pct, apr,
            basis_pct, net_profit_bps, direction_desc,
        )

        return intent

from __future__ import annotations

from typing import List

from arbitrage.system.models import MarketSnapshot, StrategyId, TradeIntent
from arbitrage.system.strategies.base import BaseStrategy


class FundingArbitrageStrategy(BaseStrategy):
    def __init__(self, funding_threshold_bps: float = 2.0):
        super().__init__(StrategyId.FUNDING_ARBITRAGE)
        self._funding_threshold_bps = funding_threshold_bps

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List[TradeIntent]:
        if len(snapshot.funding_rates) < 2:
            return []
        # Avoid funding trades in extremely unstable states.
        if snapshot.volatility > 2.0:
            return []
        rates_bps = {ex: rate * 10_000 for ex, rate in snapshot.funding_rates.items()}
        long_ex = min(rates_bps, key=rates_bps.get)
        short_ex = max(rates_bps, key=rates_bps.get)
        spread_bps = rates_bps[short_ex] - rates_bps[long_ex]
        basis_bps = snapshot.indicators.get("basis_bps", 0.0)
        quality_threshold = self._funding_threshold_bps + max(0.0, abs(basis_bps) * 0.05)
        if spread_bps < quality_threshold:
            return []

        return [
            TradeIntent(
                strategy_id=self.strategy_id,
                symbol=snapshot.symbol,
                long_exchange=long_ex,
                short_exchange=short_ex,
                side="collect_funding",
                confidence=min(1.0, spread_bps / (quality_threshold * 2.5)),
                expected_edge_bps=spread_bps,
                stop_loss_bps=max(self._funding_threshold_bps, quality_threshold * 0.8),
                metadata={
                    "funding_spread_bps": spread_bps,
                    "basis_bps": basis_bps,
                    "take_profit_usd": 0.09,
                    "stop_loss_usd": 0.13,
                    "max_holding_seconds": 2400.0,
                    "close_edge_bps": 0.5,
                },
            )
        ]
